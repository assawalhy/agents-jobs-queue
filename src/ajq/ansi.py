"""ANSI escape handling: the patterns, and a stripper for a byte stream.

The daemon removes the escape sequences from the child's output before it
reaches `out.log`, so the file agents read stays plain. The same sequences are
kept in `color.log`, where the TUI parses them to draw the log with colours.

A sequence can straddle two chunks of the child's output, so the stripper
holds back the tail that could still grow into one.
"""

from __future__ import annotations

import re

ESCAPE_RE = re.compile(
    r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[()#][0-9A-Za-z]"
    r"|\x1b[=>]"
)
ESCAPE_BYTES_RE = re.compile(ESCAPE_RE.pattern.encode())


def strip_escapes(text: str) -> str:
    """`text` without the escape sequences."""
    return ESCAPE_RE.sub("", text).replace("\x1b", "")


def _partial_tail(buffer: bytes) -> int:
    """How many bytes at the end of `buffer` may be an unfinished escape sequence."""
    index = buffer.rfind(b"\x1b")
    if index < 0:
        return 0
    tail = buffer[index:]
    if len(tail) == 1:
        return 1
    kind = tail[1:2]
    if kind == b"[":
        for byte in tail[2:]:
            if 0x40 <= byte <= 0x7E:
                return 0
        return len(tail)
    if kind == b"]":
        # An OSC sequence ends at the BEL or at the ST (ESC backslash); a BEL
        # inside the tail means the sequence is complete and the rest of the
        # buffer is plain text that must not be held back.
        if b"\x07" in tail[2:]:
            return 0
        return 0 if tail.endswith(b"\x1b\\") else len(tail)
    if kind in (b"(", b")", b"#"):
        return 0 if len(tail) >= 3 else len(tail)
    return 0


class StreamStripper:
    """Removes the escape sequences from a byte stream that arrives in chunks."""

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, data: bytes) -> bytes:
        """The bytes of `data` with the escape sequences removed."""
        buffer = self._pending + data
        hold = _partial_tail(buffer)
        if hold:
            self._pending = buffer[len(buffer) - hold :]
            buffer = buffer[: len(buffer) - hold]
        else:
            self._pending = b""
        return ESCAPE_BYTES_RE.sub(b"", buffer).replace(b"\x1b", b"")

    def flush(self) -> bytes:
        """The held back tail, stripped, for the end of the stream."""
        pending, self._pending = self._pending, b""
        return ESCAPE_BYTES_RE.sub(b"", pending).replace(b"\x1b", b"")
