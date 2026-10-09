# TODO — raise ajq's memory/output limits

- [x] `resources.memory_mb` 2048 -> 6144, `resources.memory_headroom` 1.2 -> 1.4,
      `defaults.max_output_bytes` 8388608 -> 16777216 in `~/.config/ajq/config.json`
      (check: `ajq config` prints the new values)
- [x] Confirm nothing is running (`ajq ls`), then restart the daemon
      (check: `ajq daemon status` active — note: the `stop && ensure` job killed
      itself, `kill_reason daemon_restart`, and systemd restarted ajqd. Unavoidable
      when the restart is issued from inside an ajq job.)
- [x] Submit a 4 GB allocation probe through ajq, no `--memory-mb`
      Before the fix: `j-f4d54389a763` killed at a 2.306 GiB peak, `signal 9`,
      journal `The kernel OOM killer killed some processes in this unit`.
      After the fix: `j-4f7cc9c837b1` printed `allocated 4GiB ok` with
      `systemctl --user show ajq-j-<id>.scope -p MemoryMax` = 6442450944 (6144 MiB).
- [x] Fix `src/ajq/daemon.py:467` — `get_backend(wanted)` never received the config, so
      `LinuxBackend` fell back to `backends/base.py:DEFAULT_MEMORY_MB` (2048) and the
      cgroup ignored `resources.memory_mb`. Now `get_backend(wanted, self.config)`
      (`_mapping` in base.py already unwraps `resources.*` from a Config-like object).
      Regression test `tests/test_exec.py::TestDaemonResourceWiring` — red before the
      fix (`AssertionError: 2048 != 6144`), green after. Whole suite: 128 pass.
      Deployed by rebuilding the zipapp exactly as `install.sh:build_zipapp` does
      (backup at `~/.local/bin/ajq.bak`), then restarting ajqd via a
      `systemd-run --user` helper so the restart outlives the job issuing it.
- [x] `skills/ajq/SKILL.md`: the `--memory-mb` row now says a full typecheck +
      coverage run outgrows the 2 GB default and how to raise it
- [x] Final: `ajq doctor` — backend linux-systemd, daemon alive pid 707427,
      memory total 31879M / available 13505M, max_concurrent 8