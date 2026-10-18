# PLAN — complete the config docs, and make memory admission per-pool

## Goal

Two outcomes.

1. `docs/DETAILS.md` documents every `config.json` key, including the ones
   missing today, and states the admission formula that decides when a job
   starts.
2. A heavy job can be capped at 6 GB without charging that 6 GB reservation to
   every lint and format job, so `heavy 2` is limited by real RAM rather than by
   a global reservation.

## What is actually wrong

`heavy 2` shows one running job because the pool cap is not the limiter. The
admission gate is (`src/ajq/scheduler.py:230-232`):

```
admit only when  MemAvailable >= job_memory_mb * memory_headroom
```

`resources.memory_mb` is one global number. `tick()` reads it through
`job.get("memory_mb") or config.resources.memory_mb`, so the heavy-tuned 6144 MB
is reserved for every pool. This box shows ~7.9 GB `MemAvailable` against a
`6144 * 1.4 = 8601 MB` requirement, so almost nothing new starts, and two heavy
jobs would need `6144 * (1 + 1.4) = 14745 MB` free at once.

Swap does not change this. The Linux backend pins every job to
`MemorySwapMax=0` (`src/ajq/backends/linux.py:196`), and
`platform.available_memory_mb()` reports `MemAvailable` only. A job that passes
its cgroup `MemoryMax` is OOM-killed with or without box swap.

## Decisions

- **Per-pool memory reservations, keyed `resources.pool_memory_mb`.**
  A job with no explicit `--memory-mb` resolves its cap from its pool, falling
  back to `resources.memory_mb`. One number serves both the cgroup cap and the
  admission reservation, so they cannot drift. Rejected: a separate
  `admission_mb` decoupled from `MemoryMax` — two numbers to keep in sync, and
  the cap is what the kernel enforces.
- **Resolve at submit, store on the job row.** `Scheduler.submit` writes the
  resolved `memory_mb`, so the backend's per-job override path
  (`effective_resources`) and `tick()` read the same number. A `_memory_for`
  helper also resolves in `tick()` so queued rows written before this change
  still honor the pool mapping.
- **Ship `resources.pool_memory_mb` empty in DEFAULTS.** No behavior change for
  existing users until they set it. The docs example shows the recommended
  tuning. Rejected: shipping `heavy: 4096` as a default — plan 04 already chose
  to keep machine-specific tuning in user config.
- **Lower `memory_headroom` 1.4 -> 1.2 in the user config only.** The user
  asked for it. It moves the per-job requirement from 8601 to 7372 MB. Named
  risk: a job cannot swap, so a tighter margin raises the chance of a cgroup OOM
  on a spiky job. The DEFAULTS value is already 1.2, so no shipped change.
- **`resources.memory_mb` back to 2048; only heavy keeps 6144.** The global
  value is the fallback for a pool absent from `pool_memory_mb`. Reverting it to
  the pre-04 default means every non-heavy pool reserves 2048, and the heavy
  pool carries the 6144 the OOM-killed `typecheck + coverage` job needs.
  Rejected: keeping 6144 global plus lowering each other pool explicitly, which
  leaves the fallback at 6144 and is more to get wrong.
- **The change must be installed to take effect.** The daemon runs the zipapp at
  `~/.local/bin/ajq`, not `src/`, so the per-pool resolution only reaches it after
  `./install.sh --all` rebuilds it. Found during execution, after the config edit.
  The install restarts `ajqd.service`, so it waits for the queue to drain.
- **Docs live in `docs/DETAILS.md`, not a new `docs/CONFIG.md`.** DETAILS.md
  already holds the config reference and the README points there. A second file
  would duplicate it.
- **User config, not DEFAULTS, for 6144.** Consistent with plan 04: this box is
  unusual. DEFAULTS keeps `memory_mb: 2048`.

## Milestones

1. `resources.pool_memory_mb` in `config.py`: DEFAULTS entry, `_DOC` note.
2. `scheduler.py`: `_memory_for` helper; `submit` resolves and stores;
   `tick()` uses the helper.
3. Tests: per-pool resolution on submit, per-pool admission, config merge.
4. Rewrite the Configuration section of `docs/DETAILS.md`: every key, the
   admission formula, the swap caveat, the per-pool memory key.
5. Edit `~/.config/ajq/config.json`: `memory_mb` 6144 -> 2048, add
   `pool_memory_mb: {"heavy": 6144}`, `memory_headroom` 1.4 -> 1.2.
6. Wait for the queue to drain, then `./install.sh --all` to rebuild the zipapp.
7. Verify: a light submit reserves 2048, a heavy submit reserves 6144.

## Risks

- A per-pool cap that is too low kills a job that would have fit (light 1024 MB
  may be tight for some `check` jobs). Mitigation: only heavy gets 6144; the
  other pools fall back to 2048 unless the user sets them.
- Lowering headroom to 1.2 narrows the OOM margin. Accepted by the user.
- Old queued job rows without a stored `memory_mb` must still resolve. The
  `tick()` helper covers them.
