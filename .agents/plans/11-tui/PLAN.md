# PLAN: interactive TUI on bare `ajq`

## Goal

Running `ajq` with no subcommand in a terminal opens a live view of the queue: running and queued jobs, an elapsed counter that ticks every second (`42s`, `1m 2s`, `1h 2m 3s`), the selected job's output, and `/` search inside that output.

## Approach

- New module `src/ajq/tui.py`, built on stdlib `curses` (the CLI ships as a stdlib-only zipapp, so no Textual/urwid).
- `cli.main`: no subcommand and both stdin and stdout are ttys → ensure the daemon, run `tui.run()`. Non-tty keeps printing help, so agents and scripts calling bare `ajq` see no change.
- Data: `{"op": "list", "states": ["queued","running"]}` every 1s over the existing socket. Elapsed is computed on each redraw from `started_at` (running) or `enqueued_at` (queued) with `time.time()`, so the counter ticks between polls.
- Output pane: reads the selected job's `out_path` incrementally (keeps a byte offset, appends new lines), follows the tail unless the user scrolled up.
- Layout: header line (counts, daemon socket), job table top ~40%, output pane below, status/prompt line at the bottom.

## Keys

- `j`/`k`/arrows: select job (job pane) or scroll (output pane); `Tab` switches pane.
- `g`/`G`, `PgUp`/`PgDn`: top/bottom, page; `G` in output re-enables follow.
- `/`: search prompt; `Enter` jumps to first match from the current line; `n`/`N` next/prev; `Esc` clears.
- `a`: toggle showing recent finished jobs (done/failed/cancelled) below the active ones.
- `q` or `Ctrl-C`: quit.

## Decisions

- curses, not a third-party library: zipapp is stdlib-only by design.
- TTY gate on bare `ajq`: the hooks/skill tell agents to call ajq; a curses screen in a non-interactive shell would hang them.
- Local elapsed computation: polling the daemon at 1 Hz to get a ticking counter would still jitter; local `now - anchor` is exact and identical to the daemon formula in `scheduler.py:600`.
- Time format `1h 2m 3s` (hours shown only when non-zero, seconds always shown). `ajq list` keeps its compact `_short` format.
- Search is a case-insensitive substring match (smartcase: an uppercase letter makes it case-sensitive), matches highlighted. Rejected regex: typing a `[` or `(` mid-query would error on every keystroke.
- Output buffer capped at the last 50,000 lines; the daemon already caps file size, this bounds memory and redraw cost.
- No `ajq tui` subcommand alias. Rejected for now: the request is bare `ajq`; adding one is a one-line change.

## Milestones

1. Pure helpers (clock format, elapsed anchor, search index, row text) with unit tests.
2. curses app: table, output pane, keys, search, resize.
3. `cli.main` wiring + README line.

## Risks

- Terminal resize and tiny terminals: handle `KEY_RESIZE`, clamp to a minimum, never write past the last column (curses raises on the bottom-right cell).
- Long lines in output: truncated to pane width, no wrapping (horizontal scroll left out).
- The curses loop itself is not unit tested; verification is the helper tests plus a manual run in a pty.
