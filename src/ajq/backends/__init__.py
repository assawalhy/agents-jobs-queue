"""Per-platform resource enforcement.

  get_backend("auto")  -> systemd scope cgroups on Linux, POSIX spawn elsewhere
  get_backend("linux") -> LinuxBackend (self-degrades to POSIX when systemd is out)
  get_backend("macos") -> MacosBackend, also known as the POSIX backend
"""

from __future__ import annotations

from .base import Handle, ResourceBackend, get_backend
from .linux import FALLBACK_NAME, SYSTEMD_NAME, LinuxBackend
from .macos import MacosBackend

__all__ = [
    "Handle",
    "ResourceBackend",
    "get_backend",
    "LinuxBackend",
    "MacosBackend",
    "SYSTEMD_NAME",
    "FALLBACK_NAME",
]