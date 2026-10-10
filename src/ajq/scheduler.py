"""Queue ordering, pool caps, admission control and ETAs.

One queue, ordered by `store.queued_jobs()` (priority DESC, enqueued_at ASC).
`tick()` admits as many jobs as the caps allow and never waits on a job: each
start hands the work to a daemon thread and returns immediately.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from typing import Any, Optional, Sequence

from ajq import estimate, guard, paths, platform
from ajq.backends.base import ResourceBackend
from ajq.config import Config
from ajq.exec import JobRunner, RunResult
from ajq.store import Store

_OPTIONAL_FIELDS = ("memory_mb", "cpu_percent", "shell")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _git_root(cwd: str) -> str:
    """`git rev-parse --show-toplevel`, or "" when cwd is not in a work tree."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    root = done.stdout.strip() if done.returncode == 0 else ""
    return os.path.abspath(root) if root else ""


def _terminal_state(result: RunResult) -> str:
    kill = result.kill_reason
    if kill == "timeout":
        return "timeout"
    if kill == "canceled":
        return "canceled"
    if kill in {"output_limit", "memory_limit"}:
        return "failed"
    if result.exit_code == 0 and result.signal is None:
        return "done"
    return "failed"


def _write_meta(job: dict) -> str:
    """Best-effort `meta.json` sidecar (0o600) next to the job's out.log."""
    path = paths.job_meta_path(str(job.get("id") or ""))
    try:
        paths.ensure_dir(os.path.dirname(path))
        body = json.dumps(job, indent=2, sort_keys=True, default=str) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
    except (OSError, TypeError, ValueError):
        return ""
    return path


class Scheduler:
    """Owns the queue: submit -> enqueue, tick -> admit, on_finished -> record."""

    def __init__(
        self,
        store: Store,
        config: Config,
        runner: JobRunner,
        backend: ResourceBackend,
    ) -> None:
        self.store = store
        self.config = config
        self.runner = runner
        self.backend = backend
        self._running: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, tuple[str, str, str]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ submit
    def submit(
        self,
        *,
        argv: Sequence[str],
        cwd: str = ".",
        label: Optional[str] = None,
        kind: str = "auto",
        pool: str = "auto",
        timeout_s: Optional[int] = None,
        max_output_bytes: Optional[int] = None,
        priority: Optional[int] = None,
        serial_key: str = "auto",
        agent: Optional[str] = None,
        shell: bool = False,
        memory_mb: Optional[int] = None,
        cpu_percent: Optional[int] = None,
    ) -> dict:
        args = [str(item) for item in (argv or [])]
        if not args:
            raise ValueError("argv must not be empty")
        work_dir = os.path.abspath(os.path.expanduser(str(cwd or ".")))
        if not os.path.isdir(work_dir):
            raise ValueError(f"cwd is not a directory: {cwd}")

        verdict = guard.classify(args)
        tool = verdict["tool"]
        if kind in (None, "", "auto"):
            kind = verdict["kind"]
        kind = str(kind)
        root = _git_root(work_dir)
        if serial_key in (None, "", "auto"):
            serial = root or work_dir
        elif str(serial_key).lower() == "none":
            serial = ""
        else:
            serial = str(serial_key)

        files = estimate.changed_files(work_dir)
        est_seconds, est_source = estimate.estimate_seconds(self.store, kind, tool, work_dir, files)
        signature = estimate.signature_for(kind, tool, work_dir, files)
        if pool in (None, "", "auto"):
            pool = self._auto_pool(verdict["pool"], kind, est_seconds)
        pool = str(pool)

        job = self._add_job(
            self._fields(
                argv=args,
                cwd=work_dir,
                label=str(label) if label else f"{kind}:{tool.split(':')[0].split('/')[-1]}",
                kind=kind,
                pool=pool,
                serial_key=serial,
                priority=_as_int(priority, _as_int(self.config.get("defaults.priority", 0))),
                timeout_s=_as_int(timeout_s, _as_int(self.config.get("defaults.timeout_s", 1800))),
                max_output_bytes=_as_int(
                    max_output_bytes, _as_int(self.config.get("defaults.max_output_bytes", 8388608))
                ),
                agent=str(agent) if agent else None,
                worktree=os.path.basename(serial or work_dir) or work_dir,
                git_root=root,
                signature=signature,
                est_seconds=_as_float(est_seconds),
                est_source=str(est_source),
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                shell=bool(shell),
                tool=tool,
                kill_grace_s=_as_float(self.config.get("defaults.kill_grace_s", 10), 10.0),
            )
        )
        paths.ensure_dir(paths.job_dir(str(job["id"])))
        if not job.get("out_path"):
            job = self.store.update(job["id"], out_path=paths.job_out_path(str(job["id"]))) or job
        # in-memory mirror of what the jobs row now persists, so the estimate keys
        # survive a daemon restart even if this dict is rebuilt empty
        self._tasks[str(job["id"])] = (kind, tool, work_dir)
        _write_meta(job)
        return self.enrich(job)

    def _fields(self, **values: Any) -> dict:
        return {key: value for key, value in values.items() if value is not None or key == "shell"}

    def _add_job(self, fields: dict) -> dict:
        try:
            return self.store.add_job(**fields)
        except sqlite3.Error:  # store without the optional per-job override columns
            return self.store.add_job(**{k: v for k, v in fields.items() if k not in _OPTIONAL_FIELDS})

    def _auto_pool(self, base_pool: str, kind: str, est_seconds: float) -> str:
        """Guard pool, plus the cheap-unknown promotion the plan asks for."""
        if kind in guard.HEAVY_KINDS:
            return "heavy"
        threshold = _as_float(self.config.get("estimates.light_threshold_s", 30), 0.0)
        if threshold > 0 and kind in guard.LIGHT_KINDS | {"unknown"} and est_seconds < threshold:
            return "light"
        return base_pool

    # -------------------------------------------------------------------- tick
    def tick(self) -> list[str]:
        """Start every job that fits the caps right now; never blocks."""
        started: list[str] = []
        total_limit = _as_int(self.config.get("limits.max_concurrent", 8), 8)
        pools = self.config.get("limits.pools", {}) or {}
        running = self.store.running_jobs()
        counts: dict[str, int] = {}
        busy: set[str] = set()
        for job in running:
            tier = str(job.get("pool") or "normal")
            counts[tier] = counts.get(tier, 0) + 1
            key = str(job.get("serial_key") or "")
            if key:
                busy.add(key)
        free = _as_int(platform.available_memory_mb(), 0)
        headroom = _as_float(self.config.get("resources.memory_headroom", 1.2), 1.0)
        for job in self.store.queued_jobs():
            if len(running) + len(started) >= total_limit:
                break
            tier = str(job.get("pool") or "normal")
            cap = pools.get(tier)
            cap = _as_int(cap, total_limit) if isinstance(cap, (int, float)) else total_limit
            if counts.get(tier, 0) >= cap:
                continue
            key = str(job.get("serial_key") or "")
            if key and key in busy:
                continue
            memory = _as_int(job.get("memory_mb")) or _as_int(self.config.get("resources.memory_mb", 2048))
            if free < int(memory * headroom):
                continue
            if not self._start(job):
                continue
            started.append(str(job["id"]))
            counts[tier] = counts.get(tier, 0) + 1
            if key:
                busy.add(key)
            free -= max(0, memory)
        return started

    def _start(self, job: dict) -> bool:
        job_id = str(job["id"])
        cancel = threading.Event()
        with self._lock:
            if job_id in self._running:
                return False
        try:
            started = self.store.start(
                job_id, 0, str(getattr(self.backend, "name", "") or "")
            )  # pid filled in by _record_pid once the child exists
        except (KeyError, sqlite3.Error):
            return False
        if not started:
            return False
        row = dict(started)
        row["kill_grace_s"] = _as_float(
            row.get("kill_grace_s") or self.config.get("defaults.kill_grace_s", 10), 10.0
        )
        thread = threading.Thread(
            target=self._work, args=(row, cancel), name=f"ajq-job-{job_id}", daemon=True
        )
        with self._lock:
            self._running[job_id] = {"cancel": cancel, "thread": thread}
        before = self._runner_pid()
        thread.start()
        self._record_pid(job_id, before)
        return True

    def _runner_pid(self) -> int:
        for attribute in ("last_pid", "pid", "current_pid"):
            value = getattr(self.runner, attribute, None)
            if callable(value):
                continue
            pid = _as_int(value)
            if pid > 0:
                return pid
        return 0

    def _record_pid(self, job_id: str, previous: int, timeout: float = 1.0) -> None:
        """Attribute the spawned pid to this job once the runner reports it.

        The runner exposes a single last_pid, so wait for it to change instead of
        reading it later from the tick loop: otherwise a second job starting
        could overwrite the first job's recorded pid.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid = self._runner_pid()
            if pid and pid != previous:
                try:
                    self.store.update(job_id, pid=pid)
                except sqlite3.Error:
                    pass
                return
            time.sleep(0.02)

    def _work(self, job: dict, cancel: threading.Event) -> None:
        job_id = str(job["id"])
        try:
            result = self.runner.run(job, cancel, self._out_bytes(job_id))
        except Exception:
            result = RunResult(
                exit_code=None,
                signal=None,
                kill_reason="lost",
                out_bytes=0,
                truncated=False,
                elapsed_s=0.0,
            )
        with self._lock:
            self._running.pop(job_id, None)
        try:
            self.on_finished(job, result)
        except Exception:
            return

    def _out_bytes(self, job_id: str):
        def add(delta: int) -> None:
            try:
                self.store.add_out_bytes(job_id, _as_int(delta))
            except sqlite3.Error:
                pass

        return add

    # -------------------------------------------------------------- completion
    def on_finished(self, job: dict, result: RunResult) -> dict:
        """Persist the terminal state, feed the estimate, refresh meta.json."""
        job_id = str(job["id"])
        state = _terminal_state(result)
        finished = self.store.finish(
            job_id,
            state,
            exit_code=result.exit_code,
            signal=result.signal,
            kill_reason=result.kill_reason,
            out_bytes=result.out_bytes,
            truncated=bool(result.truncated),
            ended_at=time.time(),
        )
        row = finished or dict(job)
        elapsed = _as_float(getattr(result, "elapsed_s", 0.0))
        if result.kill_reason is None and elapsed > 0 and bool(self.config.get("estimates.enabled", True)):
            kind, tool, cwd = self._tasks.pop(
                job_id,
                (str(row.get("kind") or "unknown"), str(row.get("tool") or "unknown"), str(row.get("cwd") or ".")),
            )
            try:
                estimate.record(self.store, kind, tool, cwd, elapsed)
            except Exception:
                pass
        _write_meta(row)
        return self.enrich(row)

    def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        state = str(job.get("state") or "")
        if state == "queued":
            job = (
                self.store.finish(job_id, "canceled", kill_reason="canceled", ended_at=time.time()) or job
            )
            self._tasks.pop(job_id, None)
            _write_meta(job)
            return self.enrich(job)
        if state == "running":
            with self._lock:
                entry = self._running.get(job_id)
            if entry is not None:
                entry["cancel"].set()
            else:
                # No local runner owns this job: a stale daemon (or one from
                # before the singleton lock) started it. Kill the process group
                # recorded in the shared DB so the job still dies; its owner
                # records the terminal state when the child exits.
                self._kill_recorded(job)
        return self.enrich(self.store.get(job_id) or job)

    def _kill_recorded(self, job: dict) -> None:
        """SIGTERM then SIGKILL the process group a job recorded in the DB."""
        pid = _as_int(job.get("pid"))
        if pid <= 1:
            return
        try:
            group = os.getpgid(pid)
        except OSError:
            return
        if group <= 1 or group == os.getpgid(0):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(group, 0)
                except OSError:
                    return
                time.sleep(0.05)

    # ------------------------------------------------------------- enrichment
    def enrich(self, job: dict) -> dict:
        """Add the derived metadata keys; safe on any job dict, never raises."""
        try:
            return self._enrich(dict(job))
        except Exception:
            return dict(job)

    def _enrich(self, job: dict) -> dict:
        now = time.time()
        anchor = _as_float(job.get("started_at"), 0.0) or _as_float(job.get("enqueued_at"), now)
        ended = _as_float(job.get("ended_at"), 0.0)
        if ended:
            # a finished job's elapsed is its runtime, not time since it ended
            job["elapsed_s"] = round(max(0.0, ended - anchor), 1)
        else:
            job["elapsed_s"] = round(max(0.0, now - anchor), 1)
        job["est_source"] = str(job.get("est_source") or "none")
        job["est_seconds"] = _as_float(job.get("est_seconds"))
        position = self.compute_position(job)
        job["queue_position"] = position
        job["eta_start_s"] = self._eta_start(job, position) if position else 0.0
        job["eta_run_s"] = job["est_seconds"]
        job["eta_total_s"] = round(job["eta_start_s"] + job["eta_run_s"], 1)
        return job

    def _eta_start(self, job: dict, position: int) -> float:
        tier = str(job.get("pool") or "normal")
        ahead = 0.0
        for other in self.store.queued_jobs()[: max(position - 1, 0)]:
            if str(other.get("pool") or "normal") == tier:
                ahead += _as_float(other.get("est_seconds"))
        now = time.time()
        for other in self.store.running_jobs():
            if str(other.get("pool") or "normal") != tier:
                continue
            began = _as_float(other.get("started_at"), 0.0) or _as_float(other.get("enqueued_at"), now)
            ahead += max(0.0, _as_float(other.get("est_seconds")) - (now - began))
        return round(max(0.0, ahead), 1)

    def compute_position(self, job: dict) -> Optional[int]:
        """1-based index in `store.queued_jobs()`, None when not queued."""
        if str(job.get("state") or "") != "queued":
            return None
        job_id = str(job.get("id") or "")
        for index, queued in enumerate(self.store.queued_jobs(), start=1):
            if str(queued.get("id")) == job_id:
                return index
        return None

    # ---------------------------------------------------------------- waiting
    def running_ids(self) -> list[str]:
        with self._lock:
            return list(self._running)

    def wait_idle(self, timeout: float) -> bool:
        """Join worker threads until the queue drains or `timeout` expires."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                threads = [entry["thread"] for entry in self._running.values()]
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for thread in threads:
                thread.join(timeout=max(0.0, min(0.2, remaining)))