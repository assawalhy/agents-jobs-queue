---
name: ajq
description: Queue heavy tasks through the ajq jobs daemon instead of running them in the shell. Use when about to run a build, test, typecheck, lint, install, docker build, or any command expected to take more than ~30 seconds; and when checking on a queued job with ajq submit, ajq status, ajq wait, or ajq output. ajq caps concurrency, memory, CPU and output size so parallel coding agents cannot exhaust the machine.
---

# ajq — queue heavy tasks, never run them inline

`ajqd` runs heavy commands under memory/CPU/output caps and starts them only when
the machine has room. `ajq submit` is the only supported way to start one.

## Submit instead of running directly

Submit anything that builds, tests, typechecks, lints, installs, builds an image,
or is expected to take more than ~30 seconds:

```sh
ajq submit -- npm run build
ajq submit --label api-tests -- pytest tests/api -q
ajq submit --cwd ../other-worktree -- cargo build
```

Keep running these directly — they are milliseconds and the queue adds nothing:

`git status` / `diff` / `log`, `rg`, `grep`, `ls`, `cat`, `head`, `tail`, `jq`,
`wc`, `find`, `echo`, `pwd`, `which`.

Not sure how a command is classified?

```sh
ajq guard --explain "npm run build"
```

## Flags that matter

| flag | why |
| --- | --- |
| `--timeout S` | kill the job after S seconds (default 1800). Raise it for slow suites; do not raise it to hide a hang. |
| `--max-output-bytes N` | output cap, default 8 MiB. A job that passes it is **terminated**, not truncated. Raise it for chatty suites; never drop it. |
| `--kind K` | `build test typecheck lint format install docker check`, or leave `auto`. Affects the pool and the duration estimate. |
| `--pool P` | `heavy normal service light`. Use `service` for dev servers and watch processes. Leave `auto` otherwise. |
| `--priority N` | higher runs first inside a pool. Use it to unblock a job the user is waiting on. |
| `--serial-key auto\|none\|NAME` | `auto` (default) serializes by git worktree so two agents in one worktree never overlap. `none` opts out; a name groups unrelated dirs. |
| `--shell` | run through `$SHELL`; needed for pipes, `&&`, globs, and env prefixes. |
| `--wait` | block until the job is terminal, then print it. Convenient for a quick job; prefer submit-then-poll for anything slow. |
| `--memory-mb N`, `--cpu-percent N` | per-job overrides of the global caps. |
| `--cwd DIR`, `--label NAME`, `--agent NAME` | where to run it, and how it shows up in `ajq list`. |

## Reading a job

```sh
ajq status <id> --json     # full job dict
ajq list                   # queued + running
ajq output <id> --tail 80  # captured output
ajq wait <id>              # block until terminal
ajq cancel <id>
```

Useful keys in the JSON: `state`, `queue_position`, `eta_start_s`, `eta_run_s`,
`eta_total_s`, `est_source`, `out_path`, `out_bytes`, `truncated`, `exit_code`,
`signal`, `kill_reason`. `exit_code` is only meaningful once the state is
terminal — a queued or running job still carries the zero value `0`.

What to do per state:

- `queued` — keep working. Poll `ajq status <id> --json`; `eta_start_s` is the
  wait, `queue_position` is the place.
- `running` — keep working. `elapsed_s` against `eta_run_s` says whether it is slow.
- `done` — read `ajq output <id>`.
- `failed` — read the tail of `ajq output <id>` for the error, fix, resubmit.
- `timeout` — it hit `--timeout`. Re-run with a bigger `--timeout`, and prefer a
  narrower scope (one package, one test file) over a longer wait.
- `canceled` — someone cancelled it; resubmit if still needed.
- `lost` — the daemon restarted while it ran. Re-submit; the partial log is still
  in `out_path`.

## How scheduling behaves

Jobs are serialized per git worktree (`serial_key=auto` resolves
`git rev-parse --show-toplevel`), so two agents in one worktree never overlap and
never fight over `target/`, `.next/`, or a lockfile. Different worktrees run in
parallel up to the pool caps (`limits.pools`, `limits.max_concurrent`). Admission
also waits for available memory, so a busy machine delays jobs instead of
thrashing.

Dev servers and watch processes: `ajq submit --pool service -- npm run dev`.

## Waiting and streaming

Prefer `ajq wait <id>` over `sleep`. To watch a job as it runs:
`ajq output <id> --follow` (streams until the job is terminal). `--from-start`
replays the whole log instead of the last 40 lines.

## End to end

```sh
$ ajq submit --label api-tests -- pytest tests/api -q
j-9f31c0a7d2e4   queued#1   heavy    ...  eta_start 0s  eta_run 1m35s  out 0B
cmd pytest tests/api -q  kind test  serial /home/me/repo  cwd /home/me/repo

$ ajq status j-9f31c0a7d2e4 --json | head -c 200
{"state": "running", "elapsed_s": 12.0, "eta_run_s": 95.0, ...}

$ ajq wait j-9f31c0a7d2e4
j-9f31c0a7d2e4  failed  elapsed 1m41s
$ echo $?
2

$ ajq output j-9f31c0a7d2e4 --tail 20
FAILED tests/api/test_users.py::test_create - AssertionError: expected 201, got 500
```

Read the failure, fix the code, resubmit. Do not re-run the suite in the shell.

## Notes

- `ajq daemon ensure` starts the daemon if it is down; `ajq doctor` reports the
  backend, socket, unit and caps. `ajq config --print-default` prints the
  annotated default config (`hooks.guard_mode` is `warn` by default: the
  PreToolUse guard warns, `block` denies, `off` is silent).
- The daemon is optional for `ajq config`, `ajq guard`, `ajq version`.
