# PLAN: light jobs skip the worktree serial gate

## Goal

A job in the light pool starts at once when the pool cap, `max_concurrent` and the memory and CPU admission checks allow it, even when another job already runs in the same worktree.

## Evidence

- Job `j-3a42d33b84bb` (`bun run arch:check`) waited about 60s behind `j-80ba4afa47df` (`bun run typecheck`).
- Both had `serial_key` `/home/assawalhy/.herdr/worktrees/CRM/better-navigation-3al-104`.
- `Scheduler._hold` returns `serial_key busy` before it looks at memory or CPU (`src/ajq/scheduler.py:294`), so free resources never mattered.
- `arch:check` has kind `unknown` and tool `bun:arch:check`. It is in the light pool only because its cached estimate (4.2s, `cache:n=2`) is below `estimates.light_threshold_s` (`Scheduler._auto_pool`). A rule by kind (`lint`, `format`, `check`) would not cover it, so the rule has to be by pool.

## Approach

- In `Scheduler._hold`, skip the `serial_key busy` check when the job's pool is `light` and `limits.serial_light` is `false`.
- In `Scheduler.admission_state` and `Scheduler._busy_serials`, do not add a running light job to the busy set. A light job then never blocks a later heavy or normal job either.
- `start_all_plan` counts `blocked_by_serial` from `_busy_serials`, so it follows without a second change. A queued light job is never counted as blocked by serial.
- New config key `limits.serial_light`, default `false`: light jobs ignore the serial gate. Setting it to `true` restores today's behaviour. Env override `AJQ_SERIAL_LIGHT`.
- Heavy and normal jobs keep the gate: two of them in one worktree still run one after the other.
- `--serial-key none` and an explicit `--serial-key <key>` keep their meaning for jobs that are not light. A light job ignores an explicit key too, while the setting is `false`. This is documented, not special-cased.

## Decisions

- By pool, not by kind: the case that triggered this is an `unknown` kind promoted to light by its estimate.
- Light jobs neither wait on nor hold the key: if they held it, a 4s check would delay a typecheck the same way, only in the other direction.
- A setting with the old behaviour available, not a removal: a cheap job can still write files (`prettier --write`, `eslint --fix`, a cheap `unknown` script), and some users want the strict rule.
- Default `false` (exempt): this is the behaviour asked for, and the pool is already defined as cheap.

## Risks

- A light job that writes files can overlap a heavy job in the same worktree. Example: `prettier --write` while `tsc` reads the same sources. The window is short because light means an estimate under 30s, but it exists. The setting is the escape hatch.
- A cold `unknown` command has no estimate, so it is `normal`, not light, and keeps the gate until its estimate is cached. Accepted.

## Milestones

1. Scheduler change and the setting.
2. Tests.
3. Docs.
4. Full verify.

## Files

```
src/ajq/config.py        limits.serial_light default, AJQ_SERIAL_LIGHT, section hint
src/ajq/scheduler.py     _hold, admission_state, _busy_serials
tests/test_scheduler.py  light skips the gate, heavy keeps it, setting true restores it
tests/test_start_all.py  blocked_by_serial does not count light jobs
tests/test_config_guard.py  new key is a bool and has a default
docs/DETAILS.md, README.md  serialization rows and the config table
```
