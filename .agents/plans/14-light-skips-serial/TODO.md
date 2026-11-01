# TODO

- [x] Add `limits.serial_light` (default `false`) and `AJQ_SERIAL_LIGHT` to `src/ajq/config.py`
- [x] `_hold`: skip the `serial_key busy` check for a light job unless `limits.serial_light` is true
- [x] `admission_state` and `_busy_serials`: do not register a running light job in the busy set
- [x] Tests in `tests/test_scheduler.py`: light starts beside a running heavy job in one worktree, heavy still waits for heavy, a running light job does not block a heavy job, `serial_light: true` restores the wait
- [x] Test in `tests/test_start_all.py`: `blocked_by_serial` ignores light jobs
- [x] Test in `tests/test_config_guard.py`: the key has a default and a bool type
- [x] Update `docs/DETAILS.md` (serialization row, config table, forced start text) and `README.md` line 72
- [x] Final verify: affected test files, then the project's lint and typecheck
