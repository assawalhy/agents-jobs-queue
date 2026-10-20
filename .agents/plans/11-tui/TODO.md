# TODO: interactive TUI

- [x] `tui.py` helpers: `clock()`, `elapsed_of()`, `find_matches()`, row text
- [x] `tui.py` curses app: polling, job table, output pane with follow, keys
- [x] `/` search prompt, highlight, `n`/`N`
- [x] `a` toggle for finished jobs, resize handling
- [x] `cli.main`: bare `ajq` on a tty opens the TUI, else help
- [x] `tests/test_tui.py` for the helpers and the tty gate
- [x] README: one line under the command overview
- [x] Verify: scoped pytest for `test_tui.py` via ajq, manual pty run
