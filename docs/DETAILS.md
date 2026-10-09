# ajq — details

Reference for the guards, the config, the CLI and the harness integrations. The
README covers what it is and how to install it.

## Guards and defaults

| Guard | Default | Behaviour |
| --- | --- | --- |
| Concurrency | `max_concurrent: 8` | Caps every pool at once |
| Pools | `heavy 2 · normal 4 · service 3 · light 6` | One cap per tier; the tier comes from the job's kind and estimate |
| Worktree serialization | `serial_key` = git worktree root | Two agents in one checkout never run overlapping commands |
| Memory admission | `memory_headroom: 1.2` | A job starts only when `MemAvailable >= memory_limit × 1.2` |
| Per-job memory | `memory_mb: 2048` | Linux: cgroup `MemoryMax` + `MemorySwapMax=0` + `OOMPolicy=stop`. macOS: RSS watchdog kill (advisory) |
| CPU | `cpu_percent: 200`, `nice: 10` | Linux: `CPUQuota` + `Nice`. Both: `setpriority` |
| Timeout | `timeout_s: 1800` | SIGTERM to the process group, SIGKILL after `kill_grace_s` (10s), state `timeout` |
| Output size | `max_output_bytes: 8 MiB` | Appends a truncation marker and terminates the job. `--keep-going-on-output` only stops recording |

Every guard is overridable per submit (`--timeout`, `--max-output-bytes`,
`--pool`, `--priority`, `--memory-mb`, `--cpu-percent`) or in
`~/.config/ajq/config.json`.

## Commands

| Command | Purpose |
| --- | --- |
| `ajq submit [--kind K] [--pool P] [--timeout S] [--priority N] [--serial-key K] [--wait] -- cmd args…` | Queue a command (`--shell` to run a shell string) |
| `ajq status <id> [--json] [--fields a,b] [-v]` | State, queue position, elapsed, ETAs, output path and size, exit code |
| `ajq list [--all] [--json] [--fields a,b]` | Queued + running, or everything |
| `ajq output <id> [--tail N] [--follow] [--from-start]` | The recorded output |
| `ajq wait <id> [--tail N] [--timeout S] [--fields a,b]` | Block until terminal; `--tail` prints the last N log lines so one call returns state + log; exit 0 only for `done` |
| `ajq cancel <id>` | Cancel a queued or running job |
| `ajq stats [--clear]` | Estimate table with per-signature MAPE |
| `ajq guard --explain "<cmd>"` | Why a command is heavy or light, and the `ajq` equivalent |
| `ajq config [--print-default\|--seed\|--force-config]` | Inspect or seed the config |
| `ajq doctor` | Backend, socket, unit state, linger, memory, guard mode |
| `ajq prune [--older-than D\|--all] [--keep-files] [--yes]` | Delete finished jobs and their captured output |
| `ajq daemon serve\|ensure\|status\|stop` | Daemon control |

### Cheap polling for agents

`--fields a,b,c` (alias `--select`) prints only the keys you name, so an agent
never needs to pipe `--json` into a `python3 -c` parser:
```console
$ ajq status j-1a2b --fields state,elapsed_s,out_bytes
state=running elapsed_s=12.3 out_bytes=4096

$ ajq status j-1a2b --fields state,exit_code --json
{"exit_code": null, "state": "running"}

$ ajq list --fields id,state,eta_run_s --all
id=j-1a2b state=running eta_run_s=57.1
```

Unknown keys come back as `null` rather than vanishing, so a typo is visible
instead of silently returning nothing. Plain `ajq status <id>` prints only
state, pool, timing and the output path; `--verbose` adds `cmd`, `cwd` and the
estimate signature. Use `ajq wait <id> --tail N` (background it for a slow job),
not `sleep N`.

### States

`queued` → `running` → `done` | `failed` | `timeout` | `canceled` | `lost`

`lost` means the daemon restarted while the job ran; re-submit it. A queued or
running job reports `exit_code: null` — it has not exited yet.

## Estimates

Each job gets an `eta_run_s` from a small cache keyed by
`kind|tool|toplevel-dirs|filecount-bucket` (`test|pytest|src,tests|21-100`):

- **Warm** (`n ≥ 2`): `mean + 0.5·σ`, clamped to `[mean/2, mean·3]`, from a
  Welford running mean and variance of observed runtimes.
- **Cold**: a per-kind default (build 300s, test 120s, typecheck 90s, lint 20s,
  install 240s, docker 600s …) scaled by the file-count bucket.

`mean` measures **execution time only** (start → exit), never queue wait, since
wait depends on other jobs rather than on the signature. `eta_start_s` is the
sum of the estimates of the jobs ahead in the same pool plus the remaining time
of the running ones.

`ajq stats` prints `n`, mean, σ and MAPE per signature, so the accuracy is
measurable instead of claimed:

```console
$ ajq stats
signature                              n   mean     sigma   mape
test|pytest|src,tests|21-100           14  96.4s    12.1s   11.2%
build|gradle|app|100+                   3  412.0s   88.0s   19.7%
MAPE over 17 finished jobs: 14.0%
```

Changed files come from `git status --porcelain` in the job's cwd (2s cap), so
touching 200 files estimates differently from touching one.

## Platform support

Linux and macOS (POSIX). **No Windows.**

| | Linux | macOS |
| --- | --- | --- |
| Execution | `systemd-run --user --scope` with `MemoryMax`, `MemorySwapMax=0`, `CPUQuota`, `Nice`, `OOMPolicy=stop` | direct spawn + `setpriority` |
| Hard RAM cap | yes (cgroup v2) | **no** — advisory |
| RSS sampling | `/proc/<pid>/status` | `ps -o rss=` |
| Free memory | `/proc/meminfo` `MemAvailable` | `sysctl hw.memsize` + `vm_stat` |
| Boot service | systemd user unit + linger | launchd agent |

macOS cannot hard-cap a single process's memory: there is no cgroup write
access, and `RLIMIT_AS` counts virtual address space so it breaks Node, the JVM
and Docker outright. There, the free-RAM admission gate does the real work,
plus a watchdog kill when RSS exceeds the cap. `ajq doctor` names the active
backend and says so rather than implying parity.

## Harness integration

| Harness | Skill | Hooks | Tool guard |
| --- | --- | --- | --- |
| OpenCode | yes | plugin `~/.config/opencode/plugins/ajq.js` (V2 API, verified on 2.0.26) | yes, plus native `ajq_submit` / `ajq_status` / `ajq_output` / `ajq_wait` tools |
| Claude Code | yes | `SessionStart` + `PreToolUse Bash` in `settings.json` | yes |
| Codex | yes | `SessionStart` + `PreToolUse Bash` in `hooks.json` | yes |
| Kiro | yes | `~/.kiro/hooks/ajq.json` (`SessionStart`, `PreToolUse`) | yes |
| Pi | yes | extension (see caveat) | skill only |
| Kilo, Kimi, DeepSeek, Cursor | yes | — | skill only |

Hooks do two things: at session start they ensure the daemon is up and inject
the queue snapshot, and on `PreToolUse` for Bash they classify the command and
point the agent at `ajq submit`. A hook never blocks when the daemon is
unreachable or when the command already goes through `ajq`.

The OpenCode plugin targets the **V2 plugin API** (OpenCode `2.0.26`): V2 calls
the default export's `setup(ctx)` and ignores a V1 `server()`, so all logic lives
in `setup`. It hooks the `shell` tool (V1 called it `bash`; both are accepted).
Because V2 has no per-tool context channel, `warn` there appends the note to the
tool result after the command runs, while `block` denies the call before it runs.
`hooks.guard_mode` is read lazily (10 s cache), so a config change lands without
restarting OpenCode.

JSON files the installer does not own are **merged, never rewritten**: only
entries carrying an ajq marker are removed and re-added, so hooks from other
tools (herdr, plannotator, …) and every unrelated setting survive install,
update and uninstall. A backup is written first and the result is validated
before it replaces the original.

Known integration limits: the `pi` binary in use is a Go port with no JavaScript
extension loader, so the pi extension only takes effect on the Node build — the
skill covers pi either way.

## Configuration

`~/.config/ajq/config.json`, plain JSON. Precedence: **submit flags > `AJQ_*`
environment > user config > built-in defaults.** The installer seeds the file
only when it is absent and never overwrites it on update.

```json
{
  "limits": {
    "max_concurrent": 8,
    "pools": { "heavy": 2, "normal": 4, "service": 3, "light": 6 }
  },
  "defaults": {
    "timeout_s": 1800, "max_output_bytes": 8388608, "pool": "auto",
    "kind": "auto", "kill_grace_s": 10, "priority": 0
  },
  "resources": {
    "memory_mb": 2048, "cpu_percent": 200, "nice": 10,
    "backend": "auto", "memory_headroom": 1.2
  },
  "estimates": { "enabled": true, "light_threshold_s": 30, "sigma_weight": 0.5 },
  "hooks": { "guard_mode": "warn" },
  "daemon": { "unit": "ajqd.service", "tick_s": 0.5 }
}
```

Environment overrides: `AJQ_MAX_CONCURRENT`, `AJQ_POOL`, `AJQ_TIMEOUT_S`,
`AJQ_MAX_OUTPUT_BYTES`, `AJQ_GUARD_MODE`, `AJQ_MEMORY_MB`, `AJQ_CPU_PERCENT`,
`AJQ_NICE`, `AJQ_BACKEND`, `AJQ_LIGHT_THRESHOLD_S`, `AJQ_KILL_GRACE_S`.
`AJQ_NO_AUTOSTART=1` stops `ensure` from starting a daemon (used by the tests).

## State on disk

```
~/.local/state/ajq/state.db                       SQLite (WAL): jobs, estimates
~/.local/state/ajq/jobs/<id>/out.log              captured output, mode 0600
~/.local/state/ajq/jobs/<id>/meta.json            the same metadata as a sidecar
~/.local/state/ajq/ajqd.pid                        live daemon pid
/run/user/<uid>/ajq/ajqd.sock                      control socket (dir 0700)
```

A daemon crash leaves `running` jobs marked `lost` on the next start; their
output files stay readable. `ajq prune` removes finished jobs and their output.

Jobs are argv-only by default — the daemon never runs `sh -c` unless you pass
`--shell`.

## Development

```bash
python3 -m unittest discover -s tests -t tests   # 122 tests, no daemon required
./install.sh --dry-run --all                     # preview an install
tools/demo-gif/build.sh                          # rebuild assets/ajq-demo.gif
```

The test suite never starts a real daemon (`AJQ_NO_AUTOSTART=1`) and installs
into a throwaway `$HOME`, so it cannot touch your machine.

The demo animation is a pure function of time in `tools/demo-gif/demo.html`, so
rebuilding produces the same GIF; no CSS keyframe animations are involved.

## Known gaps

- macOS has no hard per-process RAM cap; the admission gate plus an RSS watchdog
  is the protection. `ajq doctor` states this.
- Kilo / Kimi / DeepSeek / Cursor get the skill but no hooks.
- The `pi` binary in use is a Go port without a JS extension loader.