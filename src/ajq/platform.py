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