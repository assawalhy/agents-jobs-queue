# TODO: fixed key layout and colored logs

- [x] `src/ajq/ansi.py`: the escape regexes and the streaming byte stripper
- [x] `src/ajq/paths.py`: `job_color_path`
- [x] `src/ajq/exec.py`: the pump writes raw bytes to `color.log` and stripped bytes to `out.log`
- [x] `src/ajq/backends/*`: `spawn(job, argv, env=None)`, systemd `-E`, POSIX `env=`
- [x] `src/ajq/daemon.py` and `src/ajq/config.py`: the `output.color` key and the child environment
- [x] `src/ajq/tui.py`: the fixed key layout, no `focus`
- [x] `src/ajq/tui.py`: SGR parsing, curses colour pairs, `Output` reads `color.log`
- [x] `tests/test_tui.py`: the new keys, the SGR parser, the `color.log` fallback
- [x] `tests/test_exec.py`: both log files, the stripper across a chunk boundary
- [x] `tests/test_ansi.py`: the stripper on its own
- [x] README row and `docs/DETAILS.md`: the config key and the two log files
- [x] PR #9 body: the key table and the colour note
- [x] Verify: the full suite through ajq, a manual run in tmux
