# ajq — details

Reference for the guards, the config, the CLI and the harness integrations. The
README covers what it is and how to install it.

## Guards and defaults

| Guard | Default | Behaviour |
| --- | --- | --- |
| Concurrency | `max_concurrent: 8` | Caps every pool at once |
| Pools | `heavy 2 · normal 4 · service 3 · light 6` | One cap per tier; the tier comes from the job's kind and estimate |
| Worktree serialization | `serial_key` = git worktree root | Two agents in one checkout never run overlapping commands |
| Memory admission | `memory_headroom: 1.2`, `swap_credit: 1.0` | A job starts only when `MemAvailable + zram credit >= memory_limit × 1.2`. With nothing running, the head job starts anyway |
| Pressure gate | `admission: psi_memory_max 5, psi_cpu_max 70, temp_max_c 95` | While a job runs, no further job starts while PSI memory `full avg10`, PSI CPU `some avg10`, or the 30 s average CPU temperature is over its limit. Running jobs are never killed for it |
| Per-job memory | `memory_mb: 2048` (or `pool_memory_mb[pool]`), `hard_factor: 1.5` | Linux, cgroup v2 with zram as the first swap: `MemoryHigh` at the cap (reclaim to zram, no kill), `MemoryMax` at cap × 1.5, `MemorySwapMax` at the cap. Otherwise `MemoryMax` at the cap with `MemorySwapMax=0`. `OOMPolicy=stop` in both. The RSS watchdog kills at the same ceiling as `MemoryMax` |
| CPU | `cpu_percent: 200`, `nice: 10` | Linux: `CPUQuota` + `Nice`. Both: `setpriority` |
| Timeout | `timeout_s: 1800` | SIGTERM to the process group, SIGKILL after `kill_grace_s` (10s), state `timeout` |
| Output size | `max_output_bytes: 8 MiB` | Appends a truncation marker and terminates the job. `--keep-going-on-output` only stops recording |
| Forced start | `ajq start` | Starts the whole queued backlog at once: the pool caps, `max_concurrent` and the admission gate are bypassed for that call, and the per-job `MemoryMax`/`CPUQuota` still apply. It prompts first unless `--yes`. The worktree serial key still holds unless `--ignore-serial` |

Every guard is overridable per submit (`--timeout`, `--max-output-bytes`,
`--pool`, `--priority`, `--memory-mb`, `--cpu-percent`) or in
`~/.config/ajq/config.json`. The config `resources` block is what the daemon
builds its backend from. A job's memory cap is its `--memory-mb`, else
`resources.pool_memory_mb[pool]`, else `resources.memory_mb`; that same number
is the cgroup cap the job is given (`MemoryHigh` or `MemoryMax`, see below) and the number admission is checked
against. Raise it for jobs that a plain `typecheck + coverage` run outgrows. The
daemon must be restarted for a resource change to reach a running daemon (unlike
`hooks.guard_mode`, which is cached).

## Commands

| Command | Purpose |
| --- | --- |
| `ajq submit [--kind K] [--pool P] [--timeout S] [--priority N] [--serial-key K] [--cwd D] [--wait] -- cmd args…` | Queue a command (`--shell` to run a shell string). Pass the directory with `--cwd`, never as a `cd <dir> &&` prefix |
| `ajq status <id> [--json] [--fields a,b] [-v]` | State, queue position, elapsed, ETAs, output path and size, exit code |
| `ajq list [--all] [--json] [--fields a,b] [--cmd-width N]` | Queued + running, or everything; the last column is the start of each command, trimmed to `N` characters with `...` (default 48, `0` for the full command) |
| `ajq start [--yes] [--dry-run] [--ignore-serial] [--cmd-width N]` (alias `ajq start-all`) | Start every queued job now, over the pool caps, `max_concurrent` and the admission gate, after printing the reserved memory and CPU and asking for confirmation |
| `ajq output <id> [--tail N\|--head N] [--offset N] [--follow] [--from-start]` | The recorded output: a tail (default 40), a head, or an offset window |
| `ajq wait <id> [--tail N] [--timeout S] [--fields a,b]` | Block until terminal; `--tail` prints the last N log lines so one call returns state + log; exit 0 only for `done` |
| `ajq cancel <id> [--no-wait]` (alias `ajq kill`) | Stop a queued or running job; waits (bounded by its kill grace) for the final state |
| `ajq stats [--clear]` | Estimate table with per-signature MAPE |
| `ajq guard --explain "<cmd>"` | Why a command is heavy or light, and the `ajq` equivalent |
| `ajq config [--print-default\|--seed\|--force-config]` | Inspect or seed the config |
| `ajq doctor` | Backend, socket, unit state, linger, memory, guard mode |
| `ajq prune [--older-than D\|--all] [--keep-files] [--yes]` | Delete finished jobs and their captured output |
| `ajq daemon serve\|ensure\|status\|stop` | Daemon control |

### The terminal view

Running `ajq` with no subcommand in a terminal opens a curses view of the
queue. It is the same queue as `ajq list`, plus the output of the selected
job, and the keys are fixed:

| Key | Action |
| --- | --- |
| Up / Down | Move the job selection |
| `j` / `k` | Scroll the log one line |
| `g` / `G` | Log top / log bottom; `G` resumes following |
| PgUp / PgDn, `Ctrl-u` / `Ctrl-d`, space | Page the log |
| `/`, `n` / `N`, `Esc` | Search the log, next match, previous match, clear |
| `a` | Show or hide the finished jobs |
| `q` | Quit |

The wheel scrolls the log when the pointer is over it and moves the
selection when the pointer is over the table, and a click on a row picks
that job. The view opens only when both stdin and stdout are terminals, so
an agent that calls bare `ajq` still gets the help text.

The log pane draws the colours the tool produced. The daemon starts every
job with the colour environment variables set, keeps the raw bytes in
`color.log` and the same bytes with the escape sequences removed in
`out.log`, and the view reads the raw one. The agents read the plain one,
so a captured log never spends tokens on escape codes.


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

`cmd` is the one derived key: it is how ajq renders a job's command, so
`--select cmd` and the last `ajq list` column show the same text. A shell job is
rendered as its script with a leading `cd <dir> &&` chain dropped, because the
daemon already runs the job in its own cwd and the wrapper would otherwise fill
the column with `/bin/sh -c 'cd /home/...`:

```console
$ ajq list --state queued --cmd-width 68
id               state      pool      kind         elapsed        est   eta_start  out     cmd
j-2432fced930a   queued#5   heavy     test           3m35s      4m36s      18m12s  0B      bunx vitest run tests/contexts/sales/presentation/leads-screen-fi...
j-9814a06cc8d0   queued#4   heavy     test           6m12s      4m37s      13m34s  0B      bunx vitest run tests/contexts/sales/presentation/quotation-answe...

$ ajq list --select cmd --state queued
cmd=bunx vitest run tests/contexts/sales/presentation/leads-screen-filters.test.tsx tests/contexts/sales/presentation/leads-screen-probability.test.tsx tests/contexts/sales/presentation/sales-screens.test.tsx > /tmp/claude-1000/leads.log 2>&1
```

The `cmd` key is not trimmed, so a machine asking for it gets the whole command;
`--cmd-width` applies to the table only. Nothing is rewritten beyond that prefix:
`status --verbose` prints the render on the `cmd` line and, when the two differ,
the exact argv on a `raw` line, and `--json` always carries the untouched `argv`.
Submit with `--cwd <dir>` rather than a `cd <dir> &&` prefix: the prefix buys
nothing (the daemon runs each job in one directory) and it is what pushes the
command out of the column. `ajq guard --explain` turns that prefix into `--cwd`
in the line it suggests.

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
| Execution | `systemd-run --user --scope` with `MemoryHigh` + `MemoryMax` + `MemorySwapMax` (soft cap) or `MemoryMax` + `MemorySwapMax=0` (hard cap), `CPUQuota`, `Nice`, `OOMPolicy=stop` | direct spawn + `setpriority` |
| Hard RAM cap | yes (cgroup v2) | **no** — advisory |
| RSS sampling | `/proc/<pid>/status` | `ps -o rss=` |
| Free memory | `/proc/meminfo` `MemAvailable` + zram credit from `/proc/swaps` and `/sys/block/zram*/mm_stat` | `sysctl hw.memsize` + `vm_stat` |
| Soft memory cap | cgroup v2 with the memory controller delegated to `user@<uid>.service`, and zram as the first swap | no |
| Pressure gate | `/proc/pressure/{memory,cpu}`, CPU sensor in `/sys/class/thermal` or `/sys/class/hwmon` | off |
| Boot service | systemd user unit + linger | launchd agent |

macOS cannot hard-cap a single process's memory: there is no cgroup write
access, and `RLIMIT_AS` counts virtual address space so it breaks Node, the JVM
and Docker outright. There, the free-RAM admission gate does the real work,
plus a watchdog kill when RSS exceeds the cap. `ajq doctor` names the active
backend and says so rather than implying parity.

## Harness integration

| Harness | Skill | Hooks | Tool guard |
| --- | --- | --- | --- |
| OpenCode | yes | plugin `~/.config/opencode/plugins/ajq.js` (V2 API, verified on 2.0.26) | yes, plus native `ajq_submit` / `ajq_status` / `ajq_output` / `ajq_wait` / `ajq_cancel` tools |
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

`ajq_submit` submits with `--json` and reads the daemon's own `eta_start_s` and
`eta_run_s`: when the job is expected to be over inside `wait_s` (default 20 s,
max 120, `0` disables) it waits and returns the final state plus the log tail in
that one call, so a cheap lint or check never needs an `ajq_wait` follow-up.
Anything longer — or a job still queued behind others — comes back as the job id.
The cost is one extra ~30 ms `ajq` spawn per submit.

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
only when it is absent and never overwrites it on update. Dotted key paths are
accepted as flat keys too, so `"limits.max_concurrent": 8` and a nested
`{"limits": {"max_concurrent": 8}}` are the same.

```json
{
  "limits": {
    "max_concurrent": 8,
    "pools": { "heavy": 2, "normal": 4, "service": 3, "light": 6 }
  },
  "defaults": {
    "timeout_s": 1800, "max_output_bytes": 8388608, "pool": "auto",
    "kind": "auto", "kill_grace_s": 10, "priority": 0,
    "serial_key": "auto", "shell": false
  },
  "resources": {
    "memory_mb": 2048, "pool_memory_mb": {},
    "cpu_percent": 200, "nice": 10, "backend": "auto",
    "memory_headroom": 1.2, "swap_credit": 1.0,
    "hard_factor": 1.5, "swap_mb": null,
    "admission": { "psi_memory_max": 5, "psi_cpu_max": 70, "temp_max_c": 95 },
    "extra_args": []
  },
  "estimates": {
    "enabled": true, "light_threshold_s": 30, "sigma_weight": 0.5,
    "cold_defaults_s": { "build": 300, "test": 120, "typecheck": 90,
                         "lint": 20, "format": 10, "install": 240,
                         "docker": 600, "check": 30, "unknown": 60 }
  },
  "hooks": { "guard_mode": "warn" },
  "daemon": { "unit": "ajqd.service", "tick_s": 0.5, "socket": null },
  "output": { "color": "on" }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `limits.max_concurrent` | 8 | Global cap across every pool at once |
| `limits.pools.<tier>` | heavy 2 / normal 4 / service 3 / light 6 | Per-tier concurrency cap |
| `defaults.timeout_s` | 1800 | SIGTERM the job after this many seconds, SIGKILL after `kill_grace_s` |
| `defaults.max_output_bytes` | 8388608 (8 MiB) | Truncate and stop the job past this |
| `defaults.pool` | `auto` | Pool when none is given; `auto` classifies from kind and estimate |
| `defaults.kind` | `auto` | Kind when none is given; `auto` classifies from the command |
| `defaults.kill_grace_s` | 10 | Seconds between SIGTERM and SIGKILL |
| `defaults.priority` | 0 | Queue priority; higher first, FIFO within a priority |
| `defaults.serial_key` | `auto` | Worktree serialization key; `auto` uses the git root, `none` opts out |
| `defaults.shell` | false | Run the command through `sh -c` instead of argv |
| `resources.memory_mb` | 2048 | Default per-job memory cap in MB (cgroup cap and admission reservation), and the fallback for a pool absent from `pool_memory_mb` |
| `resources.pool_memory_mb` | `{}` | Per-pool memory cap in MB, e.g. `{"heavy": 6144, "light": 1024}`; overrides `memory_mb` for that tier |
| `resources.hard_factor` | 1.5 | Kill ceiling multiplier for the soft cap: `MemoryMax` and the RSS watchdog sit at cap × `hard_factor`. Ignored with the hard cap |
| `resources.swap_mb` | null | null: soft cap with `MemorySwapMax` = cap when zram is the first swap, hard cap otherwise. A number > 0 forces the soft cap with that swap limit. 0 forces the hard cap |
| `resources.swap_credit` | 1.0 | Fraction of the free zram credit added to `MemAvailable` for admission; 0 counts RAM only |
| `resources.admission.psi_memory_max` | 5 | Hold new jobs while `/proc/pressure/memory` `full avg10` is above this percent, unless nothing is running; 0 disables |
| `resources.admission.psi_cpu_max` | 70 | Hold new jobs while `/proc/pressure/cpu` `some avg10` is above this percent, unless nothing is running; 0 disables |
| `resources.admission.temp_max_c` | 95 | Hold new jobs while the 30 s average CPU temperature is above this, unless nothing is running; 0 disables |
| `resources.cpu_percent` | 200 | Linux `CPUQuota`; elsewhere the wall is advisory |
| `resources.nice` | 10 | `Nice` in the scope, or a `nice` wrapper when the scope refuses it |
| `resources.backend` | `auto` | `auto` / `linux` / `macos` / `posix` |
| `resources.memory_headroom` | 1.2 | Admission needs `MemAvailable + zram credit >= memory_mb × memory_headroom` |
| `resources.extra_args` | `[]` | Extra arguments appended to `systemd-run` on Linux |
| `estimates.enabled` | true | Record runtimes and serve estimates |
| `estimates.light_threshold_s` | 30 | A cheap job estimated under this promotes to the light pool |
| `estimates.sigma_weight` | 0.5 | σ multiplier in the warm estimate |
| `estimates.cold_defaults_s.<kind>` | per kind | Cold-start estimate per kind, scaled by file count |
| `hooks.guard_mode` | `warn` | PreToolUse guard: `warn` / `block` / `off` |
| `daemon.unit` | `ajqd.service` | systemd unit name |
| `daemon.tick_s` | 0.5 | Scheduler tick interval |
| `daemon.socket` | null | Socket path override; null uses `/run/user/<uid>/ajq/ajqd.sock` |
| `output.color` | `on` | Start every job with the colour environment variables, so the tool writes escape codes the terminal view can draw. `off` leaves the environment alone. The daemon reads it at start-up, so a change needs `ajq daemon stop` and `ajq daemon ensure` |

### Memory, admission, and concurrency

`tick()` checks each queued job in queue order. A job starts when every check passes:

```
running < limits.max_concurrent
pool running < limits.pools[job.pool]
job.serial_key not running
--- the checks below are skipped when ajq runs no job (progress rule) ---
PSI memory full avg10 <= admission.psi_memory_max
PSI cpu some avg10 <= admission.psi_cpu_max
CPU temperature (30 s average) <= admission.temp_max_c
MemAvailable + zram credit × swap_credit >= job.memory_mb × memory_headroom
```

`job.memory_mb` is the `--memory-mb` override, else `resources.pool_memory_mb[job.pool]`, else `resources.memory_mb`.
Inside one tick, each admitted job subtracts its own `memory_mb` from the budget before the next is tested, so N jobs of size M on one reading need about `M × (N - 1 + headroom)` of budget.

The zram credit is the RAM that swapping more pages to zram can still free: for each zram swap device, `(disksize - orig_data_size) × (1 - mem_used_total / orig_data_size)`, limited by the device's `mem_limit`.
The credit is 0 when any disk swap has a higher priority than zram, because the kernel would page to disk first.
Disk swap adds no credit, because swapping to disk is what makes the desktop stall.

Memory PSI uses the `full` line (every non-idle task stalled on memory), because `some` also rises when the kernel throttles one ajq job at its `MemoryHigh`, and that would hold unrelated light jobs.

The progress rule exists because a static reservation can exceed what the box will ever free while other programs hold memory.
Without it, a 6144 MB heavy cap at headroom 1.2 needs 7373 MB of budget, and on a desktop with 5 GB `MemAvailable` the heavy pool never starts.
With it, the head job starts whenever ajq runs nothing, whatever the readings say.

The pressure gate only holds new jobs. A running job is never killed for PSI or temperature.
`ajq list` and `ajq status` show the first failing check as `blocked`, e.g. `memory 7373>5446MB`, `pool heavy 2/2`, `temp 97C>95C`.
`ajq doctor` prints the current PSI, CPU temperature and zram credit next to their limits.

On Linux the cap takes one of two forms.

- Soft cap: cgroup v2 with the memory controller delegated to the user manager, and zram as the highest-priority swap (or `resources.swap_mb` > 0). The cap is `MemoryHigh`. Past it the kernel throttles the scope and reclaims its pages to zram, so a job that slightly outgrows its cap slows down instead of failing. `MemoryMax` at `memory_mb × hard_factor` is the hard ceiling where `OOMPolicy=stop` stops the scope, and `MemorySwapMax` limits the job's swap to `memory_mb` (or `swap_mb`).
- Hard cap: everywhere else. `MemoryMax` is the cap and `MemorySwapMax=0`, so the job is stopped at the cap. Without swap, `MemoryHigh` would throttle the job until its timeout, and with disk swap first it would page the desktop. `resources.swap_mb: 0` forces this form.

cgroup v1 and hybrid hierarchies (RHEL 7, Debian 10, Ubuntu 18.04 and 20.04 by default) ignore `MemoryHigh` and `MemorySwapMax`, and a user manager often has no memory controller there. `ajq doctor` prints `cgroup v2 memory no` and a note, and the RSS watchdog at the cap is the enforcement.
On macOS and on `linux-posix-fallback` the RSS watchdog kills at the cap, the same number admission reserves.

A pool cap is a ceiling, not a promise. `limits.pools.heavy: 2` runs two heavy jobs only when the memory and pressure checks admit both.

Environment overrides: `AJQ_MAX_CONCURRENT`, `AJQ_POOL`, `AJQ_TIMEOUT_S`,
`AJQ_MAX_OUTPUT_BYTES`, `AJQ_GUARD_MODE`, `AJQ_MEMORY_MB`, `AJQ_CPU_PERCENT`,
`AJQ_NICE`, `AJQ_BACKEND`, `AJQ_LIGHT_THRESHOLD_S`, `AJQ_KILL_GRACE_S`.
`AJQ_NO_AUTOSTART=1` stops `ensure` from starting a daemon (used by the tests).

### Forced start

`ajq start` starts every queued job now, whatever the caps say. It is the
escape hatch for a backlog that admission, a pool cap or a worktree key is
holding while nothing is actually running on the box.

```
$ ajq start
3 of 3 queued job(s) start now, over the pool caps, max_concurrent 8 and the memory admission gate
memory  18432MB reserved against 7900MB available (51300MB total, 2.3x available)
cpu     600% against 1600% (16 cpus)
pools   heavy 3/2, light 0/6, normal 0/4, service 0/3
note    every job keeps its own memory and CPU cap, so the risk is aggregate pressure; a job the kernel kills reports failed with kill_reason memory_limit
job  j-9f31c0a7d2e4   heavy       6144MB    200%  ./gradlew test --tests '*RepoImplTest'
job  j-4c1e77b0a915   heavy       6144MB    200%  npm run build
job  j-7d20f5c8b3a1   heavy       6144MB    200%  pytest tests/api -q
risk    reserved memory is 10532MB over available: the kernel can OOM-kill jobs in this burst
start them anyway? [y/N]
```

A queue held by the worktree key alone prints the blocked count instead:

```
2 of 3 queued job(s) start now, over the pool caps, max_concurrent 8 and the memory admission gate
serial  1 job(s) stay queued: their worktree already runs one
```

Rules:

- The plan and the start use the same resolution code, so the memory and CPU
  figures are the per-pool `memory_mb` and `cpu_percent` the jobs will really
  carry, not the raw flags.
- Every job keeps its cgroup `MemoryMax`, `CPUQuota` and timeout. A burst over
  the RAM the box has is a burst the kernel OOM-kills, and those jobs end
  `failed` with `kill_reason memory_limit`.
- The worktree serial key is the one gate a forced start keeps, because two jobs
  in one checkout write the same `target/`, `.next/` and lockfiles. Use
  `--ignore-serial` to override that too; the prompt then reports zero blocked
  rows, and when every queued job is serial-blocked the command prints
  `nothing to start: every queued job shares a worktree with another
  (--ignore-serial starts them anyway)` instead of asking for a yes that would
  start nothing.
- `--dry-run` prints the plan and starts nothing. `--json` prints the plan and
  starts nothing unless `--yes` is also given, so a machine-readable call cannot
  oversubscribe the box by accident. `--yes` alone skips the prompt and starts.
- The command exits 0 when the backlog was empty, when the plan was only
  printed, and when jobs started. It exits 1 only when a request failed.

## State on disk

```
~/.local/state/ajq/state.db                       SQLite (WAL): jobs, estimates
~/.local/state/ajq/jobs/<id>/out.log              captured output, mode 0600
~/.local/state/ajq/jobs/<id>/color.log            the same bytes with the colours in
~/.local/state/ajq/jobs/<id>/meta.json            the same metadata as a sidecar
~/.local/state/ajq/ajqd.pid                        live daemon pid
~/.local/state/ajq/ajqd.lock                       the single-daemon flock
/run/user/<uid>/ajq/ajqd.sock                      control socket (dir 0700)
```

**One daemon per state dir.** `ajqd` holds an exclusive `flock` on `ajqd.lock`
for its lifetime, so a second daemon exits instead of sharing the queue — the
kernel releases the lock if it crashes. This is what makes `ajq cancel` reliable:
only one daemon owns the in-memory `_running` map, so a cancel cannot land on a
daemon that did not start the job. As a safety net, a cancel for a running job
with no local runner kills the process group recorded in `state.db`.

A daemon crash leaves `running` jobs marked `lost` on the next start; their
output files stay readable. `ajq prune` removes finished jobs and their output.

**Two log files per job.** `out.log` holds the captured output with the escape
sequences removed, so every reader gets plain text and an agent never spends
tokens on escape codes. `color.log` holds the same bytes as the child wrote
them, escape codes included, and the terminal view draws that one. The output
cap counts the raw bytes, and `ajq prune` removes both files with the job
directory.

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
- macOS has no PSI or portable CPU temperature source, so the pressure gate is off there.
- The admission reservation is the static cap, not the job's measured peak, so a small job in a big-cap pool reserves more than it uses.
- Kilo / Kimi / DeepSeek / Cursor get the skill but no hooks.
- The `pi` binary in use is a Go port without a JS extension loader.