# PLAN — `ajq start` (run the whole queue now) and a command column in `ajq list`

## Goal

1. A command that starts every queued job immediately, overriding the pool caps,
   `limits.max_concurrent` and the memory admission gate, after printing the
   aggregate CPU and memory the burst will demand and asking for confirmation.
2. `ajq list` shows the start of each job's command, trimmed with `...`, so a row
   can be identified without `ajq status <id> --verbose`.

## What is blocked today

There is no pause or hold in ajq. A `queued` job is held by exactly four gates
in `Scheduler.tick()` (`src/ajq/scheduler.py:216-253`): `limits.max_concurrent`,
the per-pool cap (`heavy 2`, `normal 4`, `service 3`, `light 6`), the worktree
serial key, and the admission rule `MemAvailable >= memory_mb * memory_headroom`.
So "start all pending tasks" can only mean a deliberate override of those gates,
and the only interesting output of the command is the resource cost of doing so.

## Decisions

- **Name `start`, alias `start-all`.** `ajq daemon start` does not exist (daemon
  actions are serve/run/ensure/stop/status), so the short name is free.
  Rejected: `release` (less literal), nesting under `ajq daemon` (daemon verbs are
  lifecycle, this is queue control).
- **Override caps and admission, keep worktree serialization; `--ignore-serial`
  opts into the literal all-at-once.** Two jobs in one checkout write the same
  `target/`, `.next/` and lockfile, which is the exact failure `serial_key`
  exists to prevent, so the default start count is "every queued job that no
  running or already-started job shares a worktree with". The prompt names how
  many rows stay blocked and why. Rejected: overriding serial by default, which
  turns one confirmation into two concurrent mutating builds in one checkout.
- **Baseline was locked without a user answer.** The question tool aborted twice
  and the user replied `go`, so the recommended option of each undecided pair is
  the one implemented (caps plus admission with serial kept; `start` with the
  `start-all` alias). Both are one-line changes if the intent was different.
- **A daemon op `start_all`, not a CLI loop.** One request re-reads the queue and
  starts what it saw, so the plan and the start cannot drift across N round
  trips. The op takes `dry_run` so the CLI gets its prompt numbers from the same
  resolution code that the start uses.
- **The aggregate is computed in the scheduler, not the CLI.** Per-job memory is
  resolved by `Scheduler._memory_for` from `pool_memory_mb` and the job's stored
  value, and a missing `cpu_percent` falls back to `resources.cpu_percent`.
  Rejected: summing fields from `ajq list --json` in the CLI, which would
  duplicate that resolution and drift from the daemon.
- **Per-job caps stay in force during the burst.** The jobs keep their cgroup
  `MemoryMax` and `CPUQuota`, so the risk is aggregate pressure and kernel OOM
  kills (job state `failed`, `kill_reason memory_limit`), not one unbounded
  process. The prompt states that.
- **Prompt by default; `--yes` skips it; `--dry-run` prints the plan and exits;
  `--json` prints the plan and starts only with `--yes`.** Rejected: `--json`
  implying `--yes` the way `prune` does, because an autonomous caller should not
  be able to burst the machine by parsing output.
- **`ajq list` gains a `cmd` column, appended last, trimmed to 48 characters
  with a trailing `...`; `--cmd-width N` overrides it and `0` prints the full
  command.** Last keeps the existing columns in place. Rejected: replacing the
  `kind` column (kind and pool drive estimates and caps) and a `--verbose`
  second line (`list` is a table).
- **No new OpenCode plugin tool.** Rejected: an `ajq_start` tool, because the
  caps exist to protect the machine from agents and a human escape hatch should
  not be one autonomous tool call away.

## Milestones

1. `scheduler.py`: `start_all_plan()` (startable set, serial-blocked count,
   memory and CPU totals, pool counts and caps) and `start_all()` (start them).
2. `daemon.py` + `protocol.py`: the `start_all` op with `dry_run` and
   `ignore_serial`.
3. `cli.py`: `ajq start` with `--yes`, `--dry-run`, `--ignore-serial`,
   `--cmd-width`; the risk prompt; `_trim` helper.
4. `cli.py`: the `cmd` column in `ajq list` plus `--cmd-width`.
5. Tests: `tests/test_start_all.py` (plan numbers, serial kept, `--ignore-serial`,
   caps ignored) and CLI tests for the parser, the trimmer and the list row.
6. Docs: `docs/DETAILS.md` command table and guards table, `skills/ajq/SKILL.md`,
   `README.md` what-you-get table.
7. Verify: run the touched test modules, then the full suite and
   `python3 -m compileall -q src/ajq`.

## Follow-up (found on the live queue, 18 Oct)

Reported: `ajq list --select cmd` prints `cmd=-`, and the trimmed column shows
`/bin/sh -c 'cd /home/assawalhy/orca/workspace...` so the row does not identify
its command. A third defect surfaced while checking: all five queued jobs share
one git root, so `ajq start` prints `0 of 5 queued job(s) start now` and does
nothing, with the flag that would fix it named nowhere.

Decisions:

- **A shell job is rendered by its script, with the leading `cd <dir> &&` chain
  dropped.** Harnesses submit `--shell` with `cd <worktree> && <command>`, and the
  daemon already runs in the job's `cwd`, so the prefix carries no information.
  The rule is a bounded regex strip (4 passes, quoted paths included) and it is
  the only rewriting: everything after the prefix stays verbatim, redirects
  included. Rejected: showing `label` instead (`test:sh` identifies less than the
  command) and parsing the whole script (a shell grammar in a display helper).
- **`cmd` becomes a selectable derived key**, so `--select cmd` and
  `--json --fields cmd` return the same text the column shows, untrimmed. It is
  derived client-side in `_project`, so the daemon payload and `list --json` do
  not grow. The raw `argv` stays in the payload for callers that need exactness.
  Rejected: adding `cmd` to `enrich()` server-side (bytes on every `list --json`,
  which agents poll) and leaving `--select cmd` broken.
- **`--cmd-width` trims the table only.** A machine asking for `cmd` wants the
  whole string; a table wants a column.
- **`ajq start` keeps the serial default, and says so out loud.** When some rows
  stay blocked, the serial line names `--ignore-serial`. When the plan is empty
  because of serialization, it prints the hint and starts nothing instead of
  prompting for a start that would do nothing. Rejected: overriding the serial
  key by default, which starts five mutating commands in one checkout; the live
  queue made that tempting, but it is the user's call per run.
- **Teach `--cwd` instead of `cd`, and keep the display strip only as a
  fallback.** The prefix exists because an agent wrote it, so the surfaces an
  agent reads now say the opposite: `ajq guard --explain` (whose suggestion
  became `ajq submit --cwd <dir> --shell -- <command>`), the `ajq_submit` tool
  descriptions for `command` and `cwd`, the session-start text in the OpenCode,
  claude, codex, kiro and pi hooks, and `skills/ajq/SKILL.md`. The strip stays
  for the rows already queued and for harnesses we do not control. Rejected:
  teaching alone (the existing queue still reads badly) and the strip alone
  (every new job arrives wrapped again).
- **One owner for the suggestion line.** The pre-tool-use shell hook and the
  OpenCode plugin both rebuilt it in their own language, so they disagreed with
  the CLI and never mentioned `--cwd`. Both now read the `suggest:` line from
  `ajq guard --explain`; the plugin keeps its old builder only as the fallback
  for a CLI that predates that line. Rejected: three parsers to keep in step.
- **The guard's suggestion must be a valid command.** It used to print
  `ajq submit -- cd /work/app && npm run build`, which a shell splits into two
  commands. It is now `--cwd` plus `--shell` when the command needs a shell
  (pipes, `&&`, redirects, globs), with the words quoted for the shell rather
  than for the daemon's argv parser.
- **The plain `key=value` form projects too.** `ajq list --select cmd` kept
  printing `cmd=-` after the JSON path was fixed, because `_print_fields` read
  keys straight off the row instead of going through `_project`. Both forms now
  answer the same question.

## Risks (original scope)

- A burst that exceeds real RAM gets jobs OOM-killed by the kernel. That is the
  documented risk of the command; the per-job caps and the prompt are the
  mitigation, and the prompt prints reserved MB and MB that cannot grow.
- `tick()` runs every `daemon.tick_s`, so a started job is never double-started
  (`store.start` moves the row to `running`), but a job that dies immediately
  after the burst can be replaced by a later tick only if it is still queued.
- Overridden caps mean the machine can be slower for everything else until the
  burst drains; the command prints no ETA because it cannot know one.
- The installed CLI is the zipapp at `~/.local/bin/ajq`, so the change only
  reaches the machine after `./install.sh --all` rebuilds it.
