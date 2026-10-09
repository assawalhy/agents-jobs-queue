# PLAN — restore the ajq OpenCode integration on OpenCode V2

## Goal

OpenCode sessions get the ajq guard and tools again. Today the plugin is
**inert**: every OpenCode session runs heavy commands unguarded, and only the
auto-loaded skill steers — which models ignore. Heavy bash should instead
trigger a **post-hoc `warn` nudge** (chosen posture) that names the
`ajq submit` equivalent, with `block` still available.

## Why it is broken (researched on this machine, not assumed)

- Installed OpenCode is **v2.0.26**; the repo targets v2.0.18.
- V2 calls the plugin default export's `setup(ctx)` and **ignores `server()`**
  (V2 docs; confirmed with a throwaway plugin exporting both — only `setup()`
  ran). `harnesses/opencode/ajq.js` puts everything in `server()` and has
  `setup() {}` → no guard, no `ajq_submit`/`ajq_status` tools, no daemon ensure.
- Even if it ran, the OpenCode guard only acted in `block` mode; in `warn`
  (the default) it returned early, so OpenCode got no nudge at all — unlike
  Claude/Codex/Kiro, whose shell hooks inject `additionalContext`.

Verified V2 facts the port relies on (probed against v2.0.26):

```
shell tool id           : "shell"            (V1 was "bash")
execute.before event    : {tool, sessionID, agent, messageID, id, input}
                          input = { command }
execute.after event     : {…, input, status, result}
                          result = { output:{exit,truncated,output,status},
                                     content:[{type:"text",text}], metadata:{…} }
tool executor context   : {sessionID, agent, messageID, id, progress, signal}
                          (no cwd)
session directory       : (await ctx.session.get({sessionID})).location.directory
plain default export    : { id, setup } accepted — no @opencode/plugin import
```

## Approach

```
opencode v2.0.26
      │  setup(ctx)
      ▼
ajq.js (V2 plugin)
  ├─ ensure daemon once          (ajq daemon ensure, failures swallowed)
  ├─ tool.hook("execute.before") ── shell/bash, not ajq ── block? throw : pass
  ├─ tool.hook("execute.after")  ── warn + heavy ── append nudge to result
  └─ tool.transform              ── ajq_submit, ajq_status
```

Classifier stays in Python (`ajq guard --explain`); no JS reimplementation.

## Decisions

- **V2 API via a plain `export default { id, setup }`.** Verified accepted; no
  `@opencode/plugin` dependency to resolve. *Rejected:* keeping the V1
  `server()` shape (V2 ignores it — the current bug), importing the plugin SDK
  (resolution risk, unneeded).
- **Shell tool id is `shell` on V2; accept `bash` too.** *Rejected:* hardcoding
  `bash` (the V1 name — would never fire again).
- **`warn` = post-hoc nudge in `execute.after`; `block` = throw in
  `execute.before`.** V2 has no per-tool context channel, and warn is the chosen
  posture. *Rejected:* auto-rewriting the command to `ajq submit --shell`
  (changes output semantics to a job id), per-request `context` injection
  (stale, one subprocess per model request).
- **`ajq_submit` cwd from `Session.Info.location.directory`.** *Rejected:*
  `ctx.location` (plugin-instance scoped, may not match the session), the
  daemon's cwd (wrong).
- **Read `hooks.guard_mode` once per `setup`.** *Rejected:* a config subprocess
  on every shell call.
- **A Node smoke test with a fake ctx + stub `ajq` bin.** The plugin had no
  regression guard, which is how it silently died. *Rejected:* manual-only
  verification.

## Milestones

- **M1 — V2 port + guard.** Rewrite `ajq.js` to `setup(ctx)`; `execute.before`
  (block) + `execute.after` (warn nudge); probe on real v2.0.26.
- **M2 — tools + ensure + context.** `ajq_submit`/`ajq_status` via
  `tool.transform`; daemon ensure; light queue snapshot.
- **M3 — docs + version.** DETAILS/README/header updated to V2 and the new warn
  semantics; epic 02's plugin items re-targeted.
- **M4 — verification.** Node smoke test + CI step; full Python suite; a real
  end-to-end heavy command.

## Risks

- **`execute.after` mutation may not surface to the model** if the runtime
  snapshots the result before hooks — must confirm end-to-end in M1.
- **Classifier false positives** (`make`, trivial `go build`) — low blast radius
  under `warn`; `block` stays opt-in.
- **Subprocess latency** in warn mode (one `ajq guard` call after a heavy
  command) — best-effort, short timeout, skipped when mode is `off`.
- **API drift** — header pins the verified v2.0.26; a bump needs re-verification.
- **Epic 02 collision** — 02's M3 edits the same `ajq.js` against dead V1 code;
  it must land on top of this port, not before.
