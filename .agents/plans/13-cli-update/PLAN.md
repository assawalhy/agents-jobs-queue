# PLAN: `ajq update`, a release and tag based update check

## Goal

`ajq update` tells the user whether the repo has a newer release or tag, and installs that
exact ref instead of the latest commit on main. The work lands on a branch stacked on
`feat/tui` (PR #9), with its own PR based on `feat/tui`.

## Approach

### What is installed

- The installer records the ref it installed in the registry (`~/.local/share/ajq/installed.txt`),
  as a `version|<ref>|<code version>` line. The ref is the network ref when the payload was
  fetched, and the local `git describe` when the installer ran from a checkout.
- The CLI reads that line to know what is installed. The version comes from the ref
  (`v0.1.2` is `0.1.2`), not from the code's `__version__`, because `__version__` is `0.1.0`
  at every tag published so far.
- No registry line, for instance a manual install from a clone: the CLI falls back to
  `__version__` and says the install is untracked.

### What is available

- `https://github.com/<repo>/releases.atom` is the primary source. It lists the releases and the
  tags, needs no token, and has no rate limit. The entry title is the ref, the entry content is
  the note.
- `https://api.github.com/repos/<repo>/releases/latest` is the second source. It gives the
  published release, its notes and its assets. The API is unauthenticated and limited to 60
  requests an hour per address, so it is a fallback and an addition, never the only path.
- The candidates from both sources are compared as version tuples. The highest wins, and a
  published release wins over a tag of the same version.

### The command

- `ajq update --check` reports and changes nothing. Exit 0 when the installed version is the
  latest, 1 when a newer version exists, 2 when the check could not be made.
- `ajq update` installs the latest release or tag. It prints the exact command it runs, asks
  for confirmation unless `--yes`, and refuses nothing on its own: the installer is the same
  one the user already trusts.
- The install runs the installer from the chosen ref, not from main:
  `AJQ_VERSION=<ref> bash <(curl -fsSL https://raw.githubusercontent.com/<repo>/<ref>/install.sh) --target <the harnesses in the registry>`.
  The installer already pins its payload fetch to `AJQ_VERSION`, so the script and the payload
  come from the same ref.
- `--ref <ref>` installs a chosen release or tag. `--ref main` is the explicit way to ask for
  the unreleased build.
- `--dry-run` prints the command without running it. `--json` prints the report as JSON.
- The command is daemon free: it works with the daemon down. When the daemon is up and has
  running jobs, the command says how many and asks before restarting the daemon, because the
  installer restarts the unit and that kills the running children.

## Decisions

- The atom feed is the primary source, not the API: the feed needs no token and has no rate
  limit, and a tool an agent may call must not fail because the address hit the API limit.
- The installed version comes from the recorded ref, not from `__version__`. The version string
  was never bumped after `0.1.0`, so a code based comparison would report an update for a user
  who already runs the newest tag.
- The update runs the installer instead of reimplementing the install. One installer, one set of
  rules for the unit, the config, the harness files and the registry.
- The ref is pinned in two places: the script comes from the raw URL at the ref, and
  `AJQ_VERSION` pins the payload fetch to the same ref. A script from main could otherwise
  install a payload from a tag.
- The registry grows a `version` key rather than a new file, so the uninstall path already
  drops it and the data directory keeps one file.
- No automatic check on any other command. The check costs a network round trip, and a tool
  that phones home on every call is a tool an agent pays for.
- The next release has to bump `__version__` to match its tag. The registry makes the check
  right either way, but `ajq version` is still wrong until then.

## Milestones

1. `src/ajq/update.py`: the version parsing, the feed and API parsing, the report, the install.
2. `src/ajq/cli.py`: the `update` command, daemon free, with `--check`, `--yes`, `--dry-run`,
   `--ref` and `--json`.
3. `src/ajq/paths.py`: the data directory and the registry path.
4. `install.sh`: record the installed ref, drop it on uninstall.
5. Tests: `tests/test_update.py`, and the installer assertion in `tests/test_install.py`.
6. Docs: the README row, the command table and a section in `docs/DETAILS.md`.
7. The stacked branch and the PR.

## Risks

- The version string in the code is `0.1.0` at every published tag. The check leans on the
  registry, so an install without a registry line falls back to a stale version and may report
  an update that is not one.
- The update restarts the daemon and rewrites the CLI and the harness files. The command warns
  about running jobs and asks first, but a user who answers yes loses the running children.
- The install fetches a script from the network and runs it. It is the same script the one line
  install already runs, pinned to a ref, and the ref is printed before it runs.
- The API path can hit the 60 requests an hour limit on a shared address. The feed path has no
  limit, and the command works with the feed alone.
- The GitHub layout can change. The feed and the API are two independent sources, so one
  change leaves the other working.

## Verification

- The full suite through ajq: 328 tests, OK.
- A live check against the repo: the feed gives `v0.1.2` with its note, the exit code is 1
  when a newer version exists and 2 when the check cannot be made.
- A live dry run prints the command it would run, with the ref pinned to `v0.1.2` and the
  harnesses taken from the registry.
- The machine's Python cannot verify GitHub without `SSL_CERT_FILE`, because the CA path has
  no hashed certificates. The command says so by name instead of failing with a socket error.
