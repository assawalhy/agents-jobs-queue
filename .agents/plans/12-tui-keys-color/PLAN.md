# PLAN: fixed key layout and colored logs in the TUI

## Goal

- The TUI gets one fixed key layout: the job table and the log pane have separate keys, so `Tab` is not needed.
- The TUI shows the colours the tool produced, while `out.log` stays plain text for agents.

## Approach

### Keys

- `App.on_key` loses the `focus == "jobs"` / `"output"` branches. The `focus` attribute goes away.
- Arrows (Up/Down) move the job selection. `j`/`k` scroll the log one line. `g`/`G` jump to the log top/bottom, and `G` resumes following.
- PgUp/PgDn, `Ctrl-u`/`Ctrl-d` and space page the log. `/`, `n`, `N`, `Esc` search. `a` toggles finished jobs, `q` quits.
- `Tab`, `h`/`l`, Left/Right and Home/End are removed.
- The mouse handling stays: the wheel scrolls the pane under the pointer, a click on a job row selects it.

### Colour capture

- The daemon writes two files per job: `color.log` holds the child's raw bytes, `out.log` holds the same bytes with the escape sequences removed. `out.log` is the file agents read, so it stays plain.
- The output cap counts the raw bytes, and `out_bytes` counts them too, so the cap means the same thing as before.
- The child is started with the environment variables that make tools colour their output: `FORCE_COLOR=1`, `CLICOLOR_FORCE=1`, `CLICOLOR=1`, `PY_COLORS=1`, `CARGO_TERM_COLOR=always`, and `TERM=xterm-256color` when `TERM` is unset or `dumb`.
- A new config key `output.color` (`on` / `off`, default `on`, env `AJQ_OUTPUT_COLOR`) decides whether those variables are set. `NO_COLOR` in the daemon's own environment is not overridden.
- The backends pass the environment to the child: `systemd-run -E NAME=VALUE` on Linux, `env=` in `Popen` on the POSIX backend.
- The TUI reads `color.log` when it exists and `out.log` otherwise, parses the SGR sequences into segments, and draws them with curses attributes. The plain text of each line is kept next to the segments so the search still matches on plain text.
- 256-colour and truecolour values are mapped to the nearest of the 16 base colours when curses has fewer than 256 colours.

## Decisions

- Force the colour through the environment, not through a pty: a pty is the only way to cover tools that only check `isatty()`, but it changes the line buffering, breaks the `\r` progress output, and fights `systemd-run` and the process-group kill. The environment covers the tools that matter (bun, vitest, cargo, pytest 8, npm).
- Force the colour for every job, not only while a TUI is connected: a tool decides once at start-up, so "only while the TUI runs" would give the same command a different output depending on when it was submitted. The agent log is stripped either way, so the agent sees no difference.
- Two files, not one raw file with readers that strip: `out.log` stays plain by construction, so every reader (agents, hooks, `ajq output`, `ajq wait --tail`, a human with `cat`) is correct without touching it.
- The TUI parses the escape sequences itself and keeps the plain text beside them: the search, the match highlight and the match counter all work on plain text offsets.
- New module `src/ajq/ansi.py` for the escape regexes and the streaming stripper, shared by the daemon and the TUI.
- `color.log` is derived from the job id (`paths.job_color_path`), not a new database column, so the store schema does not change.

## Milestones

1. `ansi.py` and `paths.job_color_path`.
2. The daemon side: the pump writes both files, the backends pass the environment, the config key.
3. The TUI side: the fixed key layout, then the colour rendering.
4. Tests, README, `docs/DETAILS.md`, the PR body.

## Risks

- Tools that colour only when they see a terminal stay plain. There is no env variable for them; a pty would be the fix, and it is out of scope.
- Forcing colour makes the raw stream bigger, so a job hits the output cap a little earlier.
- Two log files per job double the disk the logs take. `ajq prune` removes the whole job directory, so both files go together.
- A tool that switches to a progress bar when it thinks it is on a terminal writes `\r` heavy output. `out.log` keeps the carriage returns, the TUI collapses them to the last segment.
- The SGR parser covers the common codes. Rare sequences (OSC 8 links, exotic backgrounds) are dropped or approximated.
- The curses loop is not unit tested. The colour rendering is checked through the parser and the drawing helpers, the rest through a manual run in tmux.

## Verification

- The full suite through ajq: 283 tests, OK.
- A manual run in tmux against a daemon running the working tree: the fixed keys, the search, the colours drawn from `color.log`, and `out.log` plain next to it.
- The output preview lag the user reported before this round is gone, confirmed by the user. The fixes rode along in the TUI file, because the new key layout replaced the code the lag fixes touched.
