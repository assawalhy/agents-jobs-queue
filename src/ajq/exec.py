"""Job execution: stream a child's merged output to its log, enforce the caps.

One `JobRunner.run` call owns one child process for its whole life:

  * a daemon reader thread drains stdout+stderr into `out.log` so a chatty child
    can never fill the pipe and wedge itself, or wedge us,
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

from . import paths

TRUNCATION_MARKER = b"\n[ajq] output limit reached; output truncated\n"
DEFAULT_KILL_GRACE_S = 10.0
CHUNK = 65536
JOIN_TIMEOUT_S = 2.0
REAP_TIMEOUT_S = 5.0


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


class _OutputPump:
    """Drains the child's pipe into the log fd while counting bytes."""

    def __init__(
        self,
        fd: int,
        stream: Any,
        limit: int,
        on_bytes: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.fd = fd
        self.stream = stream
        self.limit = max(0, int(limit or 0))
        self.on_bytes = on_bytes
        self.written = 0
        self.truncated = False
        self._lock = threading.Lock()

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
                os.write(self.fd, data)
                self.written += len(data)
        if self.on_bytes is not None:
            self.on_bytes(len(data))

    def append_marker(self, marker: bytes) -> None:
        with self._lock:
            os.write(self.fd, marker)
            self.written += len(marker)
        if self.on_bytes is not None:
            self.on_bytes(len(marker))


class JobRunner:
    def __init__(
        self,
        backend: Any,
        poll_interval: float = 0.25,
        memory_limit_mb: Optional[int] = None,
    ) -> None:
        self.backend = backend
        self.poll_interval = max(0.01, float(poll_interval))
        self.memory_limit_mb = memory_limit_mb
        self._last_pid = 0
        self._pid_lock = threading.Lock()

    @property
    def last_pid(self) -> int:
        """pid of the most recently spawned child, or 0."""
        with self._pid_lock:
            return self._last_pid

    # -- limits ----------------------------------------------------------
    def memory_limit_mb_for(self, job: dict) -> Optional[int]:
        """job override, then the runner default, then the backend default."""
        for value in (job.get("memory_mb"), self.memory_limit_mb,
                      getattr(self.backend, "memory_mb", None)):
            if value is None:
                continue
            limit = int(value)
            if limit > 0:
                return limit
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
        paths.ensure_dir(os.path.dirname(out_path))

        started = time.monotonic()
        out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(out_fd, 0o600)
        pump = _OutputPump(out_fd, None, int(job.get("max_output_bytes") or 0), on_out_bytes)
        try:
            handle = self.backend.spawn(job, list(job.get("argv") or []))
        except OSError as exc:
            note = f"[ajq] spawn failed: {exc}\n".encode()
            os.write(out_fd, note)
            os.close(out_fd)
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
            if pump.truncated:
                if reason is None:
                    reason = "output_limit"
                pump.append_marker(TRUNCATION_MARKER)
        finally:
            self._drain_stop(handle, pump, thread)
            try:
                handle.release()
            except Exception:
                pass
            os.close(out_fd)

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