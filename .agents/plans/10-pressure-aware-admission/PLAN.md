# PLAN: pressure-aware admission, zram-aware memory caps

## Goal

1. The queue never sits with queued jobs and zero running jobs because of a memory number that can never be met.
2. Jobs can use zram swap instead of being OOM-killed at the cap, so the machine's real capacity (32 GB RAM + 16 GB zram at ~3.9x compression) is usable.
3. Admission backs off when the machine is actually under pressure (memory stall, CPU stall, CPU temperature), so parallel agents do not make the desktop lag or overheat.

## What is wrong today (observed 2026-10-19)

- `ajq list` shows `j-1f7fe67a3b2e` and `j-4809d3e9d149` queued in `heavy` with 0 running.
- Gate in `Scheduler.tick` (`src/ajq/scheduler.py:244`): `MemAvailable >= memory_mb * memory_headroom`.
- Heavy reserves `pool_memory_mb.heavy 6144 * 1.2 = 7373 MB`. `MemAvailable` is 5446 MB. The gate cannot pass while the agents and browsers hold that memory, so the heavy pool is deadlocked.
- `MemAvailable` ignores swap. zram0 has ~4 GB logical space left (≈3 GB of RAM to gain at the current 3.9x ratio), and `/proc/pressure/memory` `some avg10` is 0.00. The machine is not under memory pressure, yet ajq refuses work.
- Every job runs with `MemoryMax=<cap>` and `MemorySwapMax=0` (`src/ajq/backends/linux.py:195-196`), and `ProcessRunner._violation` kills at tree RSS > cap (`src/ajq/exec.py:231-234`). A job that would fit by swapping cold pages to zram is killed.
- `eta_start` prints `0s` for the blocked head job, so the stall gives no feedback.

## Approach

```
tick(): for each queued job, in order
  pool / serial / max_concurrent checks        (unchanged)
  pressure gate   PSI memory some avg10 > psi_memory_max   -> hold all
                  PSI cpu    some avg10 > psi_cpu_max      -> hold all
                  max CPU temp           > temp_max_c      -> hold all
  memory gate     budget = MemAvailable + zram credit
                  budget >= reserve * headroom             -> admit
  progress rule   nothing running in ajq and no pressure   -> admit head job anyway
```

```
cgroup per job (linux-systemd)
- MemoryMax=<cap>M  MemorySwapMax=0
+ MemoryHigh=<cap>M                 kernel reclaims to zram above cap, no kill
+ MemoryMax=<cap * hard_factor>M    kill only past the hard ceiling
+ MemorySwapMax=<swap_mb>M          default: allowed (zram is priority 5, used first)
  ProcessRunner RSS poll kill uses the hard ceiling, not <cap>
```

## Decisions

- Progress rule: when ajq runs nothing, the head job starts if no pressure signal is raised. Rationale: a static reservation must never block the only job; real pressure signals still can.
- zram credit: `(zram disksize - orig_data_size) * (1 - compr_data_size / orig_data_size)` from `/sys/block/zram*/mm_stat`, scaled by `resources.swap_credit` (default 1.0). Disk swap gives 0 credit. Rationale: swapping to zram costs CPU, swapping to disk causes the lag the user wants to avoid.
- PSI and temperature gate new admissions only. They never kill running jobs. Rationale: killing a 5-minute build at minute 4 wastes more than it saves.
- Defaults: `psi_memory_max 10`, `psi_cpu_max 70`, `temp_max_c 95` (30 s average; the box read 94-98 °C under agent load, so 90 held every second job), `hard_factor 1.5`. Rationale: PSI memory `some` above 10% means tasks stall on reclaim often enough to be felt; this box sits at 71-78 °C idle-ish, Intel throttles near 100 °C.
- Temperature source: max of `x86_pkg_temp` / `coretemp` / `k10temp` / `cpu_thermal` from `/sys/class/thermal` and `/sys/class/hwmon`; no sensor means the gate is off. macOS: PSI and temperature gates are off (no portable source).
- Keep `pool_memory_mb` as the reservation and the soft cap. Rejected: learned per-signature peak memory as the reservation. It is the better long-term fix for "a 1.5 GB typecheck reserves 6 GB", but needs tree peak sampling and storage; follow-up epic.
- Rejected: only lowering `memory_headroom` or `pool_memory_mb.heavy`. It moves the threshold, and the deadlock returns the next time the desktop uses more RAM.
- `ajq list` / `ajq status` show `blocked` (`memory 7373>5446`, `psi-mem 14%`, `temp 93C`) for a queued job whose pool has a free slot. Rationale: the stall must be visible.
- New keys go in `config.DEFAULTS` and `docs/DETAILS.md`, all under `resources` (and an `admission` sub-map for the gates).

## Milestones

1. Probes: zram credit, PSI, CPU temperature in `platform.py`.
2. Admission: pressure gate, swap-aware budget, progress rule, blocked reason in `scheduler.py`.
3. Enforcement: `MemoryHigh` / hard `MemoryMax` / swap in `backends/linux.py`, hard ceiling in `exec.py`.
4. CLI and docs: `blocked` column/field, `doctor` lines for zram/PSI/temp, `DETAILS.md`.
5. Tests for each, then the scoped test run.

## Risks

- Allowing swap per job means a runaway job can fill zram before hitting the hard ceiling. Mitigation: hard `MemoryMax` still kills at `cap * 1.5`, and PSI holds new admissions meanwhile.
- PSI `cpu some` is high on a busy desktop even without ajq jobs; the 70% default may hold admissions during heavy interactive use. That is the intended trade.
- Temperature sensor names differ by vendor; unknown hardware falls back to no thermal gate.
- `MemoryHigh` throttling makes an over-cap job slower instead of failing fast; timeouts still apply.
