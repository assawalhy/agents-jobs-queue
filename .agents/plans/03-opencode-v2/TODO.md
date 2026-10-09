# TODO — restore the ajq OpenCode integration on OpenCode V2

## M1 port + guard
- [x] `harnesses/opencode/ajq.js`: rewrite to `export default { id: "ajq", setup(ctx) }`;
      keep `ajqBin()` + `spawn()` (drop `ctx.$`); wrap every entry point in try/catch
- [x] `execute.before`: classify only `shell`/`bash`, skip commands containing `ajq`;
      `block` → throw a reason naming `ajq_submit`; never throw if daemon is unreachable
- [x] `execute.after`: in `warn`, when the command was heavy, append a nudge text
      block to `event.result` naming the `ajq submit` equivalent
- [x] read `hooks.guard_mode` lazily with a 10s TTL (not at setup — reload does not
      re-run setup for an unchanged file)
- [x] probe on real v2.0.26: warn nudge is visible to the model; block denies

## M2 tools + ensure + context
- [x] `tool.transform`: register `ajq_submit` (command, cwd, kind, timeout, label,
      priority) → `ajq submit --shell -- <cmd>`; cwd from
      `(await ctx.session.get({sessionID})).location.directory`; return `{content}`
- [x] register `ajq_status` (id, tail) → `ajq status --json` + `ajq output --tail`
- [x] ensure the daemon once in `setup` (`ajq daemon ensure`, failures swallowed)
- [x] light queue snapshot: inject `ajq: N running, M queued` when non-empty
- [x] confirm `hooks/ajq-*.sh` and claude/codex/kiro hooks are untouched

## M3 docs + version
- [x] `docs/DETAILS.md`: harness matrix + known gaps — OpenCode V2 plugin, shell
      tool id, warn=post-hoc / block=deny; update the v2.0.18 note
- [x] `README.md`: guard_mode wording for OpenCode (warn is post-hoc there)
- [x] `ajq.js` header: V2 API, verified on v2.0.26
- [x] re-target epic `02-job-notification` M3 plugin items onto this V2 port

## M4 verification
- [x] `tests/plugin_opencode.test.mjs`: fake ctx + stub `ajq` bin — setup registers
      hooks/tools, warn appends the nudge, block throws, `ajq`-prefixed skipped
- [x] CI: `node --test tests/*.test.mjs`
- [x] full Python suite green: `python3 -m unittest discover -s tests -t tests`
- [x] manual end-to-end: a real heavy command in an OpenCode session → nudge; then
      `ajq_submit` queues a real job
