# ajq — a jobs-queue daemon for heavy agent tasks

Parallel coding agents run builds, tests and typechecks at the same time, in
several worktrees, and fill the machine: RAM spikes, CPU thrash, swap storms,
and long commands that block a turn for twenty minutes. `ajq` puts one daemon in
front of all of them.

Instead of running a heavy command in the shell, an agent submits it:

```console
$ ajq submit --kind test -- pytest -q
j-1a2b3c4d5e6f  queued#1  test  eta_start 0s  eta_run 1m37s

$ ajq status j-1a2b3c4d5e6f --json
{ "state": "running", "queue_position": null, "elapsed_s": 42.1,
  "eta_run_s": 95.0, "out_path": "~/.local/state/ajq/jobs/j-1a2b.../out.log",
  "out_bytes": 128431, "truncated": false, "backend": "linux-systemd", ... }

$ ajq output j-1a2b3c4d5e6f --tail 20
...test output...
$ ajq wait j-1a2b3c4d5e6f        # blocks, exit 0 only for done
```

The daemon decides *when* it runs, caps how much runs at once, kills anything
that runs too long or gets too chatty, and tells the agent what to expect.

## What it actually enforces

| Guard | Default | Behaviour |
|---|---|---|
| Concurrency | `max_concurrent: 8`, pools `heavy 2 / normal 4 / service 3 / light 6` | First-fit over the queue; a pool cap applies per tier |
| Worktree serialization | `serial_key` = git worktree root | Two agents in one worktree never run overlapping commands; different worktrees run in parallel |
| Memory admission | `memory_headroom: 1.2` | A job only starts when `MemAvailable >= memory_limit × 1.2`, so a hot box queues instead of OOMing |
| Per-job memory | `resources.memory_mb: 2048` | Linux: cgroup `MemoryMax`+`MemorySwapMax=0`+`OOMPolicy=stop`. macOS: RSS watchdog kill (advisory — see below) |
| CPU | `cpu_percent: 200`, `nice: 10` | Linux: `CPUQuota` + `Nice`. Both: `setpriority` |
| Timeout | `defaults.timeout_s: 1800` | SIGTERM to the process group, SIGKILL after a 10s grace, state `timeout`. Per job: `--timeout` |
| Output size | `max_output_bytes: 8 MiB` | Appends a truncation marker and terminates the job (`truncated: true`). `--keep-going-on-output` to only stop recording |

Every override is available per submit (`--timeout`, `--max-output-bytes`,
`--pool`, `--priority`, `--memory-mb`, `--cpu-percent`) or in
`~/.config/ajq/config.json`.

## Install

One line, no clone, nothing left behind:

```bash
curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/main/install.sh | bash
```

That fetches the payload into a temp directory, installs, and cleans up after
itself. It needs `python3` (3.11+); `git` is used to fetch when present and an
HTTPS tarball otherwise.

```bash
ajq doctor                    # check what got installed
```

Useful variants:

```bash
# pinned to a tag instead of main
curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/main/install.sh | AJQ_VERSION=v0.1.0 bash

# only the CLI and integrations, no boot service (no linger, no systemd/launchd)
curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/main/install.sh | bash -s -- --no-daemon

# pick harnesses
curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/main/install.sh | bash -s -- --target claude,codex

# uninstall
curl -fsSL https://raw.githubusercontent.com/assawalhy/agents-jobs-queue/main/install.sh | bash -s -- --uninstall
```

`install.sh` also accepts `--dry-run`, `--force-config` and `--uninstall --purge`
(deletes job history and the estimate cache too).

From a checkout:

```bash
git clone https://github.com/assawalhy/agents-jobs-queue.git
cd agents-jobs-queue && ./install.sh --all
```

What it installs:

- **`~/.local/bin/ajq`** — a stdlib-only Python zipapp. No dependencies, no build
  step, Python 3.11+.
- **Boot service** — Linux: `~/.config/systemd/user/ajqd.service` plus
  `loginctl enable-linger` (without linger the user manager dies at logout and
  there is no boot start). macOS: `~/Library/LaunchAgents/io.ajq.ajqd.plist` plus
  `launchctl bootstrap`.
- **`~/.config/ajq/config.json`** — seeded only when absent, never overwritten on
  update. `ajq config --print-default` prints an annotated template.
- **Skill + hooks** for every detected harness.

Even without the boot service, `ajq daemon ensure` (run automatically by every
hook, and by `ajq submit`) starts the daemon on demand.

## Commands

| Command | Purpose |
|---|---|
| `ajq submit [--kind K] [--pool P] [--timeout S] [--priority N] [--serial-key K] [--wait] -- cmd args…` | queue a command (`--shell` to run a shell string) |
| `ajq status <id> [--json] [--fields a,b] [-v]` | state, queue position, elapsed, ETAs, output path/size, exit code |
| `ajq list [--all] [--json] [--fields a,b]` | queued + running (or everything) |
| `ajq output <id> [--tail N] [--follow] [--from-start]` | the recorded output |
| `ajq wait <id> [--timeout S]` | block until terminal; exit 0 only for `done` |
| `ajq cancel <id>` | cancel a queued or running job |
| `ajq stats [--clear]` | estimate table with per-signature MAPE |
| `ajq prune [--older-than D\|--all] [--keep-files] [--yes]` | delete finished jobs and their captured output |
| `ajq guard --explain "<cmd>"` | why a command is heavy/light, and the `ajq` equivalent |
| `ajq config [--print-default\|--seed\|--force-config]` | inspect or seed the config |
| `ajq doctor` | backend, socket, unit state, linger, memory, guard mode |
| `ajq daemon serve\|ensure\|status\|stop` | daemon control |

`--json` on any command prints the raw daemon response, for agents.

### Cheap polling for agents

`--fields a,b,c` (alias `--select`) prints only the keys you name, so an agent
never has to pipe `--json` into a `python3 -c` parser:

```console
$ ajq status j-1a2b --fields state,elapsed_s,out_bytes
state=running elapsed_s=12.3 out_bytes=4096

$ ajq status j-1a2b --fields state,exit_code --json
{"exit_code": null, "state": "running"}

$ ajq list --fields id,state,eta_run_s --all
id=j-1a2b state=running eta_run_s=57.1
```

`ajq wait` and `ajq list` take `--fields` too. Plain `ajq status <id>` prints
only state, pool, timing and the output path; `--verbose` adds `cmd`, `cwd` and
the estimate signature. And use `ajq wait <id>`, not `sleep N` — it blocks
until the job is terminal and exits 0 only for `done`.

### States

`queued` → `running` → `done` | `failed` | `timeout` | `canceled` | `lost`

`lost` means the daemon restarted while the job ran; re-submit it. A queued or
running job reports `exit_code: null` — it has not exited yet.

## Estimates

Each job gets an `eta_run_s` from a small cache keyed by
`kind|tool|toplevel-dirs|filecount-bucket` (`test|pytest|src,tests|21-100`):

- **Warm** (`n ≥ 2`): `mean + 0.5·σ`, clamped to `[mean/2, mean·3]`, from a
  Welford running mean/variance of observed runtimes.
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
|---|---|---|
| Execution | `systemd-run --user --scope` with `MemoryMax`, `MemorySwapMax=0`, `CPUQuota`, `Nice`, `OOMPolicy=stop` | direct spawn + `setpriority` |
| Hard RAM cap | yes (cgroup v2) | **no** — advisory |
| RSS sampling | `/proc/<pid>/status` | `ps -o rss=` |
| Free memory | `/proc/meminfo` `MemAvailable` | `sysctl hw.memsize` + `vm_stat` |
| Boot service | systemd user unit + linger | launchd agent |

macOS cannot hard-cap a single process's memory: there is no cgroup write access,
and `RLIMIT_AS` counts virtual address space so it breaks Node, the JVM and
Docker outright. There, the free-RAM admission gate does the real work, plus a
watchdog kill when RSS exceeds the cap. `ajq doctor` names the active backend and
says so rather than implying parity.

## Harness integration

| Harness | Skill | Hooks | Tool guard |
|---|---|---|---|
| OpenCode | yes | plugin `~/.config/opencode/plugins/ajq.js` | yes, plus native `ajq_submit` / `ajq_status` tools |
| Claude Code | yes | `SessionStart` + `PreToolUse Bash` in `settings.json` | yes |
| Codex | yes | `SessionStart` + `PreToolUse Bash` in `hooks.json` | yes |
| Kiro | yes | `~/.kiro/hooks/ajq.json` (`SessionStart`, `PreToolUse`) | yes |
| Pi | yes | extension (see caveat) | skill only |
| Kilo, Kimi, DeepSeek, Cursor | yes | — | skill only |

Hooks do two things: at session start they ensure the daemon is up and inject the
queue snapshot, and on `PreToolUse` for Bash they classify the command and point
the agent at `ajq submit`. The guard mode is `warn` by default — it tells the
agent, it does not block. Set `hooks.guard_mode` to `block` to deny heavy
commands outright, or `off` to disable. A hook never blocks when the daemon is
unreachable or when the command already goes through `ajq`.

JSON files the installer does not own are **merged, never rewritten**: only
entries carrying an ajq marker are removed and re-added, so hooks from other
tools (herdr, plannotator, …) and every unrelated setting survive install,
update and uninstall. A backup is written first and the result is validated
before it replaces the original.

Caveats found while building this: OpenCode v2.0.18 does not dispatch
`tui.toast.show` (the plugin logs instead, and stays forward-compatible), and the
`pi` binary in use is a Go port with no JavaScript extension loader, so the pi
extension only takes effect on the Node build — the skill covers pi either way.

## Configuration

`~/.config/ajq/config.json`, plain JSON. Precedence: **submit flags > `AJQ_*`
environment > user config > built-in defaults.**

```json
{
  "limits": { "max_concurrent": 8,
              "pools": { "heavy": 2, "normal": 4, "service": 3, "light": 6 } },
  "defaults": { "timeout_s": 1800, "max_output_bytes": 8388608, "pool": "auto",
                "kind": "auto", "kill_grace_s": 10, "priority": 0 },
  "resources": { "memory_mb": 2048, "cpu_percent": 200, "nice": 10,
                 "backend": "auto", "memory_headroom": 1.2 },
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
output files stay readable. Jobs are argv-only by default — the daemon never
runs `sh -c` unless you pass `--shell`.

## Development

```bash
python3 -m unittest discover -s tests -t tests   # 107 tests, no daemon required
./install.sh --dry-run --all                     # preview an install
```

The test suite never starts a real daemon (`AJQ_NO_AUTOSTART=1`) and installs
into a throwaway `$HOME`, so it cannot touch your machine.