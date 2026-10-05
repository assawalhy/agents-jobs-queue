"""Backend contract types plus resource-resolution helpers.

A backend knows how to spawn one job under the box's resource controls and how
to signal its whole process group. `exec.JobRunner` drives the poll loop; the
backend only spawns, signals and samples RSS.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

DEFAULT_MEMORY_MB = 2048
DEFAULT_CPU_PERCENT = 200
DEFAULT_NICE = 10
DEFAULT_EXTRA_ARGS: tuple[str, ...] = ()

_INT_FIELDS = ("memory_mb", "cpu_percent", "nice")

RESOURCE_DEFAULTS: dict[str, Any] = {
    "memory_mb": DEFAULT_MEMORY_MB,
    "cpu_percent": DEFAULT_CPU_PERCENT,
    "nice": DEFAULT_NICE,
    "extra_args": DEFAULT_EXTRA_ARGS,
}


@dataclass
class Handle:
    """One spawned job.

    `kill`/`release` are bound to this job's process group (and, on Linux, to its
    systemd scope unit) so callers do not need to know which backend owns them.
    `pop` is the owning Popen, when the backend has one: exec needs it for the
    merged-output pipe and for wait/reap.
    """

    pid: int
    name: str
    kill: Callable[[int], None]
    release: Callable[[], None]
    pop: Optional[subprocess.Popen] = field(default=None, repr=False)


@runtime_checkable
class ResourceBackend(Protocol):
    name: str

    def spawn(self, job: dict, argv: list[str]) -> Handle: ...

    def rss_mb(self, pid: int) -> int | None: ...

    def kill(self, handle: Handle, sig: int) -> None: ...


def _mapping(value: Any) -> dict[str, Any]:
    """Best-effort dict view of a resources mapping or a Config-like object."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            data = as_dict()
        except Exception:
            data = None
        if isinstance(data, dict):
            resources = data.get("resources")
            if isinstance(resources, dict):
                return dict(resources)
            return data
    get = getattr(value, "get", None)
    if callable(get):
        out: dict[str, Any] = {}
        for key, default in (
            ("resources.memory_mb", DEFAULT_MEMORY_MB),
            ("resources.cpu_percent", DEFAULT_CPU_PERCENT),
            ("resources.nice", DEFAULT_NICE),
            ("resources.extra_args", DEFAULT_EXTRA_ARGS),
        ):
            try:
                out[key.rsplit(".", 1)[1]] = get(key, default)
            except Exception:
                continue
        return out
    return {}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_resources(args: Sequence[Any] = (), kwargs: dict | None = None) -> dict[str, Any]:
    """Normalise constructor arguments into the four resource values.

    Accepts the shapes the daemon may hand over: no arguments, plain ints
    `(memory_mb, cpu_percent, nice)`, a `config.resources` mapping, or a Config
    object. Explicit keyword arguments win.
    """
    found: dict[str, Any] = {}
    ints: list[int] = []
    for arg in args:
        mapping = _mapping(arg)
        if mapping:
            found.update(mapping)
        elif isinstance(arg, (int, float)) and not isinstance(arg, bool):
            ints.append(int(arg))
    for name, value in zip(_INT_FIELDS, ints):
        found.setdefault(name, value)
    for key, value in (kwargs or {}).items():
        if value is not None:
            found[key] = value

    extra = found.get("extra_args")
    if isinstance(extra, str):
        extra = [extra]
    return {
        "memory_mb": max(1, _as_int(found.get("memory_mb"), DEFAULT_MEMORY_MB)),
        "cpu_percent": max(1, _as_int(found.get("cpu_percent"), DEFAULT_CPU_PERCENT)),
        "nice": max(0, _as_int(found.get("nice"), DEFAULT_NICE)),
        "extra_args": [str(part) for part in (extra or ())],
    }


def effective_resources(job: dict, defaults: dict[str, Any]) -> dict[str, Any]:
    """Per-job overrides (`memory_mb`, `cpu_percent`) over the constructor values.

    A stored 0/None means "not specified on this job", so it falls back to the
    defaults instead of clamping to the minimum.
    """
    effective = dict(defaults)
    for key in ("memory_mb", "cpu_percent", "nice"):
        value = _as_int(job.get(key), 0)
        if value > 0:
            effective[key] = value
    return effective


def get_backend(name: str = "auto", *args: Any, **kwargs: Any) -> ResourceBackend:
    """Resolve a backend name to an instance.

    auto -> systemd cgroups on Linux, direct POSIX spawn elsewhere.
    linux -> LinuxBackend, which self-degrades to POSIX spawn when systemd is
             unavailable and reports name "linux-posix-fallback".
    macos|posix -> MacosBackend.
    """
    from .linux import LinuxBackend
    from .macos import MacosBackend

    key = (name or "auto").strip().lower()
    if key == "auto":
        from .. import platform

        backend_class = LinuxBackend if platform.IS_LINUX else MacosBackend
    elif key == "linux":
        backend_class = LinuxBackend
    elif key in ("macos", "posix"):
        backend_class = MacosBackend
    else:
        raise ValueError(f"unknown backend: {name!r} (expected auto|linux|macos|posix)")
    return backend_class(*args, **kwargs)