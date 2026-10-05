"""Filesystem locations for ajq.

Environment overrides (used by tests and by the installer):
  AJQ_STATE_DIR   state root, default $XDG_STATE_HOME/ajq or ~/.local/state/ajq
  AJQ_CONFIG      config file,  default $XDG_CONFIG_HOME/ajq/config.json or
                                 ~/.config/ajq/config.json
  AJQ_SOCKET      socket path, default $XDG_RUNTIME_DIR/ajq/ajqd.sock, falling
                  back to $TMPDIR/ajq-<uid>/ajqd.sock (macOS has no XDG_RUNTIME_DIR)
"""

from __future__ import annotations

import os

APP = "ajq"


def _home() -> str:
    return os.path.expanduser("~")


def _state_home() -> str:
    value = os.environ.get("XDG_STATE_HOME") or ""
    return value if value else os.path.join(_home(), ".local", "state")


def _config_home() -> str:
    value = os.environ.get("XDG_CONFIG_HOME") or ""
    return value if value else os.path.join(_home(), ".config")


def _runtime_dir() -> str:
    """Socket directory: XDG_RUNTIME_DIR when present, else a private TMPDIR dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, APP)
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp.rstrip("/"), f"{APP}-{os.getuid()}")


STATE_DIR = os.environ.get("AJQ_STATE_DIR") or os.path.join(_state_home(), APP)
CONFIG_PATH = os.environ.get("AJQ_CONFIG") or os.path.join(_config_home(), APP, "config.json")
SOCKET_PATH = os.environ.get("AJQ_SOCKET") or os.path.join(_runtime_dir(), "ajqd.sock")
DB_PATH = os.path.join(STATE_DIR, "state.db")


def jobs_dir() -> str:
    return os.path.join(STATE_DIR, "jobs")


def job_dir(job_id: str) -> str:
    return os.path.join(jobs_dir(), job_id)


def job_out_path(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "out.log")


def job_meta_path(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "meta.json")


def ensure_dir(path: str, mode: int = 0o700) -> str:
    """Create `path` (and parents) if needed and return it."""
    os.makedirs(path, mode=mode, exist_ok=True)
    return path