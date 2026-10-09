"""The `ajq` command line interface.

Every command except `daemon` and `version` talks to the daemon over the unix
socket, so it first calls `daemon.ensure_running()` and prints one actionable
line when that fails.  Human output is terse single lines; `--json` prints the
raw daemon response.

Exit codes: 0 success, 1 error, 2 a `wait` that ended in a non-done state.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from typing import Any, Callable, Optional, Sequence

from . import __version__, daemon, paths, platform
from . import guard as guard_mod
from .config import Config, default_config_json, load_config, seed_config
from .protocol import ProtocolError, request
from .store import TERMINAL_STATES

# Commands that never talk to the daemon, so they skip ensure_running().
# The contract says "every command except daemon and version" calls it, but
# config and guard are pure local operations: config reads/writes one JSON file
# and guard is a classifier with no state at all.  Requiring a live daemon for
# them would make `ajq config --print-default` fail on a fresh machine, which
# is exactly when you want to print the template.  doctor deliberately does NOT
# appear here: per the contract it starts the daemon and then reports on it, so
# with an unreachable socket it prints one actionable line and exits 1.
DAEMON_FREE = frozenset({"daemon", "version", "config", "guard"})
_POLL_S = 0.2
_READ_CHUNK = 65536
_CONFIG_SUMMARY_KEYS = (
    "limits.max_concurrent",
    "limits.pools",
    "defaults.timeout_s",
    "defaults.kill_grace_s",
    "resources.backend",
    "resources.memory_mb",
    "resources.cpu_percent",
    "estimates.enabled",
    "hooks.guard_mode",
    "daemon.unit",
    "daemon.tick_s",
)


# -- output helpers -------------------------------------------------------


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    sys.stdout.flush()


def _field_names(raw: Any) -> list[str]:
    """Parse `--fields a,b,c` / `--select a,b,c` into a list of keys."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    names: list[str] = []
    for chunk in raw:
        for name in str(chunk).split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


def _project(payload: Any, names: list[str]) -> Any:
    """Keep only `names` from a job dict, or from every job in a list."""
    if not names:
        return payload
    if isinstance(payload, list):
        return [_project(item, names) for item in payload]
    if isinstance(payload, dict) and "jobs" in payload and isinstance(payload["jobs"], list):
        return {
            "jobs": [_project(item, names) for item in payload["jobs"]],
            "count": payload.get("count", len(payload["jobs"])),
        }
    if not isinstance(payload, dict):
        return payload
    return {name: payload.get(name) for name in names}


def _print_fields(payload: Any, names: list[str]) -> None:
    """One line, `key=value` pairs: the cheapest thing for an agent to read."""
    if isinstance(payload, dict) and "jobs" in payload:
        for job in payload["jobs"]:
            print(" ".join(f"{name}={_scalar(job.get(name))}" for name in names))
        return
    if isinstance(payload, list):
        for job in payload:
            print(" ".join(f"{name}={_scalar(job.get(name))}" for name in names))
        return
    print(" ".join(f"{name}={_scalar(payload.get(name))}" for name in names))


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)


def _fail(message: str) -> None:
    sys.stderr.write(f"ajq: {message}\n")
    sys.stderr.flush()


def _short(value: float | None) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value < 0:
        value = 0.0
    if value < 60:
        return f"{value:.0f}s"
    minutes = int(value // 60)
    if minutes < 60:
        seconds = int(value % 60)
        return f"{minutes}m{seconds:02d}s"
    hours = minutes // 60
    return f"{hours}h{minutes % 60:02d}m"


def _bytes(count: int | None) -> str:
    if not count:
        return "0B"
    size = float(count)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"





def _cell(value: Any, width: int) -> str:
    """Left-aligned cell; `width` is the minimum, never a truncation point."""
    text = "-" if value in (None, "") else str(value)
    return text if len(text) >= width else text.ljust(width)


def _tail(value: Any, width: int) -> str:
    text = "-" if value in (None, "") else str(value)
    return text if len(text) >= width else text.rjust(width)


def _cmd_of(job: dict) -> str:
    argv = job.get("argv") or []
    if isinstance(argv, str):
        try:
            argv = json.loads(argv)
        except ValueError:
            argv = [argv]
    if not argv:
        return "(no command)"
    return shlex.join(str(item) for item in argv)


def _pool_of(job: dict) -> str:
    pool = job.get("pool") or "-"
    position = job.get("queue_position")
    return f"queued#{position}" if job.get("state") == "queued" and position else str(pool)


def _state_of(job: dict) -> str:
    return str(job.get("state") or "?")


def _print_job_line(job: dict) -> None:
    job_id = job.get("id") or "-"
    state = _state_of(job)
    if state == "queued" and job.get("queue_position"):
        state = f"queued#{job['queue_position']}"
    parts = [
        _cell(job_id, 15),
        _cell(state, 9),
        _cell(job.get("pool"), 8),
        _tail(_short(job.get("elapsed_s")), 7),
    ]
    if state.startswith("queued"):
        parts.append(_tail("eta_start " + _short(job.get("eta_start_s")), 20))
        parts.append(_tail("eta_run " + _short(job.get("eta_run_s")), 17))
    else:
        parts.append(_tail("eta_run " + _short(job.get("eta_run_s")), 17))
    parts.append(_tail("out " + _bytes(job.get("out_bytes")), 12))
    print("  ".join(parts))


def _print_job_block(job: dict) -> None:
    _print_job_line(job)
    details = [f"cmd {_cmd_of(job)}"]
    if job.get("kind") and job.get("kind") != "auto":
        details.append(f"kind {job['kind']}")
    if job.get("serial_key"):
        details.append(f"serial {job['serial_key']}")
    if job.get("agent"):
        details.append(f"agent {job['agent']}")
    if job.get("cwd"):
        details.append(f"cwd {job['cwd']}")
    if job.get("signature"):
        details.append(f"sig {job['signature']}")
    print("  ".join(details))
    results = []
    if job.get("exit_code") is not None:
        results.append(f"exit {job['exit_code']}")
    if job.get("signal"):
        results.append(f"signal {job['signal']}")
    if job.get("kill_reason"):
        results.append(f"killed {job['kill_reason']}")
    if job.get("truncated"):
        results.append("truncated")
    if results:
        print("  ".join(results))


# -- transport ------------------------------------------------------------


def _socket() -> str:
    """Where the CLI talks: --sock wins, then `daemon.socket`, then $AJQ_SOCKET."""
    return daemon.resolve_socket_path()


def _request(payload: dict, timeout: float = 30.0) -> dict:
    return request(_socket(), payload, timeout=timeout)


def _require_daemon() -> bool:
    if daemon.is_running():
        return True
    if daemon.ensure_running():
        return daemon.is_running()
    _fail(
        f"no daemon on {_socket()}; tried systemctl/launchctl and a detached "
        f"spawn — start it with `ajq daemon serve` or check {daemon.log_path()}"
    )
    return False


def _need_daemon(command: str) -> bool:
    return command not in DAEMON_FREE


def _ok(response: dict) -> bool:
    if response.get("ok"):
        return True
    _fail(str(response.get("error") or "request failed"))
    return False


def _job_or_fail(response: dict) -> dict:
    if not _ok(response):
        return {}
    return response.get("job") or {}


# -- commands -------------------------------------------------------------


def _cmd_submit(args: argparse.Namespace) -> int:
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        _fail("nothing to run: ajq submit -- pytest -q")
        return 1
    payload: dict[str, Any] = {"op": "submit", "argv": argv, "shell": bool(args.shell)}
    if args.cwd:
        payload["cwd"] = args.cwd
    if args.kind:
        payload["kind"] = args.kind
    if args.pool:
        payload["pool"] = args.pool
    if args.label:
        payload["label"] = args.label
    if args.timeout_s is not None:
        payload["timeout_s"] = args.timeout_s
    if args.max_output_bytes is not None:
        payload["max_output_bytes"] = args.max_output_bytes
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.serial_key is not None:
        payload["serial_key"] = args.serial_key
    if args.agent:
        payload["agent"] = args.agent
    if args.memory_mb is not None:
        payload["memory_mb"] = args.memory_mb
    if args.cpu_percent is not None:
        payload["cpu_percent"] = args.cpu_percent
    job = _job_or_fail(_request(payload))
    if not job:
        return 1
    if args.wait:
        final = _wait_for(job["id"], args.wait_timeout)
        if final:
            job = final
        if args.json:
            _emit(job)
        else:
            _print_job_block(job)
        return 0 if job.get("state") == "done" else 2
    if args.json:
        _emit(job)
        return 0
    _print_job_block(job)
    return 0


def _wait_for(job_id: str, timeout_s: float | None) -> Optional[dict]:
    payload: dict[str, Any] = {"op": "wait", "id": job_id}
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s
    response = _request(payload, timeout=(timeout_s + 10.0) if timeout_s else 86400.0)
    job = response.get("job")
    return job if isinstance(job, dict) else None


def _cmd_status(args: argparse.Namespace) -> int:
    job = _job_or_fail(_request({"op": "status", "id": args.id}))
    if not job:
        return 1
    names = _field_names(args.fields)
    if names:
        if args.json:
            _emit(_project(job, names))
        else:
            _print_fields(job, names)
        return 0
    if args.json:
        _emit(job)
        return 0
    print(f"{job.get('id')}  {_state_of(job)}  {_pool_of(job)}")
    # the long context (cmd, cwd, serial, signature) is only useful when a human
    # is reading or the agent is debugging, so it stays behind --verbose
    if args.verbose:
        _print_job_block(job)
    out_path = job.get("out_path") or paths.job_out_path(job["id"])
    print(f"out {_bytes(job.get('out_bytes'))} {_tildify(out_path)}")
    return 0


def _tildify(path: str) -> str:
    home = os.path.expanduser("~")
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _cmd_list(args: argparse.Namespace) -> int:
    if args.id:
        args.func = _cmd_status
        return _cmd_status(args)
    payload: dict[str, Any] = {"op": "list", "limit": args.limit}
    if args.state:
        payload["states"] = list(args.state)
    elif not args.all:
        payload["states"] = ["queued", "running"]
    response = _request(payload)
    if not _ok(response):
        return 1
    jobs = response.get("jobs") or []
    names = _field_names(args.fields)
    if names:
        if args.json:
            _emit(_project({"jobs": jobs, "count": len(jobs)}, names))
        else:
            if not jobs:
                print("no jobs" if not args.state else "no jobs match")
                return 0
            _print_fields({"jobs": jobs}, names)
        return 0
    if args.json:
        # an object, not a bare array, so an agent can add fields later without
        # a breaking change (and so `list --json` matches every other op)
        _emit({"jobs": jobs, "count": len(jobs)})
        return 0
    if not jobs:
        print("no jobs" if not args.state else "no jobs match")
        return 0
    header = "  ".join(
        (
            _cell("id", 15),
            _cell("state", 9),
            _cell("pool", 8),
            _cell("kind", 10),
            _tail("elapsed", 8),
            _tail("est", 9),
            _tail("eta_start", 10),
            "out",
        )
    )
    print(header)
    for job in jobs:
        state = _state_of(job)
        queued = state == "queued"
        if queued and job.get("queue_position"):
            state = f"queued#{job['queue_position']}"
        eta_start = _short(job.get("eta_start_s")) if queued else "-"
        print(
            "  ".join(
                (
                    _cell(job.get("id"), 15),
                    _cell(state, 9),
                    _cell(job.get("pool"), 8),
                    _cell(job.get("kind"), 10),
                    _tail(_short(job.get("elapsed_s")), 8),
                    _tail(_short(job.get("est_seconds")), 9),
                    _tail(eta_start, 10),
                    _bytes(job.get("out_bytes")),
                )
            )
        )
    return 0


def _open_out(job: dict, create: bool = False):
    """Open out.log for reading; `create` also creates it (for --follow)."""
    out_path = job.get("out_path") or paths.job_out_path(job["id"])
    paths.ensure_dir(paths.job_dir(job["id"]))
    try:
        if create:
            descriptor = os.open(out_path, os.O_RDONLY | os.O_CREAT, 0o600)
            return os.fdopen(descriptor, "r", encoding="utf-8", errors="replace"), out_path
        return open(out_path, "r", encoding="utf-8", errors="replace"), out_path
    except FileNotFoundError:
        return None, out_path
    except OSError as exc:
        _fail(f"cannot read {out_path}: {exc}")
        return None, out_path


def _cmd_output(args: argparse.Namespace) -> int:
    job = _job_or_fail(_request({"op": "status", "id": args.id}))
    if not job:
        return 1
    # --follow starts before the job does, so create the file it will fill.
    handle, out_path = _open_out(job, create=args.follow)
    if handle is None:
        if _state_of(job) in TERMINAL_STATES:
            _fail(f"no output file for {job['id']} ({_tildify(out_path)})")
            return 1
        print("(no output yet)")
        return 0
    if args.follow:
        return _follow(job, handle, args)
    try:
        if args.from_start:
            sys.stdout.write(handle.read())
        else:
            lines = handle.read().splitlines()
            tail = lines[-args.tail :] if args.tail > 0 else []
            if tail:
                sys.stdout.write("\n".join(tail) + "\n")
    finally:
        handle.close()
    return 0


def _follow(job: dict, handle, args: argparse.Namespace) -> int:
    """Stream until the job reaches a terminal state, then stop."""
    deadline = None if args.timeout_s is None else time.monotonic() + args.timeout_s
    while True:
        chunk = handle.read(_READ_CHUNK)
        if chunk:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        if _state_of(job) in TERMINAL_STATES:
            remaining = handle.read()
            if remaining:
                sys.stdout.write(remaining)
                sys.stdout.flush()
            state = _state_of(job)
            print(f"-- {job['id']} {state}")
            return 0 if state == "done" else 2
        if deadline is not None and time.monotonic() >= deadline:
            print(f"-- {job['id']} still {_state_of(job)}, stopping follow")
            return 0
        refreshed = _request({"op": "status", "id": job["id"]}, timeout=5.0)
        if refreshed.get("ok") and isinstance(refreshed.get("job"), dict):
            job = refreshed["job"]
        time.sleep(_POLL_S)


def _cmd_cancel(args: argparse.Namespace) -> int:
    job = _job_or_fail(_request({"op": "cancel", "id": args.id}))
    if not job:
        return 1
    if args.json:
        _emit(job)
        return 0
    print(f"{job.get('id')}  {_state_of(job)}")
    if job.get("kill_reason"):
        print(f"kill_reason {job['kill_reason']}")
    return 0


def _job_tail(job: dict, count: int) -> list[str]:
    """The last `count` lines of a job's captured output, or [] when absent."""
    if not count or count <= 0:
        return []
    out_path = job.get("out_path") or paths.job_out_path(str(job.get("id") or ""))
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return lines[-count:]


def _cmd_wait(args: argparse.Namespace) -> int:
    job = _wait_for(args.id, args.timeout_s)
    if job is None:
        _fail(f"job {args.id} is not known to the daemon")
        return 2
    tail = _job_tail(job, getattr(args, "tail", 0))
    names = _field_names(getattr(args, "fields", None))
    if names:
        if args.json:
            _emit(_project(job, names))
        else:
            _print_fields(job, names)
        return 0 if job.get("state") == "done" else 2
    if args.json:
        payload = dict(job)
        if tail:
            payload["tail"] = tail
        _emit(payload)
    else:
        print(f"{job.get('id')}  {_state_of(job)}  elapsed {_short(job.get('elapsed_s'))}")
        if job.get("kill_reason"):
            print(f"kill_reason {job['kill_reason']}")
        for line in tail:
            print(line)
    return 0 if job.get("state") == "done" else 2


def _row_mean(row: dict) -> float:
    for key in ("mean_seconds", "mean_s", "mean"):
        if row.get(key) is not None:
            return float(row[key])
    return 0.0


def _row_sigma(row: dict) -> float:
    for key in ("sigma_seconds", "sigma_s", "sigma"):
        if row.get(key) is not None:
            return float(row[key])
    count = int(row.get("n") or 0)
    if count > 1 and row.get("m2") is not None:
        return max(0.0, float(row["m2"]) / (count - 1)) ** 0.5
    return 0.0


def _cmd_prune(args: argparse.Namespace) -> int:
    """Delete finished jobs (and their output) so the queue stays readable."""
    days = 0 if args.delete_all else max(0, int(args.older_than_days))
    if not args.json and not args.yes:
        what = "every finished job" if args.delete_all else f"finished jobs older than {days}d"
        files = "" if args.keep_files else " and their captured output"
        try:
            answer = input(f"delete {what}{files}? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 0
    response = _request(
        {
            "op": "prune",
            "older_than_days": days,
            "keep_files": bool(args.keep_files),
        }
    )
    if not _ok(response):
        return 1
    if args.json:
        _emit(response)
        return 0
    removed = int(response.get("removed") or 0)
    scope = "all finished jobs" if args.delete_all else f"older than {days}d"
    print(f"removed {removed} job(s) {scope}" + ("" if args.keep_files else " and their output"))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    if args.clear:
        response = _request({"op": "estimates_clear"})
        if not _ok(response):
            return 1
        if args.json:
            _emit(response)
            return 0
        print(f"cleared {int(response.get('cleared') or 0)} estimate(s)")
        return 0
    response = _request({"op": "stats", "limit": args.limit})
    if not _ok(response):
        return 1
    if args.json:
        _emit(response)
        return 0
    rows = response.get("accuracy") or []
    print(
        "  ".join(
            (
                _cell("signature", 34),
                _tail("n", 4),
                _tail("mean", 9),
                _tail("sigma", 9),
                _tail("mape", 8),
            )
        )
    )
    if not rows:
        print("(no estimate rows yet)")
    for row in rows:
        mape = row.get("mape_pct")
        print(
            "  ".join(
                (
                    _cell(row.get("signature"), 34),
                    _tail(row.get("n"), 4),
                    _tail(_short(_row_mean(row)), 9),
                    _tail(_short(_row_sigma(row)), 9),
                    _tail("-" if mape is None else f"{float(mape):.1f}%", 8),
                )
            )
        )
    summary = response.get("mape_pct")
    samples = int(response.get("samples") or 0)
    if samples:
        print(f"mape {float(summary or 0.0):.1f}% over {samples} sample(s)")
    counts = response.get("count_by_state") or {}
    active = " ".join(
        f"{state}={counts.get(state, 0)}" for state in ("queued", "running", "done", "failed")
    )
    print(f"jobs {active}")
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    command = args.explain if args.explain is not None else " ".join(args.command)
    command = command.strip()
    if not command:
        _fail('nothing to explain: ajq guard --explain "npm run build"')
        return 1
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _fail(f"cannot parse command: {exc}")
        return 1
    verdict = guard_mod.classify(argv)
    text = guard_mod.explain(argv)
    if args.json:
        _emit({"command": command, "argv": argv, "classification": verdict, "explain": text})
        return 0
    print(text)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    target = args.path or paths.CONFIG_PATH
    if args.print_default:
        text = default_config_json()
        if args.json:
            _emit(json.loads(text))
        else:
            print(text.rstrip("\n"))
        return 0
    if args.seed:
        created = seed_config(target, force=args.force)
        if not created:
            _fail(f"{target} already exists; pass --force to overwrite")
            return 1
        if args.json:
            _emit({"path": target, "seeded": True, "forced": bool(args.force)})
            return 0
        print(f"seeded {target}")
        return 0
    if args.path and not os.path.exists(target):
        print(f"{target}  (absent)")
        return 0
    config = load_config(target)
    if args.json:
        _emit(config.as_dict())
        return 0
    present = os.path.exists(target)
    print(f"config {_tildify(target)}  ({'present' if present else 'defaults only'})")
    for key in _CONFIG_SUMMARY_KEYS:
        print(f"{_cell(key, 26)} {_value_of(config, key)}")
    return 0


def _value_of(config: Config, dotted: str) -> Any:
    try:
        return config.get(dotted)
    except Exception:
        return "-"


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    socket_path = _socket()
    alive = daemon.is_running(socket_path)
    pid = None
    version = None
    if alive:
        ping = _request({"op": "ping"}, timeout=2.0)
        if ping.get("ok"):
            pid = ping.get("pid")
            version = ping.get("version")
    else:
        candidate = daemon.read_pid()
        if candidate and candidate != os.getpid():
            try:
                os.kill(candidate, 0)
                pid = candidate
            except OSError:
                pid = None
    try:
        backend_name = daemon.detect_backend()
    except Exception:
        backend_name = str(config.get("resources.backend") or "auto")
    report: dict[str, Any] = {
        "version": version or __version__,
        "backend": backend_name,
        "socket": socket_path,
        "daemon_alive": alive,
        "daemon_pid": pid,
        "platform": "linux" if platform.IS_LINUX else ("macos" if platform.IS_MACOS else "posix"),
        "config_path": paths.CONFIG_PATH,
        "config_exists": os.path.exists(paths.CONFIG_PATH),
        "total_memory_mb": platform.total_memory_mb(),
        "available_memory_mb": platform.available_memory_mb(),
        "cpu_count": platform.cpu_count(),
        "guard_mode": config.get("hooks.guard_mode"),
        "max_concurrent": config.get("limits.max_concurrent"),
        "kill_grace_s": config.get("defaults.kill_grace_s"),
        "pools": config.get("limits.pools"),
        "state_dir": paths.STATE_DIR,
        "log_path": daemon.log_path(),
        "notes": [],
    }
    if platform.IS_LINUX:
        unit = str(config.get("daemon.unit") or daemon.UNIT_FALLBACK)
        code, out = daemon.probe(["systemctl", "--user", "is-active", unit], timeout=3.0)
        report["systemd_unit"] = unit
        report["systemd_state"] = out if code == 0 else "unavailable"
        report["linger"] = _linger()
        if not report["linger"].startswith("yes"):
            report["notes"].append("linger=no: ajqd only starts at login")
    else:
        report["launchd_label"] = daemon.PLIST_LABEL
        report["launchd_state"] = _launchd_state()
        code, out = daemon.probe(["launchctl", "print-disabled", f"gui/{os.getuid()}"], timeout=3.0)
        report["launchd_disabled"] = "unknown" if code != 0 else (
            "enabled" if f"{daemon.PLIST_LABEL} = false" in out else "disabled"
        )
        report["notes"].append("macOS memory cap is advisory; admission gate + RSS watchdog protect")
    if not os.path.exists(paths.CONFIG_PATH):
        report["notes"].append("no config file yet: ajq config --seed")
    if not alive:
        report["notes"].append("daemon is down: ajq daemon ensure")
    if args.json:
        _emit(report)
        return 0
    print(f"ajq {report['version']}  backend {report['backend']}  {report['platform']}")
    print(
        f"daemon  {'alive' if alive else 'down'}  pid {_cell(pid or '-', 8)}"
        f" socket {_tildify(socket_path)}"
    )
    if platform.IS_LINUX:
        print(f"systemd {report['systemd_unit']} {report['systemd_state']}  linger {report['linger']}")
    else:
        print(f"launchd {report['launchd_label']} {report['launchd_state']}  {report.get('launchd_disabled')}")
    print(f"config  {_tildify(paths.CONFIG_PATH)}  {'present' if report['config_exists'] else 'absent'}")
    print(
        f"memory  total {report['total_memory_mb']}M"
        f"  available {report['available_memory_mb']}M"
        f"  cpus {report['cpu_count']}"
    )
    print(f"guard   mode {report['guard_mode']}  max_concurrent {report['max_concurrent']}")
    print(f"log     {_tildify(report['log_path'])}")
    for note in report["notes"]:
        print(f"note    {note}")
    return 0


def _linger() -> str:
    code, out = daemon.probe(["loginctl", "show-user", str(os.getuid())], timeout=3.0)
    if code != 0:
        return "unknown"
    for line in out.splitlines():
        if line.startswith("Linger="):
            return line.split("=", 1)[1].strip()
    return "unknown"


def _launchd_state() -> str:
    code, out = daemon.probe(["launchctl", "list", daemon.PLIST_LABEL], timeout=3.0)
    if code == 0:
        return out.splitlines()[0].strip() if out.strip() else "loaded"
    code, _ = daemon.probe(["launchctl", "print", f"gui/{os.getuid()}/{daemon.PLIST_LABEL}"], timeout=3.0)
    return "loaded" if code == 0 else "not loaded"


def _cmd_daemon(args: argparse.Namespace) -> int:
    rest: list[str] = []
    if args.sock_override:
        rest += ["--sock", args.sock_override]
    if args.timeout_s is not None:
        rest += ["--timeout", str(args.timeout_s)]
    return daemon.daemon_entry([args.action] + rest)


def _cmd_version(args: argparse.Namespace) -> int:
    """Print the version.

    Exits through SystemExit(0) like argparse's own `--version` action, so
    `ajq version` and `ajq --version` behave identically.
    """
    if getattr(args, "json", False):
        _emit(
            {
                "name": "ajq",
                "version": __version__,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}"
                f".{sys.version_info.micro}",
            }
        )
    else:
        print(f"ajq {__version__}")
    raise SystemExit(0)


# -- parser ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ajq",
        description="queue heavy agent tasks through ajqd instead of running them directly",
    )
    parser.add_argument("--json", action="store_true", help="print the raw daemon response")
    parser.add_argument("--sock", metavar="PATH", help="daemon socket path (default $AJQ_SOCKET)")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="cmd")

    def add(
        name: str,
        help_text: str,
        func: Callable[[argparse.Namespace], int],
        aliases: Sequence[str] = (),
    ) -> argparse.ArgumentParser:
        child = sub.add_parser(name, aliases=list(aliases), help=help_text, description=help_text)
        # SUPPRESS so that `ajq --json status X` is not clobbered by this default.
        child.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="print the raw daemon response",
        )
        child.add_argument(
            "--sock", metavar="PATH", default=argparse.SUPPRESS, help="daemon socket path"
        )
        child.set_defaults(func=func)
        return child

    def add_fields(child: argparse.ArgumentParser) -> None:
        """`--fields a,b,c` keeps the output small enough for an agent."""
        child.add_argument(
            "--fields",
            "--select",
            dest="fields",
            action="append",
            metavar="A,B,C",
            help="only these keys, e.g. --fields state,elapsed_s,out_bytes",
        )

    submit = add("submit", "queue a command for the daemon to run", _cmd_submit)
    submit.add_argument("--cwd", help="working directory for the command")
    submit.add_argument("--kind", help="auto|build|test|typecheck|lint|format|install|docker|check")
    submit.add_argument("--pool", help="auto|heavy|normal|service|light")
    submit.add_argument("--label", help="human label shown by list")
    submit.add_argument("--timeout", dest="timeout_s", type=float, metavar="S", help="kill after S seconds")
    submit.add_argument("--max-output-bytes", type=int, metavar="N", help="stop recording past N bytes")
    submit.add_argument("--priority", type=int, help="higher runs first inside a pool")
    submit.add_argument("--serial-key", metavar="KEY", help="auto|none|<key> (default: git worktree root)")
    submit.add_argument("--agent", help="which agent submitted the job")
    submit.add_argument("--memory-mb", type=int, metavar="MB", help="per-job memory cap")
    submit.add_argument("--cpu-percent", type=int, metavar="PCT", help="per-job CPU quota")
    submit.add_argument("--shell", action="store_true", help="run the command through $SHELL")
    submit.add_argument("--wait", action="store_true", help="block until the job is terminal")
    submit.add_argument("--wait-timeout", dest="wait_timeout", type=float, metavar="S", help="give up waiting after S")
    submit.add_argument("command", nargs="+", help="command to run, after --")

    prune = add("prune", "delete old finished jobs and their captured output", _cmd_prune)
    prune.add_argument(
        "--older-than",
        dest="older_than_days",
        type=int,
        default=14,
        metavar="DAYS",
        help="delete finished jobs older than DAYS (default 14)",
    )
    prune.add_argument(
        "--all",
        dest="delete_all",
        action="store_true",
        help="delete every finished job regardless of age",
    )
    prune.add_argument(
        "--keep-files",
        action="store_true",
        help="keep each job's out.log/meta.json on disk",
    )
    prune.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    status = add("status", "print one job's state and metadata", _cmd_status)
    status.add_argument("id", help="job id, e.g. j-1a2b3c")
    add_fields(status)
    status.add_argument(
        "-v", "--verbose", action="store_true", help="include cmd, cwd and signature"
    )

    listing = add("list", "queued and running jobs", _cmd_list, aliases=("ls",))
    listing.add_argument("id", nargs="?", help="optional job id to show in full")
    listing.add_argument("--all", action="store_true", help="include terminal jobs")
    listing.add_argument("--state", action="append", metavar="STATE", help="filter by state (repeatable)")
    listing.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    add_fields(listing)

    output = add("output", "print a job's captured output", _cmd_output, aliases=("logs",))
    output.add_argument("id", help="job id")
    output.add_argument("--tail", type=int, default=40, metavar="N", help="last N lines (default 40)")
    output.add_argument("--follow", "-f", action="store_true", help="stream until the job is terminal")
    output.add_argument("--from-start", action="store_true", help="show the whole log, not the tail")
    output.add_argument("--timeout", dest="timeout_s", type=float, metavar="S", help="give up following after S")

    cancel = add("cancel", "cancel a queued or running job", _cmd_cancel)
    cancel.add_argument("id", help="job id")

    wait = add("wait", "block until a job is terminal", _cmd_wait)
    wait.add_argument("id", help="job id")
    wait.add_argument("--timeout", dest="timeout_s", type=float, metavar="S", help="give up after S")
    wait.add_argument(
        "--tail",
        "-n",
        type=int,
        default=0,
        metavar="N",
        help="also print the last N lines of output (one call: state + log)",
    )
    add_fields(wait)

    stats = add("stats", "estimate table and MAPE accuracy", _cmd_stats)
    stats.add_argument("--clear", action="store_true", help="clear all cached estimates")
    stats.add_argument("--limit", type=int, default=50, metavar="N", help="rows (default 50)")

    guard = add("guard", "explain how a command is classified", _cmd_guard)
    guard.add_argument("--explain", "-e", metavar="CMD", help="the command string to classify")
    guard.add_argument("command", nargs="*", help="the command, if not given with --explain")

    config = add("config", "show, print or seed the user config", _cmd_config)
    config.add_argument("--path", metavar="PATH", help="config file path (default ~/.config/ajq/config.json)")
    config.add_argument("--print-default", action="store_true", help="print the annotated default template")
    config.add_argument("--seed", action="store_true", help="write the default template when absent")
    config.add_argument("--force", action="store_true", help="with --seed, overwrite an existing file")

    add("doctor", "report backend, socket, unit, linger, memory", _cmd_doctor)

    daemon_cmd = add("daemon", "serve|run|ensure|stop|status", _cmd_daemon)
    daemon_cmd.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("serve", "run", "ensure", "stop", "status"),
        help="default status",
    )
    daemon_cmd.add_argument("--sock-override", metavar="PATH", help="socket path for this call")
    daemon_cmd.add_argument("--timeout", dest="timeout_s", type=float, metavar="S", help="seconds to wait")

    add("version", "print the version", _cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    # argparse raises SystemExit for --help and for usage errors; the tests and
    # the `ajq` entry point both expect that to propagate, so let it through.
    args = parser.parse_args(list(argv) if argv is not None else None)
    # The subparsers suppress their defaults so a global --json/--sock survives.
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "sock"):
        args.sock = None
    if args.sock:
        daemon.set_socket_override(args.sock)
    if args.version:
        return _cmd_version(args)
    if not args.cmd:
        parser.print_help()
        return 0
    if not _need_daemon(args.cmd):
        return int(args.func(args))
    if not _require_daemon():
        return 1
    try:
        return int(args.func(args))
    except ProtocolError as exc:
        _fail(f"daemon said no: {exc}")
        return 1
    except KeyboardInterrupt:
        _fail("interrupted")
        return 130
    except BrokenPipeError:
        return 0