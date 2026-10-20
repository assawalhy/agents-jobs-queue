"""Job execution: stream a child's merged output to its logs, enforce the caps.

One `JobRunner.run` call owns one child process for its whole life:

  * a daemon reader thread drains stdout+stderr into `color.log` (the raw
    bytes) and `out.log` (the same bytes without the escape sequences) so a
    chatty child can never fill the pipe and wedge itself, or wedge us,
  * a poll loop checks the limits in a fixed order (timeout, output, memory,
    cancel) and, on a violation, SIGTERMs the process group and escalates to
    SIGKILL after `kill_grace_s`,
  * everything is reaped before returning, so no zombie survives a kill.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import ansi, paths
from .backends.base import hard_ceiling_mb

TRUNCATION_MARKER = b"\n[ajq] output limit reached; output truncated\n"
DEFAULT_KILL_GRACE_S = 10.0
CHUNK = 65536
JOIN_TIMEOUT_S = 2.0
REAP_TIMEOUT_S = 5.0

# The variables that make a tool colour its output. `TERM` is added on top of
# them when the daemon's own TERM is unset or dumb.
COLOR_VARS: dict[str, str] = {
    "FORCE_COLOR": "1",
    "CLICOLOR_FORCE": "1",
    "CLICOLOR": "1",
    "PY_COLORS": "1",
    "CARGO_TERM_COLOR": "always",
}
COLOR_TERM = "xterm-256color"


@dataclass
class RunResult:
    exit_code: Optional[int]
    signal: Optional[int]
    kill_reason: Optional[str]  # None|timeout|output_limit|memory_limit|canceled
    out_bytes: int
    truncated: bool
    elapsed_s: float
    pid: int = 0  # the child's pid, 0 when it never spawned


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _config_get(config: Any, dotted: str, default: Any = None) -> Any:
    """One dotted key out of a Config object or a plain nested dict."""
    if isinstance(config, dict):
        node: Any = config
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(dotted, default)
        except TypeError:
            return default
    return default


def color_env(config: Any) -> dict[str, str]:
    """The environment variables that make a tool colour its output.

    Empty unless the config key `output.color` is exactly "on"; any other
    value turns the forcing off. `TERM` is only set when the daemon's own TERM
    is unset or dumb. The daemon's `NO_COLOR` is left alone: this helper never
    sets it and never clears it.
    """
    if str(_config_get(config, "output.color", "off")).strip() != "on":
        return {}
    env = dict(COLOR_VARS)
    term = (os.environ.get("TERM") or "").strip()
    if not term or term.lower() == "dumb":
        env["TERM"] = COLOR_TERM
    return env


class _OutputPump:
    """Drains the child's pipe into the two log files while counting bytes.

    `color.log` keeps the raw bytes, `out.log` the same bytes with the escape
    sequences removed. The cap and the byte count are about the raw bytes, so
    the cap means what it meant when there was one log file.
    """

    def __init__(
        self,
        raw_fd: int,
        clean_fd: int,
        stream: Any,
        limit: int,
        on_bytes: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.raw_fd = raw_fd
        self.clean_fd = clean_fd
        self.stream = stream
        self.limit = max(0, int(limit or 0))
        self.on_bytes = on_bytes
        self.written = 0
        self.truncated = False
        self._lock = threading.Lock()
        self._stripper = ansi.StreamStripper()

    def drain(self) -> None:
        try:
            fileno = self.stream.fileno()
        except (AttributeError, OSError, ValueError):
            return
        while True:
            try:
                chunk = os.read(fileno, CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self.write(chunk)

    def write(self, data: bytes) -> None:
        with self._lock:
            if self.limit and self.written >= self.limit:
                self.truncated = True
                return
            if self.limit and self.written + len(data) > self.limit:
                data = data[: self.limit - self.written]
                self.truncated = True
            if data:
                os.write(self.raw_fd, data)
                clean = self._stripper.feed(data)
                if clean:
                    os.write(self.clean_fd, clean)
                self.written += len(data)
        if self.on_bytes is not None:
            self.on_bytes(len(data))

    def finish(self, marker: bytes = b"") -> None:
        """Flush the stripper and append the truncation marker to both files.

        Called once the reader thread is done, so the held back tail and the
        marker land at the end of both files in that order.
        """
        with self._lock:
            tail = self._stripper.flush()
            if tail:
                os.write(self.clean_fd, tail)
            if marker:
                os.write(self.raw_fd, marker)
                os.write(self.clean_fd, marker)
                self.written += len(marker)
        if marker and self.on_bytes is not None:
            self.on_bytes(len(marker))


class JobRunner:
    def __init__(
        self,
        backend: Any,
        poll_interval: float = 0.25,
        memory_limit_mb: Optional[int] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.backend = backend
        self.poll_interval = max(0.01, float(poll_interval))
        self.memory_limit_mb = memory_limit_mb
        self.env = dict(env or {})
        self._last_pid = 0
        self._pid_lock = threading.Lock()

    @property
    def last_pid(self) -> int:
        """pid of the most recently spawned child, or 0."""
        with self._pid_lock:
            return self._last_pid

    # -- limits ----------------------------------------------------------
    def memory_limit_mb_for(self, job: dict) -> Optional[int]:
        """Kill ceiling: the cap (job override, runner default, backend default),
        times hard_factor only when the backend enforces the cap as MemoryHigh."""
        soft_cap = getattr(self.backend, "soft_cap", None)
        factor = getattr(self.backend, "hard_factor", 1.0) if callable(soft_cap) and soft_cap() else 1.0
        if not isinstance(factor, (int, float)) or isinstance(factor, bool):
            factor = 1.0
        for value in (job.get("memory_mb"), self.memory_limit_mb,
                      getattr(self.backend, "memory_mb", None)):
            if value is None:
                continue
            limit = int(value)
            if limit > 0:
                return hard_ceiling_mb(limit, float(factor))
        return None

    # -- execution -------------------------------------------------------
    def run(
        self,
        job: dict,
        cancel: threading.Event,
        on_out_bytes: Optional[Callable[[int], None]] = None,
    ) -> RunResult:
        job_id = str(job["id"])
        timeout_s = _as_float(job.get("timeout_s"), 0.0)
        grace_s = _as_float(job.get("kill_grace_s"), DEFAULT_KILL_GRACE_S)
        memory_limit = self.memory_limit_mb_for(job)
        out_path = paths.job_out_path(job_id)
        color_path = paths.job_color_path(job_id)
        paths.ensure_dir(os.path.dirname(out_path))

        started = time.monotonic()
        out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(out_fd, 0o600)
        color_fd = os.open(color_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(color_fd, 0o600)
        pump = _OutputPump(
            color_fd, out_fd, None, int(job.get("max_output_bytes") or 0), on_out_bytes
        )
        try:
            handle = self.backend.spawn(job, list(job.get("argv") or []), env=self.env)
        except OSError as exc:
            note = f"[ajq] spawn failed: {exc}\n".encode()
            os.write(out_fd, note)
            os.write(color_fd, note)
            os.close(out_fd)
            os.close(color_fd)
            return RunResult(127, None, None, len(note), False, time.monotonic() - started)

        child_pid = int(getattr(handle, "pid", 0) or 0)
        with self._pid_lock:
            self._last_pid = child_pid

        thread: Optional[threading.Thread] = None
        stream = getattr(handle.pop, "stdout", None)
        if stream is not None:
            pump.stream = stream
            thread = threading.Thread(target=pump.drain, name=f"ajq-out-{job_id}", daemon=True)
            thread.start()

        reason: Optional[str] = None
        returncode: Optional[int] = None
        try:
            deadline = started + timeout_s if timeout_s else None
            while True:
                reason = self._violation(handle, pump, memory_limit, cancel, deadline)
                if reason is not None:
                    returncode = self._terminate(handle, grace_s)
                    break
                returncode = self._poll(handle)
                if returncode is not None:
                    break
                sleep_for = self.poll_interval
                if deadline is not None:
                    sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
                if sleep_for > 0:
                    time.sleep(sleep_for)
        finally:
            # The reader thread has to be done before the stripper is flushed
            # and the marker is appended, so both files end in the same order.
            self._drain_stop(handle, pump, thread)
            if pump.truncated and reason is None:
                reason = "output_limit"
            pump.finish(TRUNCATION_MARKER if pump.truncated else b"")
            try:
                handle.release()
            except Exception:
                pass
            os.close(out_fd)
            os.close(color_fd)

        truncated = pump.truncated
        exit_code: Optional[int] = returncode
        killed_by: Optional[int] = None
        if returncode is not None and returncode < 0:
            exit_code = None
            killed_by = -returncode
        return RunResult(
            exit_code=exit_code,
            signal=killed_by,
            kill_reason=reason,
            out_bytes=pump.written,
            truncated=truncated,
            elapsed_s=time.monotonic() - started,
            pid=child_pid,
        )

    # -- helpers ---------------------------------------------------------
    def _violation(
        self,
        handle: Any,
        pump: _OutputPump,
        memory_limit: Optional[int],
        cancel: threading.Event,
        deadline: Optional[float],
    ) -> Optional[str]:
        """Which limit (if any) the still-running child has broken, in fixed order."""
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout"
        if pump.truncated:
            return "output_limit"
        if memory_limit is not None:
            rss = self.backend.rss_mb(handle.pid)
            if rss is not None and rss > memory_limit:
                return "memory_limit"
        if cancel.is_set():
            return "canceled"
        return None

    @staticmethod
    def _poll(handle: Any) -> Optional[int]:
        """Return the child's returncode once it is gone, else None."""
        pop = handle.pop
        if pop is not None:
            return pop.poll()
        try:
            os.kill(handle.pid, 0)
        except (ProcessLookupError, PermissionError):
            return 0  # gone; without a Popen the status is unknowable
        return None

    def _terminate(self, handle: Any, grace_s: float) -> Optional[int]:
        try:
            self.backend.kill(handle, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + grace_s
        while True:
            returncode = self._poll(handle)
            if returncode is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.backend.kill(handle, signal.SIGKILL)
                except OSError:
                    pass
                break
            time.sleep(min(0.05, remaining))
        pop = handle.pop
        if pop is not None:
            try:
                return pop.wait(timeout=REAP_TIMEOUT_S)
            except Exception:
                return pop.poll()
        return self._poll(handle)

    @staticmethod
    def _drain_stop(handle: Any, pump: _OutputPump, thread: Optional[threading.Thread]) -> None:
        if thread is not None:
            thread.join(timeout=JOIN_TIMEOUT_S)
        stream = pump.stream
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass