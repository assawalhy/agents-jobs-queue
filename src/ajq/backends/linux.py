"""Linux backend: every job runs inside its own transient systemd scope unit.

    systemd-run --user --scope --quiet --unit=ajq-<id> \
      -p MemoryMax=<m>M -p MemorySwapMax=0 -p CPUQuota=<cpu>% \
      [-p Nice=<n>] -p OOMPolicy=stop -- <argv>

The unit owns the cgroup, so a SIGTERM to the process group is not enough on its
own: children that re-forked into other groups are still bounded by the cgroup,
and the kernel OOM killer is told to stop the whole scope (`OOMPolicy=stop`)
rather than pick a victim inside it. That is why kill() signals the group *and*
the unit.

`-p Nice=` is optional and probe-gated: scope units have no ExecContext, so every
systemd in the field rejects it ("Unknown assignment: Nice=..."). Where it is
unsupported the job is wrapped in `nice -n <n> --` instead, so niceness is still
enforced and `nice_applied_by` says which route is in use.

When systemd is not usable the backend delegates to MacosBackend and renames
itself `linux-posix-fallback` so `ajq doctor` shows the downgrade.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Any, Optional

from .base import Handle, effective_resources, resolve_resources
from .macos import MacosBackend

UNIT_PREFIX = "ajq-"
SYSTEMD_NAME = "linux-systemd"
FALLBACK_NAME = "linux-posix-fallback"

_systemd_cache: Optional[bool] = None


def _systemctl(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_usable(refresh: bool = False) -> bool:
    """True when `systemd-run` exists and the user manager answers."""
    global _systemd_cache
    if _systemd_cache is not None and not refresh:
        return _systemd_cache
    ok = False
    if shutil.which("systemd-run") and shutil.which("systemctl"):
        ok = _probe_manager()
    _systemd_cache = ok
    return ok


def _probe_manager() -> bool:
    if not (os.environ.get("XDG_RUNTIME_DIR") or os.path.isdir(f"/run/user/{os.getuid()}")):
        return False
    out = _systemctl("show-environment")
    if out is not None and out.returncode == 0:
        return True
    out = _systemctl("is-system-running")
    if out is None:
        return False
    state = out.stdout.decode("utf-8", "replace").strip()
    return out.returncode == 0 and state in ("running", "starting", "degraded")


def _probe_scope_nice() -> bool:
    """True when this systemd accepts `Nice=` on a transient *scope*.

    Scope units have no ExecContext, so `Nice=` is rejected outright
    ("Unknown assignment: Nice=...") and the contract vector cannot be used
    verbatim; the probe is cached per backend instance.
    """
    unit = f"{UNIT_PREFIX}probe-{os.getpid()}"
    try:
        out = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
             "-p", "Nice=10", "--", "/bin/true"],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    _systemctl("stop", unit)
    return out.returncode == 0


def _unit_settled(unit: str, timeout: float = 3.0) -> bool:
    """Wait until the scope unit has left the active/deactivating states.

    A scope killed by the cgroup OOM killer only lands in `failed` after the
    runner reaps `systemd-run`, so `stop`/`reset-failed` issued any earlier are
    no-ops and the transient unit is left behind in the user's session.
    """
    deadline = time.monotonic() + timeout
    while True:
        out = _systemctl("is-active", unit)
        state = out.stdout.decode("utf-8", "replace").strip() if out else "unknown"
        if state not in ("active", "activating", "deactivating", "reloading"):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _drop_unit(unit: str) -> None:
    if not unit:
        return
    _unit_settled(unit)
    _systemctl("stop", unit)
    _systemctl("reset-failed", unit)


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return str(sig)


def unit_for(job_id: str) -> str:
    return f"{UNIT_PREFIX}{job_id}"


class LinuxBackend:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        values = resolve_resources(args, kwargs)
        self.memory_mb: int = values["memory_mb"]
        self.cpu_percent: int = values["cpu_percent"]
        self.nice: int = values["nice"]
        self.extra_args: list[str] = values["extra_args"]
        self._live: dict[int, subprocess.Popen] = {}
        self._units: dict[int, str] = {}
        self._fallback: Optional[MacosBackend] = None
        self.degrade_reason: str | None = None
        self.nice_applied_by = "systemd-property"
        self._nice_supported: Optional[bool] = None
        self.name = SYSTEMD_NAME
        if not _systemd_usable():
            self._degrade("systemd-run/systemctl --user unavailable")
        else:
            self.nice_supported()
        self.degraded = self._fallback is not None

    # -- degradation -----------------------------------------------------
    def _degrade(self, why: str) -> None:
        if self._fallback is None:
            self._fallback = MacosBackend(
                memory_mb=self.memory_mb,
                cpu_percent=self.cpu_percent,
                nice=self.nice,
                extra_args=list(self.extra_args),
            )
        self.name = FALLBACK_NAME
        self.degraded = True
        self.degrade_reason = why

    # -- introspection ---------------------------------------------------
    def resources(self, job: dict) -> dict[str, Any]:
        return effective_resources(job, {
            "memory_mb": self.memory_mb,
            "cpu_percent": self.cpu_percent,
            "nice": self.nice,
            "extra_args": self.extra_args,
        })

    def rss_mb(self, pid: int) -> int | None:
        if self._fallback is not None:
            return self._fallback.rss_mb(pid)
        return self._tree_rss_mb(pid)

    # -- lifecycle -------------------------------------------------------
    def nice_supported(self) -> bool:
        if self._nice_supported is None:
            self._nice_supported = _probe_scope_nice()
            if not self._nice_supported:
                self.nice_applied_by = "argv-wrapper" if shutil.which("nice") else "none"
        return self._nice_supported

    def command(self, job: dict, argv: list[str]) -> list[str]:
        limits = self.resources(job)
        props = [
            f"MemoryMax={limits['memory_mb']}M",
            "MemorySwapMax=0",
            f"CPUQuota={limits['cpu_percent']}%",
        ]
        payload = list(argv)
        if self.nice_supported():
            props.append(f"Nice={limits['nice']}")
        elif limits["nice"] > 0 and shutil.which("nice"):
            payload = ["nice", "-n", str(limits["nice"]), "--", *payload]
        props.append("OOMPolicy=stop")
        command = ["systemd-run", "--user", "--scope", "--quiet",
                   f"--unit={unit_for(str(job.get('id', '')))}"]
        for prop in props:
            command += ["-p", prop]
        return [*command, *limits["extra_args"], "--", *payload]

    def spawn(self, job: dict, argv: list[str]) -> Handle:
        if self._fallback is not None:
            return self._fallback.spawn(job, argv)
        try:
            pop = subprocess.Popen(
                self.command(job, list(argv)),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            self._degrade(f"systemd-run unusable: {exc}")
            return self._fallback.spawn(job, argv)
        return self._handle(job, pop)

    def _handle(self, job: dict, pop: subprocess.Popen) -> Handle:
        pid = pop.pid
        unit = unit_for(str(job.get("id", "")))
        self._live[pid] = pop
        self._units[pid] = unit

        def kill(sig: int) -> None:
            self._kill(pid, unit, sig)

        def release() -> None:
            self._live.pop(pid, None)
            self._units.pop(pid, None)
            _drop_unit(unit)

        return Handle(pid=pid, name=self.name, kill=kill, release=release, pop=pop)

    def kill(self, handle: Handle, sig: int) -> None:
        if self._fallback is not None or handle.name == FALLBACK_NAME:
            (self._fallback or MacosBackend()).kill(handle, sig)
            return
        self._kill(handle.pid, self._units.get(handle.pid, ""), sig)

    def release(self, handle: Handle) -> None:
        if self._fallback is not None or handle.name == FALLBACK_NAME:
            (self._fallback or MacosBackend()).release(handle)
            return
        unit = self._units.pop(handle.pid, "")
        self._live.pop(handle.pid, None)
        _drop_unit(unit)

    # -- helpers ---------------------------------------------------------
    def _kill(self, pid: int, unit: str, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except OSError:
            pass  # ESRCH: already gone; EPERM: not ours. Both are fine.
        if unit:
            _systemctl("kill", f"--signal={_signal_name(sig)}", unit)

    def _tree_rss_mb(self, pid: int) -> int | None:
        """RSS of `pid` and every descendant.

        The pid the runner holds is `systemd-run`, not the job itself, and the
        job may sit in its own process group, so sampling one /proc entry would
        measure the wrong thing.
        """
        pages: dict[int, int] = {}
        children: dict[int, list[int]] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return _platform_rss(pid)
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8") as handle:
                    stat = handle.read()
                tail = stat[stat.rindex(")") + 1:].split()
                ppid, rss_pages = int(tail[1]), int(tail[21])
            except (OSError, ValueError, IndexError):
                continue
            child = int(entry)
            pages[child] = rss_pages
            children.setdefault(ppid, []).append(child)
        if pid not in pages:
            return _platform_rss(pid)
        total = 0
        seen = {pid}
        frontier = [pid]
        while frontier:
            for child in children.get(frontier.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
                    total += pages.get(child, 0)
        return total * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)


def _platform_rss(pid: int) -> int | None:
    from .. import platform as _platform

    return _platform.rss_mb(pid)