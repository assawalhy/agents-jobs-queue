"""The ajqd daemon: socket server, op dispatch, process lifecycle.

`serve()` runs in the foreground: a thread-per-connection server
(`protocol.serve`) plus one daemon thread driving `scheduler.tick()` every
`daemon.tick_s` seconds.  The socket answers one JSON request per connection
(`ping submit status list cancel wait stats estimates_clear shutdown`).

Everything the daemon writes lives under `paths.STATE_DIR` (0700): `state.db`,
`ajqd.pid`, `ajqd.log` and `jobs/<id>/{out.log,meta.json}`.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

from . import __version__, estimate, paths, platform, protocol
from .backends import get_backend
from .config import Config, load_config
from .exec import JobRunner
from .scheduler import Scheduler
from .store import TERMINAL_STATES, Store

UNIT_FALLBACK = "ajqd.service"
PLIST_LABEL = "io.ajq.ajqd"
LAUNCH_AGENTS = os.path.join("Library", "LaunchAgents", PLIST_LABEL + ".plist")

_SHELL = "/bin/sh"
_ACCEPT_TIMEOUT_S = 0.5
_CONN_TIMEOUT_S = 3600.0
_SOCKET_POLL_S = 0.1
_SOCKET_PING_S = 1.0
_PRUNE_EVERY_S = 3600.0

_SOCKET_OVERRIDE: Optional[str] = None


def _log(message: str) -> None:
    sys.stderr.write(f"ajqd: {message}\n")
    try:
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# -- paths ----------------------------------------------------------------


def pid_path() -> str:
    return os.path.join(paths.STATE_DIR, "ajqd.pid")


def log_path() -> str:
    return os.path.join(paths.STATE_DIR, "ajqd.log")


def set_socket_override(path: str | None) -> None:
    """Pin the socket for this process (used by the CLI's `--sock`)."""
    global _SOCKET_OVERRIDE
    _SOCKET_OVERRIDE = os.path.abspath(os.path.expanduser(path)) if path else None


def read_pid() -> int | None:
    try:
        with open(pid_path(), encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_grace_default() -> float:
    try:
        return float(load_config().get("defaults.kill_grace_s", 10) or 10)
    except Exception:
        return 10.0


def resolve_socket_path(socket_path: str | None = None, config: Any = None) -> str:
    """Public resolver: explicit argument > --sock override > `daemon.socket` > $AJQ_SOCKET."""
    """Explicit argument > CLI override > `daemon.socket` > `paths.SOCKET_PATH`."""
    if socket_path:
        return os.path.abspath(os.path.expanduser(socket_path))
    if _SOCKET_OVERRIDE:
        return _SOCKET_OVERRIDE
    configured = ""
    try:
        if config is None:
            config = load_config()
        configured = config.get("daemon.socket") or ""
    except Exception:
        configured = ""
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    return paths.SOCKET_PATH


def probe(argv: Sequence[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a status command and return (returncode, stdout); never raises."""
    return _run(argv, timeout)


def detect_backend(name: str | None = None) -> str:
    """The backend name this platform would use for `name` (never raises)."""
    try:
        config = load_config()
        wanted = name or str(config.get("resources.backend") or "auto")
        return str(getattr(get_backend(wanted), "name", wanted))
    except Exception:
        return str(name or "auto")


def _run(argv: Sequence[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        out = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return out.returncode, (out.stdout or "").strip()


# -- lifecycle ------------------------------------------------------------


def protocol_ready(socket_path: str | None = None) -> bool:
    """True when an ajqd answers `ping` on this exact socket path."""
    try:
        response = protocol.request(
            resolve_socket_path(socket_path), {"op": "ping"}, timeout=_SOCKET_PING_S
        )
    except Exception:
        return False
    return bool(response.get("ok"))


def is_running(socket_path: str | None = None) -> bool:
    return protocol_ready(socket_path)


def _wait_for_socket(path: str, deadline: float) -> bool:
    while True:
        if is_running(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_SOCKET_POLL_S)


def _unit_name() -> str:
    try:
        config = load_config()
        return str(config.get("daemon.unit") or UNIT_FALLBACK)
    except Exception:
        return UNIT_FALLBACK


def _systemctl_start(unit: str) -> bool:
    code, _ = _run(["systemctl", "--user", "start", unit], timeout=5.0)
    return code == 0


def launchctl_plist() -> str:
    return os.path.expanduser(os.path.join("~", LAUNCH_AGENTS))


def _launchctl_bootstrap() -> bool:
    plist = launchctl_plist()
    if not os.path.isfile(plist):
        return False
    code, _ = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], timeout=5.0)
    return code == 0


def _child_env() -> dict:
    env = dict(os.environ)
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if package_parent and package_parent not in parts:
        parts.insert(0, package_parent)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def has_main_module() -> bool:
    """`python -m ajq` works only when ajq/__main__.py exists."""
    return os.path.isfile(os.path.join(os.path.dirname(__file__), "__main__.py"))


def serve_argv() -> list[str]:
    """The command a detached daemon runs: `python -m ajq daemon serve`."""
    if has_main_module():
        return [platform.interpreter_path(), "-m", "ajq", "daemon", "serve"]
    # No __main__.py (a source checkout before the entry point lands): call the
    # same daemon_entry directly, pinning this package's parent on sys.path so
    # the child works even without PYTHONPATH.
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        f"import sys;sys.path.insert(0,{parent!r});"
        "from ajq.daemon import daemon_entry;"
        "sys.exit(daemon_entry(['serve']))"
    )
    return [platform.interpreter_path(), "-c", code]


def _spawn(argv: Sequence[str]) -> bool:
    """Double-fork `argv`, detached, logging to `ajqd.log`."""
    paths.ensure_dir(paths.STATE_DIR)
    argv = [str(item) for item in argv]
    try:
        handle = open(log_path(), "ab", 0)
    except OSError as exc:
        _log(f"cannot open {log_path()}: {exc}")
        return False
    try:
        env = _child_env()
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                if os.fork() > 0:
                    os._exit(0)
                os.chdir("/")
                devnull = os.open(os.devnull, os.O_RDONLY)
                os.dup2(devnull, 0)
                os.dup2(handle.fileno(), 1)
                os.dup2(handle.fileno(), 2)
                if devnull > 2:
                    os.close(devnull)
                os.execve(argv[0], argv, env)
            except BaseException:  # noqa: BLE001 - the child must never unwind
                os._exit(127)
        os.waitpid(pid, 0)
        return True
    except OSError as exc:
        _log(f"detached spawn failed: {exc}")
        return False
    finally:
        try:
            handle.close()
        except OSError:
            pass


def spawn_detached() -> bool:
    """Start the daemon detached; True when the forked process was launched."""
    return _spawn(serve_argv())


def ensure_running(timeout: float = 5.0) -> bool:
    """Bring the daemon up however this platform allows; True once it pings."""
    path = resolve_socket_path()
    if is_running(path):
        return True
    if _autostart_disabled():
        return False
    deadline = time.monotonic() + max(0.5, float(timeout))
    if platform.IS_LINUX and _systemctl_start(_unit_name()):
        if _wait_for_socket(path, deadline):
            return True
    if platform.IS_MACOS and _launchctl_bootstrap():
        if _wait_for_socket(path, deadline):
            return True
    if spawn_detached() and _wait_for_socket(path, deadline):
        return True
    return False


def _autostart_disabled() -> bool:
    """AJQ_NO_AUTOSTART=1 keeps `ensure` from starting anything.

    Set by the test suite and useful in CI or on a machine where a stray daemon
    must never appear; an already-running daemon is still reported as up.
    """
    return (os.environ.get("AJQ_NO_AUTOSTART") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _stopped(path: str) -> bool:
    """The daemon is gone: the socket neither answers nor exists."""
    return not os.path.exists(path) and not is_running(path)


def stop_running(timeout: float = 5.0) -> bool:
    """Ask the daemon to shut down; True once the socket is gone."""
    path = resolve_socket_path()
    if _stopped(path):
        return True
    try:
        protocol.request(path, {"op": "shutdown"}, timeout=2.0)
    except Exception as exc:
        _log(f"shutdown request failed: {exc}")
    deadline = time.monotonic() + max(0.5, float(timeout))
    while time.monotonic() < deadline:
        if _stopped(path):
            return True
        time.sleep(_SOCKET_POLL_S)
    pid = read_pid()
    if pid and _pid_alive(pid):
        _log(f"pid {pid} ignored shutdown after {timeout:g}s; sending SIGTERM")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            _log(f"SIGTERM failed: {exc}")
        hard = time.monotonic() + max(2.0, _kill_grace_default())
        while time.monotonic() < hard:
            if _stopped(path):
                return True
            time.sleep(_SOCKET_POLL_S)
    return _stopped(path)


# -- request helpers ------------------------------------------------------


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _state_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [str(value).strip()]
    states = [part for part in parts if part]
    return states or None


def _argv_of(payload: dict) -> list[str]:
    argv = payload.get("argv")
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if isinstance(argv, (list, tuple)) and argv:
        return [str(item) for item in argv]
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    return []


def _as_shell_argv(argv: Sequence[str]) -> list[str]:
    """`--shell` means `sh -c "<joined argv>"`; argv is otherwise never joined."""
    return [_SHELL, "-c", " ".join(str(item) for item in argv)]


def _job_id_of(payload: dict) -> str:
    for key in ("id", "job_id", "job"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


# -- the server -----------------------------------------------------------


class _Server:
    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = resolve_socket_path(socket_path)
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.listener: Optional[socket.socket] = None
        self.config: Optional[Config] = None
        self.store: Optional[Store] = None
        self.backend: Any = None
        self.runner: Optional[JobRunner] = None
        self.scheduler: Optional[Scheduler] = None
        self.backend_name = "unknown"
        self.recovered = 0
        self._conn_lock = threading.Lock()
        self._conn_threads: set[threading.Thread] = set()
        self._ops: dict[str, Callable[[dict], dict]] = {
            "ping": self._op_ping,
            "submit": self._op_submit,
            "status": self._op_status,
            "list": self._op_list,
            "cancel": self._op_cancel,
            "wait": self._op_wait,
            "stats": self._op_stats,
            "estimates_clear": self._op_estimates_clear,
            "shutdown": self._op_shutdown,
        }

    # -- startup / teardown ----------------------------------------------
    def _kill_grace_s(self) -> float:
        try:
            grace = float(self.config.get("defaults.kill_grace_s", 10) or 10)
        except (TypeError, ValueError):
            grace = 10.0
        return max(0.0, grace)

    def startup(self) -> None:
        paths.ensure_dir(paths.STATE_DIR)
        parent = os.path.dirname(self.socket_path) or "."
        paths.ensure_dir(parent)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        # Check before unlinking: unlinking first would pull the socket out from
        # under the live daemon and let a second one bind the same path.
        if self._another_instance_live():
            raise RuntimeError(
                f"another ajqd is already listening on {self.socket_path} "
                f"(pid {read_pid()}); run `ajq daemon status`"
            )
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.store = Store(paths.DB_PATH)
        self.recovered = self.store.recover_orphans()
        self._recover_meta()
        self._write_pidfile()
        self.config = load_config()
        wanted = str(self.config.get("resources.backend") or "auto")
        self.backend = get_backend(wanted)
        self.backend_name = str(getattr(self.backend, "name", wanted))
        self.runner = JobRunner(self.backend)
        self.scheduler = Scheduler(self.store, self.config, self.runner, self.backend)
        self._bind()
        self._install_signals()
        _log(
            f"listening on {self.socket_path} pid={os.getpid()} backend={self.backend_name}"
            + (f" recovered={self.recovered}" if self.recovered else "")
        )

    def _another_instance_live(self) -> bool:
        """Refuse to clobber a daemon that is already answering on this socket."""
        return protocol_ready(self.socket_path)

    def _bind(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        listener.listen(128)
        listener.settimeout(_ACCEPT_TIMEOUT_S)
        self.listener = listener

    def _write_pidfile(self) -> None:
        try:
            handle = os.open(pid_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()}\n")
        except OSError as exc:
            _log(f"cannot write {pid_path()}: {exc}")

    def _install_signals(self) -> None:
        def _handler(_signum: int, _frame: Any) -> None:
            self.stop_event.set()

        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def _recover_meta(self) -> None:
        """recover_orphans() has no scheduler thread to write meta.json, so do it here."""
        try:
            orphans = self.store.list_jobs(states=["lost"], limit=200)
        except Exception:
            return
        for job in orphans:
            self._write_meta(job["id"])

    def run(self) -> int:
        ticker = threading.Thread(target=self.tick_loop, name="ajqd-tick", daemon=True)
        ticker.start()
        return self.accept_loop()

    def accept_loop(self) -> int:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, InterruptedError):
                continue
            except OSError as exc:
                if self.stop_event.is_set():
                    break
                _log(f"accept failed: {exc}")
                time.sleep(0.1)
                continue
            thread = threading.Thread(target=self._serve_conn, args=(conn,), daemon=True)
            with self._conn_lock:
                self._conn_threads.add(thread)
            thread.start()
        return self.teardown()

    def _serve_conn(self, conn: socket.socket) -> None:
        current = threading.current_thread()
        try:
            conn.settimeout(_CONN_TIMEOUT_S)
            protocol.serve(conn, self.dispatch)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._conn_lock:
                self._conn_threads.discard(current)

    def teardown(self) -> int:
        self.stop_event.set()
        self._close_listener()
        grace = self._kill_grace_s()
        running = self._running_ids()
        if running:
            _log(f"stopping: {len(running)} running job(s), grace {grace:g}s")
            self.scheduler.wait_idle(grace)
        leftover = self._running_ids()
        if leftover:
            self._terminate_jobs(leftover)
        self._join_conns(2.0)
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
        _remove(self.socket_path, "socket")
        # Only clear the pidfile if it is still ours: a restart that raced us
        # may already have written a new one.
        if read_pid() == os.getpid():
            _remove(pid_path(), "pidfile")
        _log("stopped")
        return 0

    def _close_listener(self) -> None:
        listener, self.listener = self.listener, None
        if listener is None:
            return
        try:
            listener.close()
        except OSError:
            pass

    def _join_conns(self, timeout: float) -> None:
        with self._conn_lock:
            threads = list(self._conn_threads)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _running_ids(self) -> list[str]:
        try:
            return list(self.scheduler.running_ids())
        except Exception:
            return []

    def _terminate_jobs(self, job_ids: Sequence[str]) -> None:
        own_group = os.getpgid(0)
        for job_id in job_ids:
            job = self.store.get(job_id) if self.store is not None else None
            pid = int((job or {}).get("pid") or 0)
            if pid <= 1:
                continue
            try:
                group = os.getpgid(pid)
            except OSError:
                continue
            if group == own_group or group <= 1:
                continue
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(group, sig)
                except OSError:
                    break
                if sig is signal.SIGTERM:
                    time.sleep(1.0)
            _log(f"killed orphaned pid {pid} for {job_id}")

    # -- scheduler thread -------------------------------------------------
    def tick_loop(self) -> None:
        tick_s = 0.5
        try:
            tick_s = max(0.05, float(self.config.get("daemon.tick_s", 0.5) or 0.5))
        except Exception:
            pass
        next_prune = time.monotonic() + _PRUNE_EVERY_S
        while not self.stop_event.is_set():
            try:
                started = self.scheduler.tick()
                if started:
                    _log("started " + " ".join(str(item) for item in started))
            except Exception as exc:  # a bad tick must not kill the daemon
                _log(f"tick error: {type(exc).__name__}: {exc}")
            if time.monotonic() >= next_prune:
                next_prune = time.monotonic() + _PRUNE_EVERY_S
                try:
                    dropped = self.store.prune()
                    if dropped:
                        _log(f"pruned {dropped} stale job row(s)")
                except Exception:
                    pass
            self.stop_event.wait(tick_s)

    # -- metadata ---------------------------------------------------------
    def _write_meta(self, job_id: str) -> None:
        job = self.store.get(job_id) if self.store is not None else None
        if job is None:
            return
        job = self.enrich(job)
        target = paths.job_meta_path(job["id"])
        try:
            paths.ensure_dir(paths.job_dir(job["id"]))
            handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(job, stream, indent=2, default=str)
                stream.write("\n")
        except OSError as exc:
            _log(f"cannot write {target}: {exc}")

    # -- ops --------------------------------------------------------------
    def enrich(self, job: dict) -> dict:
        try:
            return self.scheduler.enrich(job)
        except Exception:
            return job

    def dispatch(self, payload: dict) -> dict:
        op = str(payload.get("op") or "")
        handler = self._ops.get(op)
        if handler is None:
            known = ", ".join(protocol.OPS)
            return {"ok": False, "error": f"unknown op: {op or '(missing)'} (known: {known})"}
        return handler(payload)

    def _op_ping(self, _payload: dict) -> dict:
        return {
            "ok": True,
            "version": __version__,
            "pid": os.getpid(),
            "socket": self.socket_path,
            "backend": self.backend_name,
            "state_dir": paths.STATE_DIR,
            "uptime_s": round(time.time() - self.started_at, 3),
        }

    def _op_submit(self, payload: dict) -> dict:
        argv = _argv_of(payload)
        if not argv:
            return {"ok": False, "error": "submit needs a command (argv or command)"}
        shell = bool(payload.get("shell"))
        job = self.scheduler.submit(
            argv=_as_shell_argv(argv) if shell else argv,
            cwd=str(payload.get("cwd") or "."),
            label=_opt_str(payload.get("label")),
            kind=str(payload.get("kind") or "auto"),
            pool=str(payload.get("pool") or "auto"),
            timeout_s=_opt_int(payload.get("timeout_s")),
            max_output_bytes=_opt_int(payload.get("max_output_bytes")),
            priority=_opt_int(payload.get("priority")),
            serial_key=str(payload.get("serial_key") or "auto"),
            agent=_opt_str(payload.get("agent")),
            shell=shell,
            memory_mb=_opt_int(payload.get("memory_mb")),
            cpu_percent=_opt_int(payload.get("cpu_percent")),
        )
        # scheduler.submit already wrote meta.json at enqueue time.
        return {"ok": True, "job": self.enrich(job)}

    def _op_status(self, payload: dict) -> dict:
        job_id = _job_id_of(payload)
        job = self.store.get(job_id) if job_id else None
        if job is None:
            return {"ok": False, "error": f"unknown job: {job_id or '(no id)'}"}
        return {"ok": True, "job": self.enrich(job)}

    def _op_list(self, payload: dict) -> dict:
        states = _state_list(payload.get("states"))
        limit = _opt_int(payload.get("limit")) or 200
        jobs = [self.enrich(job) for job in self.store.list_jobs(states=states, limit=limit)]
        return {"ok": True, "jobs": jobs, "count": len(jobs)}

    def _op_cancel(self, payload: dict) -> dict:
        job_id = _job_id_of(payload)
        if not job_id or self.store.get(job_id) is None:
            return {"ok": False, "error": f"unknown job: {job_id or '(no id)'}"}
        try:
            job = self.scheduler.cancel(job_id)
        except KeyError:  # the row vanished between the check and the cancel
            return {"ok": False, "error": f"unknown job: {job_id}"}
        # A queued job goes terminal inside cancel(); a running one is signaled
        # and scheduler.on_finished() records its terminal state and meta.json.
        return {"ok": True, "job": self.enrich(job)}

    def _op_wait(self, payload: dict) -> dict:
        job_id = _job_id_of(payload)
        job = self.store.get(job_id) if job_id else None
        if job is None:
            return {"ok": False, "error": f"unknown job: {job_id or '(no id)'}"}
        timeout_s = _opt_float(payload.get("timeout_s"))
        interval = max(0.05, _opt_float(payload.get("poll_s")) or 0.5)
        deadline = None if not timeout_s or timeout_s <= 0 else time.monotonic() + timeout_s
        while job.get("state") not in TERMINAL_STATES:
            if self.stop_event.is_set():
                return {
                    "ok": False,
                    "error": "daemon is shutting down",
                    "job": self.enrich(job),
                }
            if deadline is not None and time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "error": f"wait timed out after {timeout_s:g}s",
                    "timed_out": True,
                    "job": self.enrich(job),
                }
            self.stop_event.wait(interval)
            fresh = self.store.get(job_id)
            if fresh is not None:
                job = fresh
        return {"ok": True, "job": self.enrich(job), "timed_out": False}

    def _op_stats(self, payload: dict) -> dict:
        limit = _opt_int(payload.get("limit")) or 50
        try:
            rows = self.store.estimate_rows(limit=500)
        except Exception:
            rows = []
        try:
            accuracy = estimate.accuracy(self.store, limit=limit)
        except Exception:
            accuracy = []
        mape = 0.0
        if rows:
            try:
                mape = float(
                    estimate.mape_pct(
                        [float(row.get("est_seconds") or 0.0) for row in rows],
                        [float(row.get("actual_s") or 0.0) for row in rows],
                    )
                )
            except Exception:
                mape = 0.0
        return {
            "ok": True,
            "estimates": self.store.list_estimates(limit=limit),
            "accuracy": accuracy,
            "mape_pct": mape,
            "samples": len(rows),
            "count_by_state": self.store.count_by_state(),
        }

    def _op_estimates_clear(self, _payload: dict) -> dict:
        cleared = self.store.clear_estimates()
        return {"ok": True, "cleared": int(cleared or 0)}

    def _op_shutdown(self, _payload: dict) -> dict:
        self.stop_event.set()
        return {"ok": True, "stopping": True, "pid": os.getpid()}


def _remove(path: str, what: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"cannot remove {what} {path}: {exc}")


def serve(socket_path: str | None = None) -> int:
    """Run the daemon in the foreground; returns the process exit code."""
    server = _Server(socket_path)
    try:
        server.startup()
    except Exception as exc:
        _log(f"startup failed: {type(exc).__name__}: {exc}")
        if server.store is not None:
            try:
                server.store.close()
            except Exception:
                pass
        # Only clear the pidfile if we are the ones who wrote it.
        if os.getpid() == read_pid():
            _remove(pid_path(), "pidfile")
        if "already listening" in str(exc):
            # Another live daemon owns the socket. Exit 0 on purpose: under
            # systemd `Restart=always` a non-zero exit would crash-loop forever
            # while a perfectly healthy daemon is serving. `ajq daemon ensure`
            # restarts the unit if that other daemon ever goes away.
            _log("another daemon is serving; exiting 0 without restarting")
            return 0
        return 1
    return server.run()


def daemon_entry(argv: Sequence[str] | None = None) -> int:
    """Entry point for `ajq daemon ...`: serve|run|ensure|stop|status."""
    tokens = [str(token) for token in (argv or [])]
    command = ""
    socket_path = ""
    timeout = 5.0
    rest: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--sock" and index + 1 < len(tokens):
            socket_path = tokens[index + 1]
            index += 2
            continue
        if token == "--timeout" and index + 1 < len(tokens):
            try:
                timeout = float(tokens[index + 1])
            except ValueError:
                _log(f"bad --timeout {tokens[index + 1]!r}")
                return 1
            index += 2
            continue
        if not command and not token.startswith("-"):
            command = token
            index += 1
            continue
        rest.append(token)
        index += 1
    if not command:
        command = "status"
    if rest:
        _log(f"unexpected arguments: {' '.join(rest)}")
        return 1
    if socket_path:
        set_socket_override(socket_path)
    if command == "serve":
        return serve(None)
    if command == "run":
        # `run` is the systemd/launchd command: start and stay in the
        # foreground.  serve() already refuses when a daemon is listening, so
        # this only short-circuits before the Store is opened.
        if is_running():
            _log(f"already listening on {resolve_socket_path()}")
            return 0
        return serve(None)
    if command == "ensure":
        if ensure_running(timeout):
            _log(f"running on {resolve_socket_path()}")
            return 0
        _log(f"would not start; see {log_path()}")
        return 1
    if command == "stop":
        was_running = is_running()
        if stop_running(timeout):
            _log("stopped" if was_running else "not running")
            return 0
        _log("still running")
        return 1
    if command == "status":
        path = resolve_socket_path()
        if is_running(path):
            _log(f"running on {path} pid {read_pid() or os.getpid()}")
            return 0
        _log(f"not running ({path})")
        return 1
    _log(f"unknown daemon command: {command} (serve|run|ensure|stop|status)")
    return 1