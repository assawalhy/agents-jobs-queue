"""ajq — a jobs-queue daemon for heavy agent tasks.

Modules:
  paths      filesystem locations (state, config, socket, job artifacts)
  platform   memory/cpu probes, interpreter path, RSS sampling, nice
  protocol   JSON-lines request/response over a unix socket
  store      SQLite persistence (jobs, estimates, meta)
  estimate   Welford duration cache keyed by a task signature
  guard      command -> kind/pool/heaviness classification
  config     user config file + env + flag precedence
  backends   per-platform resource enforcement (linux cgroups / macos posix)
  exec       job execution: spawn, timeout, output cap
  scheduler  queue ordering, pool caps, admission, ETAs
  daemon     serve loop
  cli        command line interface
"""

__version__ = "0.1.0"