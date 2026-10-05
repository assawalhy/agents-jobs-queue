# CONTRACT — internal interfaces for ajq (authoritative for all workers)

Target: Python 3.11+, **stdlib only** (no third-party imports), **Linux + macOS
(POSIX only, no Windows branches)**. Everything is a module inside
`src/ajq/`. No build step; the installer turns `src/` into a zipapp.

Style: terse, typed signatures, no docstring bloat, no filler comments. Linux is
the primary platform; macOS paths must work with the same code.

## Already written — DO NOT MODIFY

`src/ajq/paths.py`
```python
APP: str
STATE_DIR: str          # ~/.local/state/ajq        ($AJQ_STATE_DIR overrides)
CONFIG_PATH: str        # ~/.config/ajq/config.json ($AJQ_CONFIG overrides)
SOCKET_PATH: str        # $XDG_RUNTIME_DIR/ajq/ajqd.sock, else $TMPDIR/ajq-<uid>/ajqd.sock ($AJQ_SOCKET)
DB_PATH: str            # STATE_DIR/state.db
jobs_dir() -> str
job_dir(job_id: str) -> str
job_out_path(job_id: str) -> str      # job_dir/out.log
job_meta_path(job_id: str) -> str     # job_dir/meta.json
ensure_dir(path: str, mode: int = 0o700) -> str
```

`src/ajq/platform.py`
```python
IS_LINUX: bool; IS_MACOS: bool
total_memory_mb() -> int
available_memory_mb() -> int          # MemAvailable | free+inactive+speculative
cpu_count() -> int
interpreter_path() -> str            # absolute; systemd user PATH lacks ~/.local/bin
rss_mb(pid: int) -> int | None
set_nice(pid: int, niceness: int) -> None
```

`src/ajq/protocol.py`
```python
OPS: tuple[str, ...]
class ProtocolError(RuntimeError); class DaemonUnavailable(ProtocolError)
encode(payload: dict) -> bytes                     # one JSON line + \n
request(sock_path: str, payload: dict, timeout: float = 30.0) -> dict
read_request(conn) -> dict | None
write_response(conn, response: dict) -> None
serve(conn, handler: Callable[[dict], dict]) -> None   # one request per connection
```
Ops: `ping submit status list cancel wait stats estimates_clear shutdown`.
Request `{"op": ..., ...}`; response `{"ok": true, ...}` or `{"ok": false, "error": str}`.

## Job dict — every JOB_FIELDS key is always present in any job response

```
id label argv cwd kind pool serial_key state priority agent
enqueued_at started_at ended_at exit_code signal kill_reason
timeout_s max_output_bytes out_path out_bytes truncated
queue_position elapsed_s eta_start_s eta_run_s eta_total_s est_source
backend pid worktree git_root signature est_seconds
```

States: `queued running done failed timeout canceled lost`.
`TERMINAL_STATES = {done, failed, timeout, canceled, lost}`.

## store.py

```python
STATES: tuple[str, ...]
TERMINAL_STATES: frozenset[str]
JOB_FIELDS: tuple[str, ...]          # exactly the keys listed above, in that order
def new_job_id() -> str               # "j-" + 12 hex chars

class Store:
    def __init__(self, db_path: str) -> None
    def close(self) -> None
    # jobs
    def add_job(self, **fields) -> dict          # fills id/enqueued_at/state='queued', returns full dict
    def get(self, job_id: str) -> dict | None
    def list_jobs(self, states: Sequence[str] | None = None, limit: int = 200) -> list[dict]
    def queued_jobs(self) -> list[dict]           # ORDER BY priority DESC, enqueued_at ASC, id ASC
    def running_jobs(self) -> list[dict]
    def count_by_state(self) -> dict[str, int]
    def update(self, job_id: str, **fields) -> dict | None
    def start(self, job_id: str, pid: int, backend: str, started_at: float | None = None) -> dict
    def finish(self, job_id: str, state: str, *, exit_code: int | None = None,
               signal: int | None = None, kill_reason: str | None = None,
               out_bytes: int | None = None, truncated: bool | None = None,
               ended_at: float | None = None) -> dict
    def add_out_bytes(self, job_id: str, delta: int) -> None
    def recover_orphans(self) -> int             # running -> lost; returns rows changed
    # estimates
    def get_estimate(self, signature: str) -> dict | None   # {signature,n,mean,m2,kind,tool,last_at}
    def record_duration(self, signature: str, kind: str, tool: str, seconds: float) -> dict
    def list_estimates(self, limit: int = 100) -> list[dict]
    def clear_estimates(self) -> int
    def estimate_rows(self, limit: int = 500) -> list[dict]  # finished jobs w/ signature+est
    # housekeeping
    def prune(self, keep_days: int = 14) -> int
```
SQLite: WAL, `busy_timeout=5000`, one connection per Store guarded by
`threading.RLock`, `row_factory = sqlite3.Row`. Tables `jobs`, `estimates`,
`meta(key,value)` with `schema_version`. Timestamps are float epoch seconds.

## estimate.py

```python
COLD_DEFAULTS_S: dict[str, float]    # build 300 test 120 typecheck 90 lint 20 format 10
                                      # install 240 docker 600 check 30 unknown 60
FILECOUNT_BUCKETS: tuple[tuple[int, str], ...]      # ((1,"1"),(5,"2-5"),(20,"6-20"),(100,"21-100"),(1e9,"100+"))
def kind_from_command(argv: Sequence[str]) -> str    # build|test|typecheck|lint|format|install|docker|check|unknown
def tool_from_command(argv: Sequence[str]) -> str    # "pytest" | "npm:build" | "cargo" | ...
def changed_files(cwd: str, timeout: float = 2.0) -> list[str]
def file_signature(files: Sequence[str]) -> str      # "src,tests/21-100"
def signature_for(kind: str, tool: str, cwd: str, files: Sequence[str] | None = None) -> str
def parse_signature(signature: str) -> tuple[str, str, str, str]   # kind,tool,dirs,bucket
def estimate_seconds(store, kind: str, tool: str, cwd: str,
                     files: Sequence[str] | None = None) -> tuple[float, str]
    # -> (seconds, est_source); est_source = "cache:n=6" | "cold:build"
def record(store, kind: str, tool: str, cwd: str, seconds: float,
           files: Sequence[str] | None = None) -> str     # -> signature
def mape_pct(estimates: Sequence[float], actuals: Sequence[float]) -> float
def accuracy(store, limit: int = 50) -> list[dict]         # per-signature rows incl. mape_pct
```
Signature format: `kind|tool|dirs|bucket`. Estimate: warm -> `mean +
sigma_weight*sigma` clamped to `[mean/2, mean*3]`; cold -> `COLD_DEFAULTS_S[kind]`
scaled by filecount bucket (1 file x0.5, 100+ files x2.0). `changed_files` uses
`git status --porcelain -z` in `cwd`, capped at 200 paths, 2s timeout, `[]` on any
failure or non-git dir.

## backends/

```python
# backends/base.py
@dataclass
class Handle:
    pid: int
    name: str                      # backend name that spawned it
    kill: Callable[[int], None]    # signal -> terminate this job's process group (+ its scope unit)
    release: Callable[[], None]    # drop the systemd scope unit; no-op elsewhere

class ResourceBackend(Protocol):
    name: str
    def spawn(self, job: dict, argv: list[str]) -> Handle: ...
    def rss_mb(self, pid: int) -> int | None: ...
    def kill(self, handle: Handle, sig: int) -> None: ...

def get_backend(name: str = "auto") -> ResourceBackend   # auto|linux|macos|posix
```
Config read by backends: `resources.memory_mb` (default 2048), `resources.cpu_percent`
(200), `resources.nice` (10), `resources.extra_args` ([]), per-job overrides
`job["memory_mb"]`, `job["cpu_percent"]`.

`backends/linux.py` — `LinuxBackend`: `systemd-run --user --scope --quiet
--unit=ajq-<id> -p MemoryMax=<m>M -p MemorySwapMax=0 -p CPUQuota=<cpu>% -p
Nice=<n> -p OOMPolicy=stop -- <argv>`, spawned with `start_new_session=True`.
Kill = `killpg(pid, sig)` plus `systemctl --user kill --signal=<sig> ajq-<id>`.
`release()` -> `systemctl --user stop ajq-<id>` (ignore errors). If
`systemd-run` is missing or `systemctl --user` is unavailable, `LinuxBackend`
delegates every call to `MacosBackend` (direct spawn) and keeps
`name = "linux-posix-fallback"` so the downgrade stays visible.

`backends/macos.py` — `MacosBackend` (a.k.a. the POSIX backend): `Popen(argv,
start_new_session=True)`, `preexec_fn` applying `os.setpriority` when
`resources.nice > 0`, `kill` = `os.killpg(pid, sig)`, `rss_mb` delegates to
`platform.rss_mb`.

## exec.py

```python
@dataclass
class RunResult:
    exit_code: int | None
    signal: int | None
    kill_reason: str | None      # None | "timeout" | "output_limit" | "memory_limit" | "canceled"
    out_bytes: int
    truncated: bool
    elapsed_s: float

class JobRunner:
    def __init__(self, backend: ResourceBackend, poll_interval: float = 0.25,
                 memory_limit_mb: int | None = None) -> None
    def run(self, job: dict, cancel: threading.Event,
            on_out_bytes: Callable[[int], None] | None = None) -> RunResult
```
`run` opens `paths.job_out_path(job["id"])` in append mode (0o600), merges
stderr into stdout, streams to the file, and enforces in a poll loop:
`timeout_s` -> kill_reason `"timeout"`; `max_output_bytes` exceeded -> append a
truncation marker, `"output_limit"`; RSS over the effective memory limit ->
`"memory_limit"`; `cancel` set -> `"canceled"`. Terminate = SIGTERM to the group,
SIGKILL after `kill_grace_s` (default 10). Never deadlock: the reader is a daemon
thread and is joined with a timeout.

## config.py

```python
DEFAULTS: dict                  # the exact tree below
class Config:
    def get(self, dotted_key: str, default: Any = None) -> Any
    def __getitem__(self, dotted_key: str) -> Any
    def as_dict(self) -> dict
def env_overrides() -> dict                  # AJQ_MAX_CONCURRENT AJQ_POOL AJQ_TIMEOUT_S
                                            # AJQ_MAX_OUTPUT_BYTES AJQ_GUARD_MODE AJQ_MEMORY_MB
                                            # AJQ_CPU_PERCENT AJQ_NICE AJQ_BACKEND
                                            # AJQ_LIGHT_THRESHOLD_S AJQ_KILL_GRACE_S
def load_config(path: str | None = None, overrides: dict | None = None) -> Config
def default_config_json() -> str            # annotated template (a "_doc" key)
def seed_config(path: str, force: bool = False) -> bool
```
```json
{
  "limits": {"max_concurrent": 8, "pools": {"heavy": 2, "normal": 4, "service": 3, "light": 6}},
  "defaults": {"timeout_s": 1800, "max_output_bytes": 8388608, "pool": "auto", "kind": "auto",
               "kill_grace_s": 10, "priority": 0, "serial_key": "auto", "shell": false},
  "resources": {"memory_mb": 2048, "cpu_percent": 200, "nice": 10, "backend": "auto",
                "memory_headroom": 1.2, "extra_args": []},
  "estimates": {"enabled": true, "light_threshold_s": 30, "sigma_weight": 0.5,
                "cold_defaults_s": {"build": 300, "test": 120, "typecheck": 90, "lint": 20,
                                    "format": 10, "install": 240, "docker": 600, "check": 30,
                                    "unknown": 60}},
  "hooks": {"guard_mode": "warn"},
  "daemon": {"unit": "ajqd.service", "tick_s": 0.5, "socket": null}
}
```
Precedence: **flags > env > user file > DEFAULTS**. A malformed user file must not
crash: warn on stderr and fall back to DEFAULTS.

## guard.py

```python
HEAVY_KINDS: frozenset[str]      # build test typecheck install docker
LIGHT_KINDS: frozenset[str]      # lint format check
def classify(argv: Sequence[str]) -> dict
    # {"kind","tool","pool","heavy":bool,"reason":str}
def explain(argv: Sequence[str]) -> str          # multi-line, human readable
def is_heavy_command(command: str) -> bool       # string form, for PreToolUse hooks
```
Classifier covers at least: npm/yarn/pnpm/bun/deno run+build+test, make, cargo,
go, gradle/gradlew/mvn/mvnw, cmake --build, bazel, nix build, tsc, next build,
vite/webpack/rollup build, jest/vitest/mocha/phpunit/rspec/tox/playwright, pytest,
tox, dotnet build/test, sbt, mix test, swift build/test, flutter build/test,
docker build/compose, eslint/prettier/ruff/black/gofmt/rustfmt/stylelint/
shellcheck (light), and read-only commands (`git status|diff|log`, `rg`, `jq`,
`ls`, `cat`) -> kind `check`, pool `light`. `pool` = `light` for LIGHT_KINDS,
`heavy` for HEAVY_KINDS, else `normal`. Anything unrecognised -> `unknown`/`normal`.

## scheduler.py

```python
class Scheduler:
    def __init__(self, store: Store, config: Config, runner: JobRunner,
                 backend: ResourceBackend) -> None
    def submit(self, *, argv: Sequence[str], cwd: str = ".", label: str | None = None,
               kind: str = "auto", pool: str = "auto", timeout_s: int | None = None,
               max_output_bytes: int | None = None, priority: int | None = None,
               serial_key: str = "auto", agent: str | None = None, shell: bool = False,
               memory_mb: int | None = None, cpu_percent: int | None = None) -> dict
    def tick(self) -> list[str]        # starts every eligible job; returns started ids
    def on_finished(self, job: dict, result: RunResult) -> dict   # finish + estimate update
    def cancel(self, job_id: str) -> dict
    def enrich(self, job: dict) -> dict # adds the derived metadata keys
    def compute_position(self, job: dict) -> int | None
```
Eligibility for `tick`: pool cap free, `max_concurrent` free, no running job with
the same `serial_key`, and `platform.available_memory_mb() >=
effective_memory_mb * memory_headroom`. First fit over `store.queued_jobs()`.
`serial_key="auto"` -> `git rev-parse --show-toplevel` in `cwd` (2s timeout),
falling back to the resolved cwd; `"none"` disables serialization.
Enrichment: `elapsed_s` = now - (started_at or enqueued_at); `eta_run_s` =
`job["est_seconds"]`; `eta_start_s` = sum of `est_seconds` over queued jobs ahead
sharing the same pool plus the remaining time of running jobs in that pool (min 0,
0 when the job is running); `eta_total_s = eta_start_s + eta_run_s`;
`queue_position` = 1-based index in `store.queued_jobs()` or None when not queued.

## daemon.py

```python
def is_running(socket_path: str | None = None) -> bool
def ensure_running(timeout: float = 5.0) -> bool     # systemctl/launchctl, else detached spawn
def stop_running(timeout: float = 5.0) -> bool
def serve(socket_path: str | None = None) -> int     # foreground; returns exit code
def daemon_entry(argv: Sequence[str]) -> int        # serve|ensure|stop|status|run
```
Thread-per-connection server (one daemon thread runs `scheduler.tick()` every
`daemon.tick_s`). On start: unlink a stale socket, `store.recover_orphans()`, write
`STATE_DIR/ajqd.pid`, log to stderr. Dispatch table for every op in
`protocol.OPS`; `wait` blocks until the job is terminal (poll 0.5s) then returns
the enriched job; `shutdown` stops the loop. Write `paths.job_meta_path(id)`
(`meta.json`, mode 0o600) at enqueue time and again at terminal state.
`ensure_running` tries `systemctl --user start <unit>` (Linux) or
`launchctl bootstrap gui/<uid> <plist>` (macOS), then a detached double-fork
`ajq daemon serve`, then waits for the socket to answer `ping`.
SIGTERM: stop accepting, give running jobs `kill_grace_s` to finish, exit 0.

## cli.py

```python
def main(argv: Sequence[str] | None = None) -> int
```
Commands: `submit status list output cancel wait stats guard config doctor daemon
version`, global `--json` and `--sock PATH`. Every command except `daemon` and
`version` calls `ensure_running()` first and prints a clear one-line error when
the daemon cannot be reached. `--json` prints the raw response object.
`submit` takes the command after `--` (or as trailing args) and supports
`--shell`; supports `--wait` to block until terminal. `output` supports `--tail N`,
`--follow`, `--from-start`. `doctor` prints: backend name, socket path + alive,
systemd unit/launchd state, linger, config path, total/available memory, cpu
count, guard mode, daemon pid. Human output stays terse, e.g.

```
j-1a2b3c  running  test:unit       elapsed 42s   eta_run 1m35s   out 128K   ~/.local/state/ajq/jobs/j-1a2b3c/out.log
j-3c4d5e  queued#2 heavy  build:jar  eta_start 2m10s  eta_run 4m00s
```

## Repo rules for every worker

- Own **only** the files assigned to you. Never touch another group's file, the
  spine files, `install.sh`, `tests/`, or the plan directory.
- No third-party imports. No `print()` in library modules (use the logger passed
  in or nothing). No Windows code paths.
- Verify before reporting: `cd <repo> && python3 -c "import sys; sys.path.insert(0,'src');
  import ajq.<module>"` for each file you own, plus a tiny `python3 - <<'PY'`
  smoke script that exercises the main entry point of your module.
- Report back: the files you wrote, the exact signatures you implemented if they
  differ from this contract, and the smoke-test output.