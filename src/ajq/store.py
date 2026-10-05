"""SQLite persistence for ajq: jobs, duration estimates, meta.

One connection per Store guarded by a threading.RLock, WAL journalling and a 5s
busy timeout, so the daemon's thread-per-connection server and its scheduler
thread can share a single Store. Every read helper returns job dicts carrying
all of JOB_FIELDS, so callers never KeyError on a partial row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Sequence

from . import paths

SCHEMA_VERSION = 1

STATES: tuple[str, ...] = (
    "queued",
    "running",
    "done",
    "failed",
    "timeout",
    "canceled",
    "lost",
)
TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "failed", "timeout", "canceled", "lost"}
)

JOB_FIELDS: tuple[str, ...] = (
    "id",
    "label",
    "argv",
    "cwd",
    "kind",
    "pool",
    "serial_key",
    "state",
    "priority",
    "agent",
    "enqueued_at",
    "started_at",
    "ended_at",
    "exit_code",
    "signal",
    "kill_reason",
    "timeout_s",
    "max_output_bytes",
    "out_path",
    "out_bytes",
    "truncated",
    "queue_position",
    "elapsed_s",
    "eta_start_s",
    "eta_run_s",
    "eta_total_s",
    "est_source",
    "backend",
    "pid",
    "worktree",
    "git_root",
    "signature",
    "est_seconds",
    "tool",
    "memory_mb",
    "cpu_percent",
    "kill_grace_s",
    "shell",
)

_TEXT_FIELDS = frozenset(
    {
        "label",
        "cwd",
        "kind",
        "pool",
        "serial_key",
        "state",
        "agent",
        "kill_reason",
        "out_path",
        "est_source",
        "backend",
        "worktree",
        "git_root",
        "signature",
        "tool",
    }
)
_INT_FIELDS = frozenset(
    {
        "priority",
        "exit_code",
        "signal",
        "max_output_bytes",
        "out_bytes",
        "pid",
        "memory_mb",
        "cpu_percent",
    }
)
_REAL_FIELDS = frozenset(
    {"enqueued_at", "started_at", "ended_at", "timeout_s", "est_seconds", "kill_grace_s"}
)
_BOOL_FIELDS = frozenset({"truncated", "shell"})
# Stored as INTEGER columns, but read back as null until the job actually ends,
# so `exit_code: 0` can never be mistaken for "the job succeeded".
_NULLABLE_FIELDS = frozenset({"exit_code", "signal", "pid"})
_JSON_FIELDS = frozenset({"argv"})
_DERIVED_FIELDS = frozenset(
    {"queue_position", "elapsed_s", "eta_start_s", "eta_run_s", "eta_total_s"}
)


def _zero_value(field: str):
    if field in _JSON_FIELDS:
        return []
    if field in _NULLABLE_FIELDS:
        # Not "0": a queued job reporting exit_code 0 reads as "it passed".
        return None
    if field in _BOOL_FIELDS:
        return False
    if field in _INT_FIELDS:
        return 0
    if field in _REAL_FIELDS:
        return 0.0
    if field in _TEXT_FIELDS:
        return ""
    return None


def blank_job() -> dict:
    """A job dict with every JOB_FIELDS key at its zero value."""
    return {field: _zero_value(field) for field in JOB_FIELDS}


def new_job_id() -> str:
    return "j-" + uuid.uuid4().hex[:12]


def _sql_type(field: str) -> str:
    if field in _BOOL_FIELDS:
        return "INTEGER"
    if field in _INT_FIELDS:
        return "INTEGER"
    if field in _REAL_FIELDS:
        return "REAL"
    return "TEXT"


_JOBS_DDL = (
    "CREATE TABLE IF NOT EXISTS jobs (\n"
    + ",\n".join(f"  {field} {_sql_type(field)}" for field in JOB_FIELDS)
    + ",\n  PRIMARY KEY (id)\n)"
)
_ESTIMATES_DDL = (
    "CREATE TABLE IF NOT EXISTS estimates (\n"
    "  signature TEXT PRIMARY KEY,\n"
    "  n INTEGER NOT NULL DEFAULT 0,\n"
    "  mean REAL NOT NULL DEFAULT 0.0,\n"
    "  m2 REAL NOT NULL DEFAULT 0.0,\n"
    "  kind TEXT NOT NULL DEFAULT '',\n"
    "  tool TEXT NOT NULL DEFAULT '',\n"
    "  last_at REAL NOT NULL DEFAULT 0.0\n)"
)
_META_DDL = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)",
    "CREATE INDEX IF NOT EXISTS jobs_enqueue ON jobs(enqueued_at)",
    "CREATE INDEX IF NOT EXISTS jobs_signature ON jobs(signature)",
    "CREATE INDEX IF NOT EXISTS jobs_pool ON jobs(pool)",
)


def _encode(value):
    """Normalise a Python value for an INTEGER/REAL column."""
    if isinstance(value, bool):
        return int(value)
    return value


def _decode_argv(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [str(value)]
    return [str(item) for item in parsed] if isinstance(parsed, list) else [str(value)]


def _encode_argv(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        value = parsed if isinstance(parsed, list) else [value]
    if not isinstance(value, (list, tuple)):
        value = [str(value)]
    return json.dumps([str(item) for item in value])


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            paths.ensure_dir(parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_JOBS_DDL)
        self._conn.executescript(_ESTIMATES_DDL)
        self._conn.executescript(_META_DDL)
        self._add_missing_columns()
        for statement in _INDEX_DDL:
            self._conn.execute(statement)
        self._conn.commit()
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _add_missing_columns(self) -> None:
        """ALTER TABLE ADD COLUMN for fields added after a DB was first created.

        CREATE TABLE IF NOT EXISTS silently keeps an old column set, so without
        this an upgraded daemon would fail every INSERT that sets a new field.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for field in JOB_FIELDS:
            if field in existing or field == "id":
                continue
            self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {field} {_sql_type(field)}")

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def schema_version(self) -> int:
        with self._lock:
            raw = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        try:
            return int(raw["value"]) if raw else 0
        except (TypeError, ValueError):
            return 0

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            with self._conn:
                cur = self._conn.execute(sql, params)
                return cur.rowcount

    def _row(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

    def _row_to_job(self, row: sqlite3.Row) -> dict:
        job = blank_job()
        keys = row.keys()
        for field in JOB_FIELDS:
            if field not in keys:
                continue
            value = row[field]
            if value is None:
                continue
            if field in _JSON_FIELDS:
                job[field] = _decode_argv(value)
            elif field in _BOOL_FIELDS:
                job[field] = bool(value)
            else:
                job[field] = value
        return job

    def _assign(self, job_id: str, fields: dict) -> None:
        if not fields:
            return
        columns = []
        params: list = []
        for field, value in fields.items():
            if field not in JOB_FIELDS or field == "id":
                continue
            if field in _JSON_FIELDS:
                value = _encode_argv(value)
            elif field in _BOOL_FIELDS:
                value = int(bool(value))
            elif field in _INT_FIELDS or field in _REAL_FIELDS:
                value = _encode(value)
            columns.append(f"{field} = ?")
            params.append(value)
        if not columns:
            return
        params.append(job_id)
        self._write(f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?", tuple(params))

    def _fetch_jobs(
        self, where: str = "", params: tuple = (), order: str = "", limit: int | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + where
        if order:
            sql += " ORDER BY " + order
        if limit is not None and limit >= 0:
            sql += " LIMIT ?"
            params = tuple(params) + (int(limit),)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_job(row) for row in rows]

    # -- jobs -------------------------------------------------------------
    def add_job(self, **fields) -> dict:
        with self._lock:
            job = blank_job()
            job.update({k: v for k, v in fields.items() if k in JOB_FIELDS})
            job["id"] = str(fields.get("id") or new_job_id())
            job["state"] = "queued"
            job["enqueued_at"] = float(fields.get("enqueued_at") or time.time())
            job["argv"] = _decode_argv(fields.get("argv"))
            if not job["out_path"]:
                job["out_path"] = paths.job_out_path(job["id"])
            if job["worktree"] is None:
                job["worktree"] = ""
            if job["git_root"] is None:
                job["git_root"] = ""
            columns = ["id"] + [f for f in JOB_FIELDS if f != "id"]
            values = []
            for field in columns:
                value = job[field]
                if field in _JSON_FIELDS:
                    values.append(_encode_argv(value))
                elif field in _BOOL_FIELDS:
                    values.append(int(bool(value)))
                elif field in _INT_FIELDS or field in _REAL_FIELDS:
                    values.append(_encode(value) if value is not None else 0)
                else:
                    values.append("" if value is None else str(value))
            placeholders = ",".join("?" for _ in columns)
            self._write(
                f"INSERT OR REPLACE INTO jobs({','.join(columns)}) VALUES({placeholders})",
                tuple(values),
            )
            return job

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._row(job_id)
            return self._row_to_job(row) if row is not None else None

    def list_jobs(self, states: Sequence[str] | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            if states:
                wanted = [s for s in states if s]
                if not wanted:
                    return []
                marks = ",".join("?" for _ in wanted)
                return self._fetch_jobs(
                    where=f"state IN ({marks})",
                    params=tuple(wanted),
                    order="enqueued_at DESC, id DESC",
                    limit=limit,
                )
            return self._fetch_jobs(order="enqueued_at DESC, id DESC", limit=limit)

    def queued_jobs(self) -> list[dict]:
        with self._lock:
            return self._fetch_jobs(
                where="state = 'queued'", order="priority DESC, enqueued_at ASC, id ASC"
            )

    def running_jobs(self) -> list[dict]:
        with self._lock:
            return self._fetch_jobs(where="state = 'running'", order="started_at ASC, id ASC")

    def count_by_state(self) -> dict[str, int]:
        with self._lock:
            counts = {state: 0 for state in STATES}
            for row in self._conn.execute(
                "SELECT state, COUNT(*) AS total FROM jobs GROUP BY state"
            ):
                counts[row["state"]] = int(row["total"])
            return counts

    def update(self, job_id: str, **fields) -> dict | None:
        with self._lock:
            if self._row(job_id) is None:
                return None
            self._assign(job_id, fields)
            row = self._row(job_id)
            return self._row_to_job(row) if row is not None else None

    def start(
        self, job_id: str, pid: int, backend: str, started_at: float | None = None
    ) -> dict:
        with self._lock:
            self._assign(
                job_id,
                {
                    "state": "running",
                    "pid": int(pid),
                    "backend": backend or "",
                    "started_at": time.time() if started_at is None else float(started_at),
                    "ended_at": 0.0,
                    "exit_code": None,
                    "signal": None,
                    "kill_reason": "",
                },
            )
            row = self._row(job_id)
            if row is None:
                raise KeyError(job_id)
            return self._row_to_job(row)

    def finish(
        self,
        job_id: str,
        state: str,
        *,
        exit_code: int | None = None,
        signal: int | None = None,
        kill_reason: str | None = None,
        out_bytes: int | None = None,
        truncated: bool | None = None,
        ended_at: float | None = None,
    ) -> dict:
        with self._lock:
            if state not in STATES:
                raise ValueError(f"unknown state: {state}")
            fields: dict = {
                "state": state,
                "ended_at": time.time() if ended_at is None else float(ended_at),
            }
            if exit_code is not None:
                fields["exit_code"] = int(exit_code)
            if signal is not None:
                fields["signal"] = int(signal)
            if kill_reason is not None:
                fields["kill_reason"] = kill_reason
            if out_bytes is not None:
                fields["out_bytes"] = int(out_bytes)
            if truncated is not None:
                fields["truncated"] = bool(truncated)
            self._assign(job_id, fields)
            row = self._row(job_id)
            if row is None:
                raise KeyError(job_id)
            return self._row_to_job(row)

    def add_out_bytes(self, job_id: str, delta: int) -> None:
        with self._lock:
            self._write(
                "UPDATE jobs SET out_bytes = MAX(0, COALESCE(out_bytes, 0) + ?)"
                " WHERE id = ?",
                (int(delta), job_id),
            )

    def recover_orphans(self) -> int:
        """Mark every `running` row as `lost`; returns the number of rows changed."""
        with self._lock:
            return self._write(
                "UPDATE jobs SET state = 'lost', kill_reason = 'daemon_restart',"
                " ended_at = ? WHERE state = 'running'",
                (time.time(),),
            )

    # -- estimates --------------------------------------------------------
    def get_estimate(self, signature: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM estimates WHERE signature = ?", (signature,)
            ).fetchone()
            return dict(row) if row is not None else None

    def record_duration(
        self, signature: str, kind: str, tool: str, seconds: float
    ) -> dict:
        with self._lock:
            row = self.get_estimate(signature)
            n = int(row["n"]) if row else 0
            mean = float(row["mean"]) if row else 0.0
            m2 = float(row["m2"]) if row else 0.0
            sample = max(0.0, float(seconds))
            n += 1
            delta = sample - mean
            mean += delta / n
            m2 += delta * (sample - mean)
            last_at = time.time()
            self._write(
                "INSERT INTO estimates(signature, n, mean, m2, kind, tool, last_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(signature) DO UPDATE SET n = excluded.n, mean = excluded.mean,"
                " m2 = excluded.m2, kind = excluded.kind, tool = excluded.tool,"
                " last_at = excluded.last_at",
                (signature, n, mean, m2, kind or "", tool or "", last_at),
            )
            return {
                "signature": signature,
                "n": n,
                "mean": mean,
                "m2": m2,
                "kind": kind or "",
                "tool": tool or "",
                "last_at": last_at,
            }

    def list_estimates(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM estimates ORDER BY last_at DESC, signature ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_estimates(self) -> int:
        with self._lock:
            return self._write("DELETE FROM estimates")

    def estimate_rows(self, limit: int = 500) -> list[dict]:
        """Finished jobs that carry both a signature and an estimate."""
        marks = ",".join("?" for _ in TERMINAL_STATES)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, state, pool, signature, kind, est_source, est_seconds,"
                " started_at, ended_at,"
                " COALESCE(ended_at - started_at, 0.0) AS actual_s"
                f" FROM jobs WHERE state IN ({marks}) AND COALESCE(signature, '') <> ''"
                " AND COALESCE(est_seconds, 0.0) > 0.0"
                " AND started_at > 0.0 AND ended_at > started_at"
                " ORDER BY ended_at DESC LIMIT ?",
                tuple(TERMINAL_STATES) + (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    # -- housekeeping -----------------------------------------------------
    def prune(self, keep_days: int = 14) -> int:
        """Drop terminal job rows older than keep_days; output files are untouched."""
        cutoff = time.time() - max(0, int(keep_days)) * 86400.0
        marks = ",".join("?" for _ in TERMINAL_STATES)
        with self._lock:
            return self._write(
                f"DELETE FROM jobs WHERE state IN ({marks})"
                " AND COALESCE(ended_at, 0.0) > 0.0 AND ended_at < ?",
                tuple(TERMINAL_STATES) + (cutoff,),
            )