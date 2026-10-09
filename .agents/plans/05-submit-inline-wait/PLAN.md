# PLAN — `ajq_submit` returns the answer for cheap jobs

## Goal

Every heavy command costs two tool calls: `ajq_submit` returns just an id, then
`ajq_wait` fetches state + log. For a 1-15 s job that is a wasted round trip and
an id the agent has to re-parse by hand. Make `ajq_submit` block briefly and
return state + log when the job looks cheap, and stay fire-and-forget otherwise.

## Approach

Change is confined to the OpenCode harness plugin `harnesses/opencode/ajq.js`
(already installed at `~/.config/opencode/plugins/ajq.js`, byte-identical). No
daemon, CLI or protocol change — `ajq --json submit` already returns everything
needed to decide.

```
agent ──ajq_submit{command}──> ajq --json submit --shell -- <cmd>
                                    │
                     id  eta_start_s  eta_run_s  queue_position
                                    │
                eta_start_s + eta_run_s <= wait_s ?
                yes ──> ajq wait <id> --tail 40 --timeout <budget> ──> "state done + log"
                no  ──> "<id>  running  eta_run 3m"        (today's one-liner, unchanged)
```

Default `wait_s = 20`, `0` disables, hard clamp 120 (same ceiling `ajq_wait`
already uses). New tool property: `wait_s`.

## Decisions

- **Plugin-side, not a CLI flag.** The agent-facing contract is the tool; the
  CLI keeps its non-blocking submit for bash users. Rejected: `ajq submit
  --wait-s` in `cli.py` — same behaviour for the CLI, but touches the shipped CLI
  surface and its tests for no gain to the tool path.
- **Gate on the daemon's own estimate, not on the pool.** `eta_start_s` +
  `eta_run_s` is exactly "when will this be over". Rejected: "wait whenever the job
  landed in the light pool" — a light job behind a busy `serial_key` still burns
  the whole budget for nothing, which is the case that made my own submissions
  queue at `queued#1`.
- **Small default (20 s).** Longer makes the tool blocking for real work, which is
  the whole point of the queue. The agent can still pass `wait_s` when it knows.
- **`--json submit` always, id on the first line of both paths.** Fixes the other
  half of the friction: today the agent has to regex the id out of the human line.
- **Never regress on bad JSON.** If the JSON does not parse, fall back to the
  current plain `ajq submit` output.
- **Deploy via `install.sh`, then restart OpenCode** — the tool schema is read at
  session start, so the new `wait_s` argument only exists after a reload.

## Milestones

1. `runSubmit`: `--json submit`, parse, gate on eta, wait or return.
2. `submitInput()`: `wait_s` property + description.
3. `tests/plugin_opencode.test.mjs`: cheap job waits and returns state + log;
   expensive job returns the one-liner without waiting.
4. Docs: `skills/ajq/SKILL.md` flag row, `docs/DETAILS.md` harness section.
5. Install + verify live with a real `ajq_submit` of `echo hi`.

## Risks

- One extra ~30 ms `ajq` spawn per submit, to buy the parse. Accepted.
- A job that overruns its estimate after the gate passes leaves the agent holding
  an id — same as today, no worse.
- `install.sh` overwrites the installed plugin; it is byte-identical to the repo
  copy today, so nothing is lost, but re-check the diff before running.