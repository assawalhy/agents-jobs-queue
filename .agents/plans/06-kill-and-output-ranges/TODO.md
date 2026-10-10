# TODO — kill a job, and read a range of its output

## M1 single daemon (unblocks kill)
- [x] `daemon.py`: `_Server.startup()` takes an exclusive `flock` on
      `$STATE_DIR/ajqd.lock` before opening the Store/binding; raise
      "another ajqd is already serving" when held; keep the fd, release in
      `teardown()` (check: two `_Server().startup()` in one state dir → second raises)
- [x] `daemon.py`: `_another_instance_live()` is true when the lock is held, not
      just when the socket pings (check: a locked-but-unreachable daemon blocks a start)
- [x] `daemon.py`: remember the bound socket's `st_ino`; `teardown()` unlinks the
      path only when it still matches (check: a second daemon's socket survives our teardown)
- [x] `daemon.py`: `ensure_running()` starts the unit/plist and does not spawn
      detached when one is installed (check: no second daemon appears)
- [x] `install.sh`: systemd `Restart=always` → `on-failure`; launchd
      `KeepAlive` → `{SuccessfulExit=false}` (check: `grep Restart install.sh`)
- [x] tests: lock refusal, inode-safe teardown, no double-spawn

## M2 kill
- [x] `scheduler.cancel()`: running job with no local cancel event → kill its
      recorded process group (SIGTERM, grace, SIGKILL) (check: unit test)
- [x] `cli._cmd_cancel` + `daemon._op_cancel`: poll to terminal up to
      `kill_grace_s + 2s`, print the final state; exit 0 canceled / 2 still
      running; `--no-wait` opts out (check: running job → `canceled`, `signal 15`)
- [x] `cli`: `kill` alias for `cancel` (check: `ajq kill <id>` parses)
- [x] tests: cancel running reports canceled, queued instant, `--no-wait`, alias

## M3 output ranges
- [x] `cli._cmd_output`: `--head N` and `--offset N`; `--tail`/`--head` slice from
      `offset`; `--from-start` unchanged (check: head, offset+tail window)
- [x] tests: head, offset+tail, offset past EOF, head+offset, offset+from-start

## M4 surfaces + docs
- [x] `harnesses/opencode/ajq.js`: register `ajq_cancel` (id, wait); `ajq_output`
      gains `head`/`offset` (clamped like tail) (check: `node --check`)
- [x] `tests/plugin_opencode.test.mjs`: cancel tool + output range (check: node --test)
- [x] `skills/ajq/SKILL.md`, `README.md`, `docs/DETAILS.md`: kill/cancel, output
      ranges, single-daemon note (check: all three mention them)

## M5 verify + deploy
- [x] full suite: `python3 -m unittest discover -s tests -t tests` + `node --test tests/*.test.mjs`
- [x] rebuild the zipapp, reinstall the plugin, kill the stale daemons, restart
      ajqd, confirm exactly one daemon (check: `ajq doctor`, one listener)
- [x] live end-to-end: kill a running job in one call; read a head and an offset window
