# TODO: `ajq update`, the release and tag based update check

- [x] `src/ajq/update.py`: the version parsing, the feed and API parsing, the report, the install
- [x] `src/ajq/paths.py`: the data directory and the registry path
- [x] `src/ajq/cli.py`: the `update` command, daemon free, with `--check`, `--yes`, `--dry-run`, `--ref`, `--json`
- [x] `install.sh`: record the installed ref, drop it on uninstall
- [x] `tests/test_update.py`: the version parsing, the feed parsing, the report, the install command
- [x] `tests/test_install.py`: the installer records the ref
- [x] README row, the command table and a section in `docs/DETAILS.md`
- [x] The stacked branch `feat/update-check` and the PR based on `feat/tui`
- [x] Verify: the full suite through ajq, a live check against the repo
