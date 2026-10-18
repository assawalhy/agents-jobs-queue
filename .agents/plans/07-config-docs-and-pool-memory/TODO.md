# TODO — config docs and per-pool memory

- [x] config.py: add `resources.pool_memory_mb` to DEFAULTS and `_DOC`
- [x] scheduler.py: add `_memory_for`, resolve+store in `submit`, use in `tick`
- [x] tests: per-pool resolution, per-pool admission, config merge
- [x] docs/DETAILS.md: full config reference, admission formula, swap caveat
- [x] user config: memory_mb 2048, pool_memory_mb {heavy: 6144}, headroom 1.2
- [x] wait for idle, then ./install.sh --all to rebuild the installed zipapp
- [x] verify: run the touched test modules
- [x] verify: live submit reserves 2048 (light) and 6144 (heavy)
