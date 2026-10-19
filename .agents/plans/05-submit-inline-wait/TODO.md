# TODO — `ajq_submit` returns the answer for cheap jobs

- [x] `runSubmit` in `harnesses/opencode/ajq.js`: `ajq --json submit`, parse
      `id` / `eta_start_s` / `eta_run_s`; when the sum is within `wait_s` (default
      20, clamp 120, 0 disables) run `ajq wait <id> --tail 40` and return state + log,
      else return `<id>  state ...  pool ...  eta_run Ns`; unparseable JSON hands back
      the raw line (never a second submit)
      (check: `node --check harnesses/opencode/ajq.js`)
- [x] `submitInput()` gains `wait_s`, and `SUBMIT_DESCRIPTION` says a cheap job
      comes back in the same call
      (check: schema renders, plugin tests still load)
- [x] `tests/plugin_opencode.test.mjs`: stub answers `--json submit`; five tests cover
      cheap waits, slow returns the id, queued-behind-others skips the wait, `wait_s: 0`,
      and the session-directory submit
      (check: `node --test tests/plugin_opencode.test.mjs` — 18 pass, 0 fail)
- [x] Docs: `skills/ajq/SKILL.md` waiting section documents `wait_s` (default, max, 0
      opt-out, and the queued-behind-others case); `docs/DETAILS.md` harness section
      documents the inline wait, and the guards section now says the config `resources`
      block is what the backend is built from
      (check: both mention the default and the 0 opt-out)
- [x] Install: `~/.config/opencode/plugins/ajq.js` copied from the repo (was
      byte-identical before), and OpenCode reloaded the plugin on its own — the live
      tool picked up `wait_s` without a restart
      (check: live `ajq_submit` of `echo inline-wait-final && echo second-line` with
      `wait_s: 60` returned `done` + both lines in ONE call; `wait_s` omitted returned
      `j-…  state queued  pool light  eta_run 38s`)