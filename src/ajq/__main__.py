"""Entry point for `python3 -m ajq`.

Also what the daemon's detached spawn uses, so a restarted daemon starts with a
plain `python3 -m ajq daemon serve` instead of an inline `-c` fallback.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())