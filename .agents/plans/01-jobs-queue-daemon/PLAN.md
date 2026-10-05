# PLAN — ajq: a jobs-queue daemon for heavy agent tasks

## Goal

Heavy tasks (build, typecheck, test) run from many worktrees and parallel agents
fill RAM and CPU. `ajq` is a daemon that owns those jobs: one FIFO queue with
per-worktree serialization, pool caps, RAM/CPU admission control, timeouts and
output caps. Agents submit instead of blocking, and get back rich metadata
(state, queue position, elapsed, ETA to start, ETA to run, output path/size).
A skill + hooks make every known harness use it by default.

## Approach

Targets **Linux + macOS, POSIX only** (no Windows). One codebase, two backends.

```
agent (any harness)                ajqd (auto-started at login/boot)
  ajq submit --kind test -- pytest        ▲  JSON-lines over unix socket
  ajq status <id> --json                  │  $XDG_RUNTIME_DIR/ajq/ajqd.sock
  ajq output <id> --follow                ▼  (macOS: $TMPDIR/ajq-$UID/, 0700)
                                   scheduler: FIFO + serial_key(worktree)
                                   pools: heavy 2 | normal 4 | service 3 | light 6
                                   admission: free RAM >= mem_limit * 1.2
                                   exec backend (pluggable):
                                     linux  -> systemd-run --user --scope
                                                -p MemoryMax -p CPUQuota -p Nice
                                     macos  -> direct spawn + setpriority(10)
                                                + RSS watchdog via ps
                                   both: timeout TERM->KILL, output cap, killpg
                                   state: SQLite WAL ~/.local/state/ajq/state.db
                                   artifacts: ~/.local/state/ajq/jobs/<id>/out.log
                                   estimates: Welford cache keyed by
                                     kind|tool|dirs|filecount-bucket
```

Boot: Linux `~/.config/systemd/user/ajqd.service` + `loginctl enable-linger`
(Linger=no today). macOS `~/Library/LaunchAgents/io.ajq.ajqd.plist` +
`launchctl bootstrap gui/$UID`. Safety net on both: `ajq daemon ensure` starts the
daemon from any hook or from the first `ajq submit`.

## Decisions

- **Python 3.11+ stdlib only, no deps** — no build step, `sqlite3`/`subprocess`/
  `signal` in stdlib; matches the existing convention (herdr hooks embed python3).
  Rejected: Node (daemon needs none of its ecosystem), Go/Rust (toolchain dep in
  the installer), shell (too weak for scheduling + crash recovery).
- **Unit file uses the absolute interpreter path** — verified: the systemd user
  PATH here is `/nix-profile/bin:…:/run/current-system/sw/bin` and does *not*
  contain `~/.local/bin`, so `python3` would not resolve under systemd.
- **One socket, one daemon, jobs are its children** — a queue must own the
  ordering. Daemon crash ⇒ systemd restarts it and it marks leftover `running`
  jobs `lost` (unknown exit); output files stay readable.
- **serial_key defaults to the git worktree root** — two agents in one worktree
  can never run overlapping commands; different worktrees parallelize up to the
  pool caps. `--serial-key none|<s>` overrides.
- **Pools, and a `light` tier that never queues behind builds** — heavy 2,
  normal 4, service 3, light 6, global 8. Tier comes from the estimate at submit
  time (est < `light_threshold_s` = 30 and kind ∈ {lint, format, check} → light),
  so cheap work fans out in parallel while a build cannot starve it. Rejected:
  one flat cap (light jobs queue behind builds) and full RAM/CPU token
  accounting across pools (clever, hard to tune — the admission check already
  bounds the real risk). Caller can force a tier with `--pool`.
- **Linux and macOS, POSIX only — no Windows** — one queue/scheduler/store core,
  with the enforcement layer behind a `ResourceBackend` interface so each platform
  gets its real primitive. Rejected: one lowest-common-denominator path (would
  throw away cgroups on Linux, which is the whole point of the RAM guard).
- **Backend is pluggable and always reported** — `linux` = `systemd-run --user
  --scope` with `MemoryMax`+`MemorySwapMax=0`+`CPUQuota`+`Nice`+`OOMPolicy=stop`;
  `macos` = direct spawn, `os.setpriority`, and an RSS watchdog sampling
  `ps -o rss=` (macOS has no cgroup v2 write access and no per-process hard cap
  without SIP-hostile tricks). Timeout/output-cap/killpg are identical on both.
  `ajq doctor` prints the active backend; on macOS it prints the honest caveat
  that the memory cap is advisory. Rejected: `RLIMIT_AS` as a macOS memory cap —
  it counts virtual address space and breaks Node/JVM/Docker outright.
- **Free-RAM read is per-platform** — `/proc/meminfo` on Linux, `sysctl
  hw.memsize` + `vm_stat` on macOS, behind one `available_memory_mb()` helper.
  The admission gate (`free >= mem_limit * 1.2`) is therefore enforced on both,
  which is where most of the OOM protection actually comes from.
- **Config is user-owned at `~/.config/ajq/config.json`** — the installer seeds
  it only when absent and never overwrites it on update (`--force-config` to
  reseed); `ajq config --print-default` prints an annotated template. Precedence
  is fixed: **submit flags > env (`AJQ_*`) > user config > built-in defaults**.
  No TOML/YAML dependency either way — plain JSON, stdlib on both platforms.
- **Timeout 1800s default, overridable per job** — SIGTERM to the group, then
  SIGKILL after a 10s grace, state `timeout`.
- **Output cap 8 MiB default** — on exceed: append a marker, terminate, state
  `failed` with `truncated=1`. `--keep-going-on-output` only stops recording.
- **Estimates: Welford mean+sigma per signature, `mean + 0.5σ` clamped** — no
  ML, and `ajq stats` reports MAPE so "near-reality" is measurable, not claimed.
  Signature `kind|tool|toplevel-dirs|filecount-bucket`; changed files from
  `git status --porcelain -z` (2s cap). Rejected: per-repo multiplier (v1 keeps
  one table, `ajq stats` shows when a repo skews it).
- **No shell by default** — the daemon spawns argv, never `sh -c`, unless the
  caller explicitly passes `--shell`. Prevents quoting/injection surprises.
- **Guard mode default `warn`, not `block`** — a PreToolUse deny that fires
  while the daemon is unhealthy can wedge an agent; blocking is opt-in.
- **Installer merges JSON, never rewrites** — `jq`-based add/remove of only our
  own entries (identified by an `ajq` marker), backup + validate + auto-restore.
  Must preserve herdr's entries and the plannotator Stop hook.
- **Skill for every harness; hooks only where the API is verified** — opencode
  (plugin + custom tools), claude, codex, kiro, pi get hooks; kilo/kimi/
  deepseek/cursor get the skill only (no verified hook API, same as
  awesome-agent's `.skip` mechanism).
- **Follow awesome-agent's installer shape** — `install.sh` + `MANIFEST.txt` +
  registry, `--all/--target/--uninstall/--dry-run`. Prior art in
  `~/Projects/awesome-agent`; reuse its conventions rather than invent new ones.

## Milestones

- **M1 core** — daemon, SQLite store, scheduler, exec/limits/timeouts/output
  caps, CLI (`submit/status/list/output/cancel/wait/daemon`), unit tests.
- **M2 estimates + guard** — command classifier (kind/pool/heaviness), Welford
  cache, `ajq stats`, user-owned `~/.config/ajq/config.json` + env/flag overrides,
  `ajq doctor`.
- **M3 install** — `install.sh`, Linux systemd unit + linger, macOS LaunchAgent +
  `launchctl bootstrap`, `daemon ensure`, uninstall, JSON-merge regression tests.
- **M4 harnesses** — `skills/ajq/SKILL.md`, opencode plugin (+ `ajq_submit`/
  `ajq_status` tools), claude/codex/kiro hooks, pi extension, hook smoke tests.
- **M5 docs + verify** — README, example session, `~/.agents/AGENTS.md` snippet,
  full test run green.

## Risks

- **Hook blocking wedges an agent** — guard is `warn` by default; hook timeout
  5s; hook skips when the daemon is unhealthy; `ajq submit --now` escape hatch.
- **macOS cannot hard-cap a job's RAM** — no cgroup write access and `RLIMIT_AS`
  breaks Node/JVM/Docker. Mitigation: the admission gate is the real protection
  there, plus an RSS watchdog kill; `ajq doctor` states this instead of implying
  parity with Linux.
- **Boot start differs per platform** — Linux needs `loginctl enable-linger`
  (currently `Linger=no`), macOS needs `launchctl bootstrap gui/$UID`; both may be
  refused in locked-down setups, and the `daemon ensure` path covers that.
- **`systemd-run --scope` couples the Linux backend to systemd --user** — verified
  working here; if it ever fails, the backend falls back to direct spawn + RSS
  watchdog and reports the downgrade.
- **A second always-on daemon next to herdr** — deliberately not wired into
  herdr in this epic; only a skill/hook mention.
- **Guard heuristics misclassify** — classifier table lives in one config
  section, `ajq guard --explain <cmd>` shows the verdict for tuning.