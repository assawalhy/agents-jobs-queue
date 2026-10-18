# TODO: pressure-aware admission

- [x] `platform.zram_credit_mb()` from `/sys/block/zram*/mm_stat` + `disksize`, 0 when absent
- [x] `platform.psi_some_avg10(resource)` for `memory` and `cpu`, None when `/proc/pressure` is absent
- [x] `platform.cpu_temp_c()` from thermal zones / hwmon, None when no CPU sensor
- [x] Config DEFAULTS + descriptions: `resources.swap_credit`, `resources.hard_factor`, `resources.swap_mb`, `resources.admission.{psi_memory_max,psi_cpu_max,temp_max_c}`
- [x] `Scheduler.tick`: pressure gate, budget = MemAvailable + zram credit, progress rule
- [x] `Scheduler`: `blocked` reason on enriched queued jobs
- [x] `backends/linux.py`: `MemoryHigh=cap`, `MemoryMax=cap*hard_factor`, `MemorySwapMax` from config
- [x] `exec.py`: RSS poll kill at the hard ceiling
- [x] CLI: `blocked` in `ajq list` / `ajq status`; `doctor` prints zram credit, PSI, CPU temp
- [x] `docs/DETAILS.md`: new keys and the new admission formula
- [x] Tests: `test_scheduler.py` (gates, progress rule, budget), `test_config_guard.py` (defaults), linux backend argv test
- [x] Verify: scoped pytest run via ajq for the touched test files

## Review fixes

- [x] Soft cap (`MemoryHigh` + swap) only on cgroup v2 with zram as the first swap; hard `MemoryMax` + `MemorySwapMax=0` elsewhere
- [x] RSS watchdog uses `hard_factor` only with the soft cap
- [x] Memory PSI reads `full avg10`, default 5; the progress rule overrides every gate
- [x] zram credit: exact device match from `/proc/swaps`, `mem_used_total`, `mem_limit`, zero when disk swap outranks zram
- [x] CPU temperature: k10temp `Tdie`/`Tccd` labels; drop the average when the sensor disappears; update under the lock
- [x] `enrich_many` / one admission snapshot per list and start plan
- [x] `ajq doctor`: cgroup v2 memory and zram-first lines, and a note without cgroup v2
- [x] CLI sends an absolute `--cwd`, and its own cwd when none is given
- [x] Guard: chained `cd` joins directories, `cd "$DIR"` stays in the script, multi-line scripts are one quoted argument
- [x] Hook: keeps newlines from the JSON payload, escapes newlines and tabs in its JSON output, falls back to a suggestion when the CLI prints none
- [x] Tests and docs
