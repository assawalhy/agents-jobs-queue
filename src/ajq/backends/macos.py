"""POSIX backend: direct spawn, `nice` via preexec_fn, killpg for teardown.

On macOS there are no cgroups to lean on, so the memory ceiling is advisory and
enforced by exec.JobRunner sampling `platform.rss_mb`. This backend is also the
Linux fallback when the systemd user manager is unusable, which is why it is
always available on both platforms.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .. import platform
from .base import Handle, effective_resources, resolve_resources


class MacosBackend:
    name = "macos-posix"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        values = resolve_resources(args, kwargs)
        self.memory_mb: int = values["memory_mb"]
        self.cpu_percent: int = values["cpu_percent"]
        self.nice: int = values["nice"]
        self.extra_args: list[str] = values["extra_args"]
        self._live: dict[int, subprocess.Popen] = {}

    # -- introspection ---------------------------------------------------
    def resources(self, job: dict) -> dict[str, Any]:
        return effective_resources(job, {
            "memory_mb": self.memory_mb,
            "cpu_percent": self.cpu_percent,
            "nice": self.nice,
            "extra_args": self.extra_args,
        })

    def rss_mb(self, pid: int) -> int | None:
        return platform.rss_mb(pid)

    # -- lifecycle -------------------------------------------------------
    def spawn(self, job: dict, argv: list[str]) -> Handle:
        nice = self.resources(job)["nice"]

        def preexec() -> None:  # runs in the child, between fork and exec
            if nice > 0:
                try:
                    os.setpriority(os.PRIO_PROCESS, 0, nice)
                except OSError:
                    pass

        cwd = job.get("cwd") or None
        pop = subprocess.Popen(
            list(argv),
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            preexec_fn=preexec if nice > 0 else None,
        )
        pid = pop.pid
        self._live[pid] = pop

        def kill(sig: int) -> None:
            self._killpg(pid, sig)

        def release() -> None:
            self._live.pop(pid, None)

        return Handle(pid=pid, name=self.name, kill=kill, release=release, pop=pop)

    def kill(self, handle: Handle, sig: int) -> None:
        self._killpg(handle.pid, sig)

    def release(self, handle: Handle) -> None:
        self._live.pop(handle.pid, None)
        pop = handle.pop
        if pop is not None and pop.stdout is not None:
            try:
                pop.stdout.close()
            except OSError:
                pass

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _killpg(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or not ours
        except OSError:
            pass