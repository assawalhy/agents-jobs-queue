# PLAN — raise ajq's memory/output limits so heavy jobs stop dying

## Goal

A full typecheck + coverage run was OOM-killed twice under ajq's default
2 GB per-job cap and only survived with `--memory-mb 12288`. Raise the limits
so the ordinary `ajq submit` of a heavy job just works.

## Approach

Change the **user config** (`~/.config/ajq/config.json`), not the shipped
`DEFAULTS`. Restart the daemon when idle, then verify with a probe job that
allocates more than the old cap.

```
this box (measured)          user config change
--------------------------   --------------------------------
total 31879 MB               resources.memory_mb      2048 -> 6144
available 11641 MB           resources.memory_headroom 1.2 -> 1.4
cores 2, swap 0              defaults.max_output_bytes 8 MiB -> 16 MiB
```

```
admission (scheduler.py:229)
  free_mem  >=  memory_mb * memory_headroom
  2048*1.2 =  2458 MB   (today: starts, then OOM-killed at 2 GB)
  6144*1.4 =  8601 MB   (after: admitted when the box has room, cap is real)
```

## Decisions

- **User config, not `src/ajq/config.py` DEFAULTS.** The shipped default is
  documented in `docs/DETAILS.md` and tuned for a normal dev box; this machine
  is unusual (2 cores, ~20 GB already used by other things). Rejected: editing
  DEFAULTS + `backends/base.py:DEFAULT_MEMORY_MB` — it would push every user
  toward admitting fewer jobs per tick.
- **6144 MB, not 12288.** 12288 * 1.2 headroom = 14.7 GB of free memory
  required at admission; this box shows 11.6 GB available, so that job would
  sit queued forever instead of running. 6144 is the largest value that can
  actually be admitted here.
- **headroom 1.2 -> 1.4.** Admission subtracts only the cap from `free` inside a
  tick, so several big jobs can be admitted on one reading of `MemAvailable`.
  With no swap, a tighter multiplier is the cheap guard against that.
- **Output cap 8 -> 16 MiB.** A coverage run can exceed 8 MiB; a truncated log
  is a separate debugging trap.
- **Pools and `max_concurrent` untouched.** Concurrency was not the failure.
- **Restart the daemon only when nothing is running.** A restart is what killed
  the original commit; `ajq ls` first, then `ajq daemon stop && ajq daemon ensure`.
- **Out of scope:** jobs lost on daemon restart is a real durability gap, but
  it is a separate change, not a limits change.

## Milestones

1. Edit the user config keys.
2. Restart the daemon idle.
3. Verify: `ajq config` shows the new values; a 4 GB probe job finishes
   (it would have been OOM-killed under the old 2 GB cap).
4. One-line note in `skills/ajq/SKILL.md`: heavy kind jobs may still need an
   explicit `--memory-mb`.

## Risks

- Over-admission if `MemAvailable` is read high and several 6 GB jobs start at
  once; `memory_headroom` 1.4 plus no swap is the accepted exposure.
- A queued-not-running heavy job looks like a hang. `ajq ls` / `ajq status`
  shows `eta_start`; mention it in the SKILL.md line.