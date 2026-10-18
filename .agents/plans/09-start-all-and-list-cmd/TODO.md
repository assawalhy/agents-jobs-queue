# TODO — `ajq start` and the list command column

- [x] scheduler.py: `start_all_plan()` and `start_all()`
- [x] protocol.py: add `start_all` to `OPS`
- [x] daemon.py: `_op_start_all` with `dry_run` and `ignore_serial`
- [x] cli.py: `_trim`, `_cmd_width` arg, `cmd` column in `ajq list`
- [x] cli.py: `ajq start` (alias `start-all`) with the risk prompt
- [x] tests/test_start_all.py: plan, serial kept, --ignore-serial, caps ignored
- [x] tests: parser, trimmer and list row
- [x] docs/DETAILS.md, skills/ajq/SKILL.md, README.md
- [x] verify: touched modules, then full suite and compileall

## Follow-up (18 Oct)

- [x] cli.py: `_cmd_text` (shell script, leading `cd ... &&` chain dropped)
- [x] cli.py: `_cmd_text` used by the list row, the job block and the start plan
- [x] cli.py: `cmd` selectable through `--fields`/`--select` on status, list, wait
- [x] cli.py: `_print_fields` projects, so the plain form derives `cmd` too
- [x] cli.py: `ajq start` names `--ignore-serial` when rows stay blocked, and
      starts nothing without prompting when the plan is empty for that reason
- [x] guard.py: `submit_line()` turns a `cd` prefix into `--cwd`, adds `--shell`
      when the command needs one, and quotes the words for a shell
- [x] hooks + plugin: both read the CLI's `suggest:` line instead of rebuilding it
- [x] teach `--cwd`: `ajq_submit` descriptions, session-start text (opencode,
      claude, codex, kiro, pi), skills/ajq/SKILL.md, docs/DETAILS.md
- [x] tests: unwrap rules, `--select cmd` in both forms, the empty-plan hint,
      the guard suggestion, the plugin using the CLI's line
- [x] docs/DETAILS.md + skills/ajq/SKILL.md: the render rule and `--select cmd`
- [x] verify: 199 python tests, 25 node tests, compileall, `node --check`
- [x] verify live: `./install.sh --all --no-daemon` (CLI only, no daemon restart,
      no queue drain), then `ajq list`, `--select cmd`, `status -v`, `guard
      --explain` and a wrapped `--shell` job on the real daemon

## Observed, not fixed here

- A wrapped job is classified by its wrapper: `guard --explain` reports
  `tool: sh` (or `tool: cd`) and the estimate signature is `check|sh|root|1`, so
  every shell job in a repository shares one estimate row. Unwrapping the argv
  before `_tool_for` would give `tool: bunx:vitest` and per-tool estimates, but
  it changes every cached signature, so it is a separate decision.
