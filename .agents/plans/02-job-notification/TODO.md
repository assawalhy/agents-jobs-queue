# TODO — one-call wait (reduced from the push design)

## M1 wait --tail
- [x] `cli.py`: `wait --tail/-n N` prints the final state, then the last N log lines
- [x] tests: `_job_tail`, `_cmd_wait` output, parser

## M2 plugin
- [x] `ajq.js`: `ajq_wait` tool (id, tail, timeout) — in-process, returns state + tail
- [x] `ajq.js`: clamp `ajq_status.tail` (max 1000) so a runaway tail cannot read the whole log
- [x] plugin tests: `ajq_wait`, the clamp, tool registration

## M3 guidance
- [x] `SKILL.md`: background `ajq wait --tail`; never sleep, never poll `ajq_status`
- [x] `hooks/ajq-pre-tool-use.sh`: note + block reason name the background wait
- [x] plugin warn/block messages name `ajq_wait` / the background wait

## M4 docs + verification
- [x] `README.md` + `docs/DETAILS.md`: `wait --tail`, `ajq_wait`
- [x] full suite green: `python3 -m unittest discover -s tests -t tests` + `node --test tests/*.test.mjs`
- [x] close issue #1 as not implemented (comment)

## Dropped (documented, not built)
- `subscribe` op, `protocol.stream`, subscriber registry, `ajq watch`,
  `--session/--notify`, per-session plugin consumer, herdr state bridge
