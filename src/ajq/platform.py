"""Platform probes for Linux and macOS (POSIX only, no Windows).

Nothing here raises on an unexpected reading: every probe degrades to a
conservative default so the scheduler can always make a decision.
"""

from __future__ import annotations

import os
import subprocess
import sys

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0])  # kB on Linux
    except OSError:
        pass
    return values


def _sysctl(name: str) -> int:
    try:
        out = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=2.0, check=False
        )
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _vm_stat() -> tuple[int, int]:
    """Return (free_pages, reclaimable_pages, page_size) from vm_stat."""
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=2.0, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 0, 0, 4096
    page_size = 4096
    free = inactive = speculative = 0
    for line in out.stdout.splitlines():
        if line.startswith("Mach Virtual Memory Statistics"):
            continue
        key, _, value = line.partition(":")
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            continue
        number = int(digits)
        if key == "page size":
            page_size = number
        elif key == "Pages free":
            free = number
        elif key == "Pages inactive":
            inactive = number
        elif key == "Pages speculative":
            speculative = number
    return free, inactive + speculative, page_size


def total_memory_mb() -> int:
    if IS_LINUX:
        return _read_meminfo().get("MemTotal", 0) // 1024
    return _sysctl("hw.memsize") // (1024 * 1024)


def available_memory_mb() -> int:
    """Memory that can be handed to a new job without pushing the box into swap."""
    if IS_LINUX:
        info = _read_meminfo()
        available = info.get("MemAvailable")
        if available:
            return available // 1024
        return max(0, (info.get("MemFree", 0) + info.get("Cached", 0)) // 1024)
    free, reclaimable, page_size = _vm_stat()
    return (free + reclaimable) * page_size // (1024 * 1024)


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def swap_devices() -> list[tuple[str, int]]:
    """(device path, priority) for every active swap area in /proc/swaps."""
    if not IS_LINUX:
        return []
    devices: list[tuple[str, int]] = []
    for line in _read_text("/proc/swaps").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            devices.append((fields[0], int(fields[4])))
        except ValueError:
            continue
    return devices


def _zram_names(devices: list[tuple[str, int]]) -> list[str]:
    return [os.path.basename(path) for path, _ in devices if os.path.basename(path).startswith("zram")]


def zram_is_primary_swap() -> bool:
    """True when a zram device is active swap and no other swap area outranks it.

    The kernel fills the highest-priority swap first, so pages reclaimed from a
    job land in zram only when zram has the top priority.
    """
    devices = swap_devices()
    zram = [priority for path, priority in devices if os.path.basename(path).startswith("zram")]
    other = [priority for path, priority in devices if not os.path.basename(path).startswith("zram")]
    return bool(zram) and (not other or max(zram) >= max(other))


def zram_credit_mb() -> int:
    """RAM a swap-out to zram would still free, summed over zram swap devices.

    Free logical zram space times (1 - mem_used_total/orig_data_size): a page moved
    to a device storing 4:1 frees three quarters of its size. A device's
    mem_limit caps how much more it can store. Zero when zram is not the swap
    the kernel uses first, and disk swap gives none.
    """
    if not zram_is_primary_swap():
        return 0
    total = 0
    for name in _zram_names(swap_devices()):
        disksize = _read_text(f"/sys/block/{name}/disksize").strip()
        stat = _read_text(f"/sys/block/{name}/mm_stat").split()
        if not disksize.isdigit() or len(stat) < 4 or not all(field.isdigit() for field in stat[:4]):
            continue
        orig, used, limit = int(stat[0]), int(stat[2]), int(stat[3])
        ratio = used / orig if orig > 0 else 0.5
        free = max(0, int(disksize) - orig)
        if limit > 0 and ratio > 0:
            free = min(free, int(max(0, limit - used) / ratio))
        total += int(free * max(0.0, 1.0 - ratio))
    return total // (1024 * 1024)


def cgroup_v2_memory() -> bool:
    """True when the systemd user manager can set memory.high / memory.swap.max.

    Needs the unified cgroup v2 hierarchy with the memory controller delegated
    to user@<uid>.service; cgroup v1 and hybrid setups ignore MemoryHigh and
    MemorySwapMax.
    """
    if not IS_LINUX:
        return False
    uid = os.getuid()
    path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    return "memory" in _read_text(path).split()


def psi_avg10(resource: str, line: str = "some") -> float | None:
    """`<line> avg10` from /proc/pressure/<resource>, None when PSI is absent."""
    if not IS_LINUX:
        return None
    for row in _read_text(f"/proc/pressure/{resource}").splitlines():
        if not row.startswith(line + " "):
            continue
        for field in row.split()[1:]:
            key, _, value = field.partition("=")
            if key == "avg10":
                try:
                    return float(value)
                except ValueError:
                    return None
    return None


_THERMAL_ZONES = ("x86_pkg_temp", "cpu_thermal", "cpu-thermal")
_HWMON_CPU = ("coretemp", "k10temp", "zenpower")
# k10temp's Tctl (temp1) carries a fixed offset of up to 27 C on some Ryzen and
# Threadripper parts; Tdie / Tccd* are the real die readings when present.
_DIE_LABELS = ("Tdie", "Tccd")


def _hwmon_temps(base: str) -> list[int]:
    labelled: list[int] = []
    plain: list[int] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    for entry in entries:
        if not (entry.startswith("temp") and entry.endswith("_input")):
            continue
        raw = _read_text(f"{base}/{entry}").strip()
        if not raw.lstrip("-").isdigit():
            continue
        label = _read_text(f"{base}/{entry[:-len('_input')]}_label").strip()
        if label.startswith(_DIE_LABELS):
            labelled.append(int(raw))
        else:
            plain.append(int(raw))
    return labelled or plain


def cpu_temp_c() -> float | None:
    """Hottest CPU package/die sensor in degrees C, None when none is found."""
    if not IS_LINUX:
        return None
    readings: list[int] = []
    try:
        zones = os.listdir("/sys/class/thermal")
    except OSError:
        zones = []
    for zone in zones:
        if _read_text(f"/sys/class/thermal/{zone}/type").strip() in _THERMAL_ZONES:
            raw = _read_text(f"/sys/class/thermal/{zone}/temp").strip()
            if raw.lstrip("-").isdigit():
                readings.append(int(raw))
    try:
        monitors = os.listdir("/sys/class/hwmon")
    except OSError:
        monitors = []
    for monitor in monitors:
        if _read_text(f"/sys/class/hwmon/{monitor}/name").strip() in _HWMON_CPU:
            readings.extend(_hwmon_temps(f"/sys/class/hwmon/{monitor}"))
    return max(readings) / 1000.0 if readings else None


def cpu_count() -> int:
    return os.cpu_count() or 1


def interpreter_path() -> str:
    """Absolute interpreter path.

    The systemd user manager on this machine has a PATH that excludes
    ~/.local/bin, so unit files must embed this instead of `python3`.
    """
    return os.path.realpath(sys.executable)


def rss_mb(pid: int) -> int | None:
    """Resident set size of `pid` in MiB, or None when it cannot be sampled."""
    if IS_LINUX:
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        value = out.stdout.strip()
        return int(value) // 1024 if value.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def set_nice(pid: int, niceness: int) -> None:
    """Lower (numerically raise) the priority of `pid`; best effort."""
    try:
        os.setpriority(os.PRIO_PROCESS, pid, niceness)
    except (OSError, PermissionError):
        pass