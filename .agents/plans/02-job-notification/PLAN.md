# PLAN — one-call wait; the push design was dropped

## Goal

An agent that submits a heavy job learns the result from a signal, not a
sleep-poll loop, and one call returns both the final state and the log. The wait
must not be killed by the harness's ~120 s shell timeout.

## Decision: the harness already provides the signal

The original plan built a daemon push (`subscribe` op, `protocol.stream`,
`ajq watch`, a per-session plugin consumer, a herdr bridge). That is unnecessary:
the harness already resumes the agent when a **background** command finishes and
includes its output (OpenCode's shell background mode; Claude Code's
`run_in_background`). So the agent runs `ajq wait <id> --tail N` in the
background — the session stays interactive, and the agent is resumed with the
state and log when the job ends.

Dropped, deliberately: `subscribe` op, streaming protocol, subscriber registry,
`ajq watch`, `--session/--notify`, the per-session plugin consumer, and the herdr
state bridge.

## Why the reflex existed

`sleep 115` is just under the ~120 s shell tool timeout: the agent wants one
blocking wait, the harness SIGTERMs the call, so it fakes the wait. Backgrounding
the wait sidesteps the timeout entirely.

## What ships

- `ajq wait <id> --tail N`: one call returns the final state and the last N log lines.
- OpenCode `ajq_wait` tool: the same wait in-process (state + tail), not the shell timeout.
- `ajq_status`'s `tail` is clamped (max 1000): a model passing a runaway tail as
  a wait can no longer read the whole log.
- Skill + hook text: wait (background it for a slow job); never sleep, never poll `ajq status`.

## Milestones

- **M1** `wait --tail` + tests.
- **M2** plugin `ajq_wait` + `tail` clamp + tests.
- **M3** skill + hook + plugin-message text.
- **M4** docs + full suite; close issue #1 as not implemented.

## Risks

- The pane still reads `idle` while a background wait runs (the turn ended).
  Accepted: the herdr bridge was dropped as too much complexity.
- A model may still poll; the clamp bounds the damage and the skill names the
  anti-pattern explicitly.
