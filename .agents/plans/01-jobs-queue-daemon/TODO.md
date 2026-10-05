# TODO — ajq jobs-queue daemon

## M1 core daemon + CLI
- [x] repo skeleton: `src/ajq/` package, stdlib only, no build step
- [x] `paths.py` (XDG dirs; macOS socket falls back to `$TMPDIR/ajq-$UID`, 0700)
- [x] `platform.py`: `available_memory_mb()` (`/proc/meminfo` | `sysctl`+`vm_stat`),
      cpu count, absolute interpreter path
- [x] `store.py`: SQLite WAL schema (jobs/estimates/meta), state transitions,
      `_add_missing_columns` migration, `exit_code`/`signal`/`pid` null until set
- [x] `protocol.py`: JSON-lines request/response over unix socket
- [x] `daemon.py`: serve loop, thread-per-connection, op dispatch, job reaper
- [x] `scheduler.py`: FIFO+priority, serial_key, pool caps incl. light tier,
      free-RAM admission, ETAs, meta sidecars
- [x] `backends/base.py`: `ResourceBackend` interface + `effective_resources`
- [x] `backends/linux.py`: `systemd-run --user --scope` (MemoryMax, MemorySwapMax=0,
      CPUQuota, Nice, OOMPolicy=stop); direct-spawn fallback when unusable
- [x] `backends/macos.py`: direct spawn, `os.setpriority`, RSS via `ps -o rss=`
- [x] `exec.py`: backend dispatch, timeout TERM→KILL grace, output cap + marker,
      `killpg`, `RunResult.pid`
- [x] `cli.py`: `submit`, `status`, `list`, `output`, `cancel`, `wait`, `stats`,
      `guard`, `config`, `doctor`, `daemon`
- [x] `ajq status` metadata: state, queue_position, elapsed, eta_start, eta_run,
      output path/bytes/truncated, exit_code/signal/kill_reason
- [x] crash recovery: leftover `running` → `lost` on daemon start
- [x] `__main__.py` so `python3 -m ajq` and the detached spawn work
- [x] tests: scheduler order/serial_key/pool caps/light tier, timeout, output cap,
      meta shape, backend selection + RSS watchdog kill (Linux and macOS paths)

## M2 estimates + guard
- [x] `guard.py`: command → kind/pool/heaviness table + `explain`, launcher
      unwrapping (`uv run`, `env FOO=1`, `nice -n 10`, `timeout 300`), bounded
      tool names, `python -c` never becomes a cache key
- [x] `estimate.py`: signature build (git changed files), Welford cache, clamping
- [x] `ajq stats`: per-signature n/mean/sigma/MAPE; `ajq estimates clear`
- [x] `config.py`: user-owned `~/.config/ajq/config.json` (seeded only when
      absent, `--force-config` to reseed) + precedence flags > env > file > defaults
- [x] `ajq doctor`: active backend, socket, unit/launchd state, linger, config
      path, memory, cpu count, guard mode
- [x] tests: classifier cases, launcher unwrapping, cold defaults, Welford
      update, ETA math, schema migration

## M3 installer
- [x] `install.sh` + `MANIFEST.txt` (awesome-agent conventions: `--all`,
      `--target`, `--no-daemon`, `--uninstall`, `--purge`, `--dry-run`)
- [x] Linux: systemd unit (`ajqd.service`, absolute interpreter, explicit PATH) + linger
- [x] macOS: launchd plist (`io.ajq.ajqd`) + `launchctl bootstrap gui/$UID`
- [x] `daemon ensure` (systemctl → launchctl → detached spawn); `AJQ_NO_AUTOSTART`
- [x] non-destructive JSON merge for claude `settings.json` + codex `hooks.json`
      (scrub by marker, backup, validate, restore on failure, idempotent)
- [x] uninstall removes unit/plist, hook entries, files, registry; `--purge` for state
- [x] tests: fake-HOME install preserves herdr + plannotator entries; uninstall
      clean; merge idempotent; unit uses absolute python

## M4 harness integrations
- [x] `skills/ajq/SKILL.md` (when to submit, flags, per-state actions, example)
- [x] opencode: `harnesses/opencode/ajq.js` — ensure daemon, queue context,
      `ajq_submit`/`ajq_status` custom tools, bash guard
- [x] claude: SessionStart hook + PreToolUse Bash guard (warn|block)
- [x] codex: same two hooks in `~/.codex/hooks.json`
- [x] kiro: `~/.kiro/hooks/ajq.json` v1 schema (SessionStart + PreToolUse)
- [x] pi: `harnesses/pi/ajq.ts` session_start ensure + context
- [x] shared shims `hooks/ajq-*.sh` (exit 2 blocks; exit 0 stdout → context)
- [x] tests: hook smoke tests (stdin JSON in, valid JSON/exit code out)

## M5 docs + final verification
- [x] `README.md`: install (Linux + macOS), CLI reference, harness matrix,
      config reference, platform limits
- [x] example session in the README
- [x] full test run green: `python3 -m unittest discover -s tests -t tests` → 98 OK
- [x] manual end-to-end on this machine: real systemd unit, cgroup-scoped jobs,
      timeout kill (`signal 15`), output capture, 2-worktree serialization
      (one job per worktree in parallel, queued within each)
- [x] live install verified against the real herdr + plannotator configs

## Known gaps (deliberate, documented)
- macOS has no hard per-process RAM cap; the admission gate plus an RSS watchdog
  is the protection. `ajq doctor` states this.
- Kilo / Kimi / DeepSeek / Cursor get the skill but no hooks (no verified hook API).
- OpenCode v2.0.18 does not dispatch `tui.toast.show`; the plugin logs instead.
- The `pi` binary in use is a Go port without a JS extension loader, so the pi
  extension only applies to the Node build.
- No `AGENTS.md` snippet was added to `~/.agents/AGENTS.md`; the skill and the
  session-start hook carry the same guidance. Say the word if you want it there.