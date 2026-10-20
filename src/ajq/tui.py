"""Interactive terminal view of the queue, opened by a bare `ajq` on a tty.

The job table polls the daemon once a second. Elapsed time is recomputed on
every redraw from the job's timestamps, with the same formula as
`Scheduler._enrich`, so the counter ticks between polls.

The log pane reads `color.log` when the job has one, and parses the SGR
sequences of the raw bytes into segments, so the colours the tool produced are
drawn. The plain text of every line is kept beside the segments, so the search
and the match highlight still work on plain text.

The keys are fixed: the arrows move the job selection, `j` and `k` scroll the
log one line, `g` and `G` jump to the top and the bottom of the log.
"""

from __future__ import annotations

import curses
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from . import ansi, daemon, paths
from .cli import _cmd_text
from .protocol import request
from .store import TERMINAL_STATES

_POLL_S = 1.0
_FRAME_MS = 100
_MAX_LINES = 50_000
_READ_LIMIT = 4 * 1024 * 1024
_FINISHED_LIMIT = 15
_CACHED_OUTPUTS = 8
_WHEEL_LINES = 3
_PAIR_BASE = 8
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")


def clock(seconds: Optional[float]) -> str:
    """`42s`, `1m 2s`, `1h 2m 3s`; `-` when unknown."""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def elapsed_of(job: dict, now: float) -> Optional[float]:
    """Runtime of a finished job, time since start (or enqueue) otherwise."""
    anchor = _float(job.get("started_at")) or _float(job.get("enqueued_at"))
    if not anchor:
        return job.get("elapsed_s")
    ended = _float(job.get("ended_at"))
    return max(0.0, (ended or now) - anchor)


def smartcase(query: str) -> bool:
    """True when the search is case-sensitive: the query has an uppercase letter."""
    return query != query.lower()


def find_matches(lines: list[str], query: str) -> list[int]:
    """Indices of the lines containing `query`, smartcase."""
    if not query:
        return []
    if smartcase(query):
        return [index for index, line in enumerate(lines) if query in line]
    needle = query.lower()
    return [index for index, line in enumerate(lines) if needle in line.lower()]


def spans(line: str, query: str) -> list[tuple[int, int]]:
    """Start and end offsets of every occurrence of `query` in `line`."""
    if not query:
        return []
    hay, needle = (line, query) if smartcase(query) else (line.lower(), query.lower())
    found = []
    start = hay.find(needle)
    while start >= 0:
        found.append((start, start + len(needle)))
        start = hay.find(needle, start + len(needle))
    return found


# -- colour -----------------------------------------------------------------

# A colour is ("base", 0..15), ("x256", 0..255) or ("rgb", (r, g, b)).
_BASE_RGB = (
    (0, 0, 0),
    (205, 0, 0),
    (0, 205, 0),
    (205, 205, 0),
    (0, 0, 238),
    (205, 0, 205),
    (0, 205, 205),
    (229, 229, 229),
    (127, 127, 127),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (92, 92, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
)


@dataclass(frozen=True)
class Style:
    """One SGR state: the colours and the attributes of a run of text."""

    fg: Optional[tuple] = None
    bg: Optional[tuple] = None
    bold: bool = False
    dim: bool = False
    underline: bool = False
    reverse: bool = False
    italic: bool = False


def _nearest_base(rgb: tuple) -> int:
    """The base colour closest to `rgb`, by the distance of the three channels."""
    red, green, blue = rgb
    best, best_distance = 0, None
    for index, (base_r, base_g, base_b) in enumerate(_BASE_RGB):
        distance = (red - base_r) ** 2 + (green - base_g) ** 2 + (blue - base_b) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = index, distance
    return best


def _x256_rgb(index: int) -> tuple:
    """The RGB value of a 256 colour index."""
    if index < 16:
        return _BASE_RGB[index]
    if index < 232:
        cube = index - 16
        levels = (0, 95, 135, 175, 215, 255)
        return (levels[cube // 36], levels[(cube // 6) % 6], levels[cube % 6])
    grey = 8 + 10 * (index - 232)
    return (grey, grey, grey)


def resolve_color(spec: Optional[tuple]) -> Optional[int]:
    """The curses colour number of a colour spec, None for the default."""
    if spec is None:
        return None
    kind, value = spec
    if kind == "base":
        return value
    if kind == "x256":
        if getattr(curses, "COLORS", 0) >= 256:
            return value
        return _nearest_base(_x256_rgb(value))
    return _nearest_base(value)


def _sgr_values(params: str) -> list[int]:
    """The SGR parameters as a list of codes.

    The semicolon form is `1;31`. The colon form carries the sub parameters of
    one code, as in `38:2::255:0:0`, and is flattened to the same list.
    """
    if ":" not in params:
        return [int(part) if part.isdigit() else 0 for part in params.split(";")]
    values: list[int] = []
    for part in params.split(";"):
        cells = part.split(":")
        code = cells[0]
        rest = cells[1:]
        if code in ("38", "48") and rest:
            mode = rest[0]
            if mode == "2":
                values += [int(code), 2] + [
                    int(cell) if cell.isdigit() else 0 for cell in rest[-3:]
                ]
            elif mode == "5":
                values += [int(code), 5, int(rest[-1]) if rest[-1].isdigit() else 0]
            else:
                values.append(int(code) if code.isdigit() else 0)
        else:
            values.append(int(code) if code.isdigit() else 0)
            values += [int(cell) if cell.isdigit() else 0 for cell in rest]
    return values


def _apply_sgr(style: Style, params: str) -> Style:
    """The style after the SGR parameters `params`."""
    values = _sgr_values(params)
    result = style
    index = 0
    while index < len(values):
        code = values[index]
        index += 1
        if code == 0:
            result = Style()
        elif code == 1:
            result = replace(result, bold=True)
        elif code == 2:
            result = replace(result, dim=True)
        elif code == 3:
            result = replace(result, italic=True)
        elif code == 4:
            result = replace(result, underline=True)
        elif code == 7:
            result = replace(result, reverse=True)
        elif code == 22:
            result = replace(result, bold=False, dim=False)
        elif code == 23:
            result = replace(result, italic=False)
        elif code == 24:
            result = replace(result, underline=False)
        elif code == 27:
            result = replace(result, reverse=False)
        elif 30 <= code <= 37:
            result = replace(result, fg=("base", code - 30))
        elif code == 39:
            result = replace(result, fg=None)
        elif 40 <= code <= 47:
            result = replace(result, bg=("base", code - 40))
        elif code == 49:
            result = replace(result, bg=None)
        elif 90 <= code <= 97:
            result = replace(result, fg=("base", code - 90 + 8))
        elif 100 <= code <= 107:
            result = replace(result, bg=("base", code - 100 + 8))
        elif code in (38, 48):
            if index >= len(values):
                continue
            mode = values[index]
            index += 1
            if mode == 5 and index < len(values):
                color = ("x256", values[index] & 0xFF)
                index += 1
            elif mode == 2 and index + 2 < len(values):
                color = ("rgb", (values[index], values[index + 1], values[index + 2]))
                index += 3
            else:
                continue
            result = replace(result, fg=color) if code == 38 else replace(result, bg=color)
    return result


def _tokens(raw: str) -> list[tuple[str, str, int, int]]:
    """The text chunks and the escape sequences of a raw line.

    Every token carries its offset in the raw line and in the plain text.
    """
    tokens: list[tuple[str, str, int, int]] = []
    raw_offset = 0
    plain_offset = 0
    for match in ansi.ESCAPE_RE.finditer(raw):
        if match.start() > raw_offset:
            text = raw[raw_offset : match.start()]
            tokens.append(("text", text, raw_offset, plain_offset))
            plain_offset += len(text)
        tokens.append(("escape", match.group(0), match.start(), plain_offset))
        raw_offset = match.end()
    if raw_offset < len(raw):
        tokens.append(("text", raw[raw_offset:], raw_offset, plain_offset))
    return tokens


def _expand(segments: list[tuple[str, Style]]) -> list[tuple[str, Style]]:
    """The segment texts with the tabs expanded and the control characters removed."""
    expanded: list[tuple[str, Style]] = []
    column = 0
    for text, style in segments:
        buffer = []
        for char in text:
            if char == "\t":
                spaces = 4 - (column % 4)
                buffer.append(" " * spaces)
                column += spaces
            else:
                buffer.append(char)
                column += 1
        expanded.append((_CONTROL.sub("", "".join(buffer)), style))
    return [(text, style) for text, style in expanded if text]


def parse_line(raw: str) -> tuple[str, list[tuple[str, Style]]]:
    """A raw log line: the plain text and the styled segments.

    The escape sequences that are not SGR are dropped. A carriage return keeps
    the text after the last one, and the styles are rebased onto that text.
    """
    tokens = _tokens(raw)
    stripped = "".join(text for kind, text, _, _ in tokens if kind == "text")
    body = stripped.rstrip("\r")
    cut = body.rfind("\r")
    start = cut + 1 if cut >= 0 else 0
    raw_start = len(raw)
    for kind, text, offset, plain_offset in tokens:
        if kind == "text" and plain_offset + len(text) > start:
            raw_start = offset + (start - plain_offset)
            break
    style = Style()
    segments: list[tuple[str, Style]] = []
    for kind, text, offset, _ in tokens:
        if kind == "escape":
            match = _SGR_RE.fullmatch(text)
            if match:
                style = _apply_sgr(style, match.group(1))
            continue
        if offset + len(text) <= raw_start:
            continue
        chunk = text[max(0, raw_start - offset) :]
        if chunk:
            segments.append((chunk, style))
    segments = _expand(segments)
    return "".join(text for text, _ in segments), segments


def clean(line: str) -> str:
    """A captured output line as one printable row: no ANSI, no carriage-return history."""
    return parse_line(line)[0]


# -- jobs -------------------------------------------------------------------


def order_jobs(active: list[dict], finished: list[dict]) -> list[dict]:
    """Running by start time, queued by position, then finished newest first."""
    running = sorted(
        (job for job in active if job.get("state") == "running"),
        key=lambda job: (_float(job.get("started_at")), str(job.get("id"))),
    )
    queued = sorted(
        (job for job in active if job.get("state") != "running"),
        key=lambda job: (job.get("queue_position") or 1 << 30, _float(job.get("enqueued_at"))),
    )
    done = sorted(finished, key=lambda job: _float(job.get("ended_at")), reverse=True)
    return running + queued + done


def state_label(job: dict) -> str:
    state = str(job.get("state") or "?")
    if state == "queued" and job.get("queue_position"):
        return f"queued#{job['queue_position']}"
    if state in TERMINAL_STATES and job.get("exit_code") not in (None, 0):
        return f"{state}({job['exit_code']})"
    return state


def _columns(job_id: str, state: str, pool: str, elapsed: str, cmd: str, width: int) -> str:
    text = "  ".join((job_id.ljust(15), state.ljust(11), pool.ljust(7), elapsed.rjust(10), cmd))
    return text[:width]


HEADER = ("id", "state", "pool", "elapsed", "cmd")


def job_row(job: dict, now: float, width: int) -> str:
    return _columns(
        str(job.get("id") or "-"),
        state_label(job),
        str(job.get("pool") or "-"),
        clock(elapsed_of(job, now)),
        _cmd_text(job),
        width,
    )


class Output:
    """The tail of one job's log, read incrementally by byte offset.

    The colour log is read when the job has one, so the styles can be drawn;
    the plain log is the fallback. A colour log that appears later replaces
    the plain one, and that file is read from its start again, so the two
    files never disagree about the line count.
    """

    def __init__(self, job: dict) -> None:
        self.job_id = str(job.get("id") or "")
        self.out_path = job.get("out_path") or paths.job_out_path(self.job_id)
        self.color_path = job.get("color_path") or paths.job_color_path(self.job_id)
        self.path = self.out_path
        self.offset = 0
        self.partial = ""
        self.lines: list[str] = []
        self.segments: list[list[tuple[str, Style]]] = []
        self.dropped = 0
        self.from_start = False

    def use_color(self) -> None:
        """Read the colour log from its start, replacing what the plain log gave."""
        self.path = self.color_path
        self.offset = 0
        self.partial = ""
        self.lines = []
        self.segments = []
        self.dropped = 0
        self.from_start = True

    def append(self, raw: str) -> None:
        """Add one raw line: the plain text and the styled segments."""
        text, segments = parse_line(raw)
        self.lines.append(text)
        self.segments.append(segments)

    def read(self) -> bool:
        """Append what was written since the last read; True when lines changed."""
        if self.path != self.color_path and os.path.exists(self.color_path):
            self.use_color()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:
            self.offset, self.partial = 0, ""
            self.lines, self.segments, self.dropped = [], [], 0
        if size == self.offset:
            return False
        if not self.offset and size > _READ_LIMIT and not self.from_start:
            self.offset = size - _READ_LIMIT
        self.from_start = False
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(_READ_LIMIT)
        self.offset += len(data)
        chunks = (self.partial + data.decode("utf-8", errors="replace")).split("\n")
        if self.lines and self.partial:
            self.lines.pop()
            self.segments.pop()
        self.partial = chunks[-1]
        for chunk in chunks[:-1]:
            self.append(chunk)
        if self.partial:
            self.append(self.partial)
        overflow = len(self.lines) - _MAX_LINES
        if overflow > 0:
            del self.lines[:overflow]
            del self.segments[:overflow]
            self.dropped += overflow
        return True


class Palette:
    """The curses colour pairs, allocated on demand from pair 8."""

    def __init__(self, foreground: int = -1, background: int = -1) -> None:
        self.foreground = foreground
        self.background = background
        self.pairs: dict[tuple[int, int], int] = {}
        self.next = _PAIR_BASE

    def number(self, fg: Optional[int], bg: Optional[int]) -> int:
        """The pair number for a foreground and a background colour, 0 when none."""
        if not curses.has_colors():
            return 0
        colors = getattr(curses, "COLORS", 0)
        foreground = self.foreground if fg is None else fg
        background = self.background if bg is None else bg
        if foreground >= colors:
            foreground = -1
        if background >= colors:
            background = -1
        key = (foreground, background)
        if key in self.pairs:
            return self.pairs[key]
        number = 0
        limit = getattr(curses, "COLOR_PAIRS", 0) - 1
        if self.next <= limit:
            try:
                curses.init_pair(self.next, foreground, background)
                number = self.next
                self.next += 1
            except curses.error:
                number = 0
        self.pairs[key] = number
        return number

    def attr(self, fg: Optional[int], bg: Optional[int]) -> int:
        """The colour pair attribute for a foreground and a background colour."""
        number = self.number(fg, bg)
        return curses.color_pair(number) if number else 0


class App:
    def __init__(self, screen) -> None:
        self.screen = screen
        self.socket = daemon.resolve_socket_path()
        self.jobs: list[dict] = []
        self.selected_id: Optional[str] = None
        self.cursor = 0
        self.table_top = 0
        self.show_finished = False
        self.output: Optional[Output] = None
        self.outputs: dict[str, Output] = {}
        self.out_top = 0
        self.follow = True
        self.prompt: Optional[str] = None
        self.query = ""
        self.matches: list[int] = []
        self.match_index = -1
        self.message = ""
        self.last_poll = 0.0
        self.out_height = 1
        self.table_rows = 1
        self.hits: tuple[Any, int] = (None, 0)
        self.out_y = 0
        self.palette = Palette()

    # -- data ---------------------------------------------------------------

    def _ask(self, payload: dict) -> Optional[dict]:
        try:
            response = request(self.socket, payload, timeout=2.0)
        except Exception as exc:
            self.message = f"daemon unreachable: {exc}"
            return None
        if not response.get("ok"):
            self.message = str(response.get("error") or "request failed")
            return None
        return response

    def poll(self) -> None:
        self.last_poll = time.monotonic()
        active = self._ask({"op": "list", "states": ["queued", "running"], "limit": 500})
        if active is None:
            return
        finished: list[dict] = []
        if self.show_finished:
            response = self._ask(
                {"op": "list", "states": sorted(TERMINAL_STATES), "limit": _FINISHED_LIMIT}
            )
            finished = (response or {}).get("jobs") or []
        jobs = order_jobs(active.get("jobs") or [], finished)
        ids = [job.get("id") for job in jobs]
        if self.selected_id and self.selected_id not in ids:
            # keep a job that just finished on screen until the cursor leaves it
            response = self._ask({"op": "status", "id": self.selected_id})
            if response and isinstance(response.get("job"), dict):
                jobs.append(response["job"])
                ids.append(self.selected_id)
        self.message = ""
        self.jobs = jobs
        if self.selected_id in ids:
            self.cursor = ids.index(self.selected_id)
        self.select(self.cursor)

    def select(self, index: int) -> None:
        if not self.jobs:
            self.cursor, self.selected_id = 0, None
            return
        self.cursor = max(0, min(index, len(self.jobs) - 1))
        self.selected_id = self.jobs[self.cursor].get("id")

    def sync_output(self) -> None:
        """Point the output pane at the selected job, reading its log once per visit."""
        if not self.jobs or not self.selected_id:
            self.output = None
            return
        if self.output is not None and self.output.job_id == self.selected_id:
            return
        output = self.outputs.pop(self.selected_id, None) or Output(self.jobs[self.cursor])
        self.outputs[self.selected_id] = output
        while len(self.outputs) > _CACHED_OUTPUTS:
            del self.outputs[next(iter(self.outputs))]
        self.output = output
        self.out_top, self.follow, self.match_index = 0, True, -1
        output.read()
        self.research()

    def research(self) -> None:
        lines = self.output.lines if self.output else []
        self.matches = find_matches(lines, self.query)
        if self.match_index >= len(self.matches):
            self.match_index = len(self.matches) - 1

    # -- search -------------------------------------------------------------

    def jump(self, step: int) -> None:
        self.sync_output()
        if not self.matches:
            self.message = f"no match for {self.query}" if self.query else ""
            return
        if self.match_index < 0:
            middle = self.out_top
            after = [i for i, line in enumerate(self.matches) if line >= middle]
            self.match_index = after[0] if after else 0
            if step < 0:
                self.match_index = (self.match_index - 1) % len(self.matches)
        else:
            self.match_index = (self.match_index + step) % len(self.matches)
        self.follow = False
        self.out_top = max(0, self.matches[self.match_index] - self.out_height // 2)

    def on_prompt_key(self, key) -> None:
        if key == "\x1b":
            self.prompt = None
            self.research()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.query, self.prompt, self.match_index = self.prompt or "", None, -1
            self.research()
            self.jump(1)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.prompt = (self.prompt or "")[:-1]
        elif isinstance(key, str) and key.isprintable():
            self.prompt = (self.prompt or "") + key

    # -- keys ---------------------------------------------------------------

    def scroll(self, delta: int) -> None:
        self.sync_output()
        total = len(self.output.lines) if self.output else 0
        bottom = max(0, total - self.out_height)
        self.out_top = max(0, min(self.out_top + delta, bottom))
        self.follow = self.out_top >= bottom

    def on_key(self, key) -> bool:
        if self.prompt is not None:
            self.on_prompt_key(key)
            return True
        page = max(1, self.out_height - 1)
        if key in ("q", "Q"):
            return False
        if key == curses.KEY_MOUSE:
            self.on_mouse()
        elif key == "/":
            self.prompt = ""
        elif key == "n":
            self.jump(1)
        elif key == "N":
            self.jump(-1)
        elif key == "\x1b":
            self.query, self.matches, self.match_index = "", [], -1
        elif key == "a":
            self.show_finished = not self.show_finished
            self.poll()
        elif key in (curses.KEY_NPAGE, "\x04", " "):
            self.scroll(page)
        elif key in (curses.KEY_PPAGE, "\x15"):
            self.scroll(-page)
        elif key == curses.KEY_DOWN:
            self.select(self.cursor + 1)
        elif key == curses.KEY_UP:
            self.select(self.cursor - 1)
        elif key == "j":
            self.scroll(1)
        elif key == "k":
            self.scroll(-1)
        elif key == "g":
            self.scroll(-(1 << 30))
        elif key == "G":
            self.scroll(1 << 30)
        return True

    def on_mouse(self) -> None:
        try:
            _, _, y, _, state = curses.getmouse()
        except curses.error:
            return
        wheel = 0
        if state & curses.BUTTON4_PRESSED:
            wheel = -1
        elif state & getattr(curses, "BUTTON5_PRESSED", 0):
            wheel = 1
        in_output = y >= self.out_y - 1
        if wheel:
            if in_output:
                self.scroll(wheel * _WHEEL_LINES)
            else:
                self.select(self.cursor + wheel)
            return
        if in_output:
            return
        if 2 <= y < 2 + self.table_rows:
            self.select(self.table_top + y - 2)

    # -- drawing ------------------------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        room = width - x - (1 if y == height - 1 else 0)
        if room <= 0:
            return
        try:
            self.screen.addstr(y, x, text[:room], attr)
        except curses.error:
            pass

    def style_attr(self, style: Style, marked: bool, bold: bool) -> int:
        """The curses attributes of one piece of text."""
        attr = 0
        if style.bold:
            attr |= curses.A_BOLD
        if style.dim:
            attr |= curses.A_DIM
        if style.underline:
            attr |= curses.A_UNDERLINE
        if style.reverse:
            attr |= curses.A_REVERSE
        if style.italic:
            attr |= getattr(curses, "A_ITALIC", 0)
        if marked:
            attr |= curses.A_REVERSE
            if bold:
                attr |= curses.A_BOLD
        foreground = resolve_color(style.fg)
        background = resolve_color(style.bg)
        if foreground is None and background is None:
            attr |= _pair(4) if marked else 0
        else:
            attr |= self.palette.attr(foreground, background)
        return attr

    def draw_line(self, y: int, segments: list, plain: str, query: str, bold: bool) -> None:
        """One log line: the segments drawn with their styles, the matches marked."""
        if not segments:
            self.put(y, 0, plain)
            return
        ranges = spans(plain, query)
        x = 0
        offset = 0
        for text, style in segments:
            end = offset + len(text)
            hits = [(start, stop) for start, stop in ranges if start < end and stop > offset]
            for piece, marked in _split(text, offset, hits):
                self.put(y, x, piece, self.style_attr(style, marked, bold))
                x += len(piece)
            offset = end

    def draw(self) -> None:
        now = time.time()
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 6 or width < 30:
            self.put(0, 0, "terminal too small")
            screen.refresh()
            return
        running = sum(1 for job in self.jobs if job.get("state") == "running")
        queued = sum(1 for job in self.jobs if job.get("state") == "queued")
        title = f" ajq  {running} running  {queued} queued"
        if self.show_finished:
            title += "  +finished"
        self.put(0, 0, title.ljust(width), curses.A_REVERSE | curses.A_BOLD)

        table_rows = self.table_rows = max(2, min(len(self.jobs) or 1, (height - 4) * 2 // 5))
        self.put(1, 0, _columns(*HEADER, width), curses.A_DIM)
        if self.cursor < self.table_top:
            self.table_top = self.cursor
        elif self.cursor >= self.table_top + table_rows:
            self.table_top = self.cursor - table_rows + 1
        if not self.jobs:
            self.put(2, 1, "no running or queued jobs" + ("" if self.show_finished else "  (a: show finished)"), curses.A_DIM)
        for row, job in enumerate(self.jobs[self.table_top : self.table_top + table_rows]):
            index = self.table_top + row
            attr = _state_attr(str(job.get("state")))
            if index == self.cursor:
                attr |= curses.A_REVERSE
            self.put(2 + row, 0, job_row(job, now, width).ljust(width), attr)

        bar_y = 2 + table_rows
        out_y = self.out_y = bar_y + 1
        self.out_height = max(1, height - out_y - 1)
        lines = self.output.lines if self.output else []
        if self.follow:
            self.out_top = max(0, len(lines) - self.out_height)
        job = self.jobs[self.cursor] if self.jobs else {}
        bar = f"─ {job.get('id', '')} {state_label(job) if job else ''} "
        bar += "following " if self.follow else f"line {self.out_top + 1}/{len(lines)} "
        if self.output and self.output.dropped:
            bar += f"({self.output.dropped} earlier lines dropped) "
        self.put(bar_y, 0, bar.ljust(width, "─"), curses.A_DIM)
        if job and not lines:
            self.put(out_y, 1, "(no output yet)", curses.A_DIM)
        current = self.matches[self.match_index] if 0 <= self.match_index < len(self.matches) else -1
        query = self.prompt if self.prompt is not None else self.query
        for row in range(self.out_height):
            index = self.out_top + row
            if index >= len(lines):
                break
            segments = self.output.segments[index] if self.output else []
            self.draw_line(out_y + row, segments, lines[index], query, index == current)

        status_y = height - 1
        if self.prompt is not None:
            key = (self.prompt, id(self.output), len(lines))
            if self.hits[0] != key:
                self.hits = (key, len(find_matches(lines, self.prompt)))
            hits = self.hits[1]
            self.put(status_y, 0, f"/{self.prompt}")
            self.put(status_y, max(len(self.prompt) + 3, width - 16), f"{hits} lines", curses.A_DIM)
            try:
                curses.curs_set(1)
                screen.move(status_y, min(width - 2, len(self.prompt) + 1))
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            if self.message:
                self.put(status_y, 0, self.message, _pair(3))
            elif self.query:
                where = f"{self.match_index + 1}/{len(self.matches)}" if self.matches else "0/0"
                self.put(status_y, 0, f"/{self.query}  match {where}  n/N next/prev  esc clear", curses.A_DIM)
            else:
                self.put(status_y, 0, "up/dn select  j/k scroll  g/G top/bottom  / search  a finished  q quit", curses.A_DIM)
        screen.refresh()

    # -- loop ---------------------------------------------------------------

    def keys(self) -> list:
        """The next key, then every key already pending, so a burst costs one redraw."""
        pending = []
        self.screen.timeout(_FRAME_MS)
        while True:
            try:
                pending.append(self.screen.get_wch())
            except curses.error:
                return pending
            self.screen.timeout(0)

    def loop(self) -> None:
        self.poll()
        while True:
            if time.monotonic() - self.last_poll >= _POLL_S:
                self.poll()
            self.sync_output()
            if self.output and self.output.read():
                self.research()
            self.draw()
            for key in self.keys():
                if key != curses.KEY_RESIZE and not self.on_key(key):
                    return


def _split(text: str, offset: int, hits: list[tuple[int, int]]) -> list[tuple[str, bool]]:
    """The text of one segment, cut at the match boundaries."""
    pieces = []
    position = 0
    for start, stop in sorted(hits):
        start = max(start, offset) - offset
        stop = min(stop, offset + len(text)) - offset
        if start > position:
            pieces.append((text[position:start], False))
        pieces.append((text[start:stop], True))
        position = stop
    if position < len(text):
        pieces.append((text[position:], False))
    return pieces


def _pair(number: int) -> int:
    return curses.color_pair(number) if curses.has_colors() else 0


def _state_attr(state: str) -> int:
    if state == "running":
        return _pair(1)
    if state == "queued":
        return _pair(2)
    if state in ("failed", "canceled", "timeout", "lost"):
        return _pair(3)
    return curses.A_DIM


def _main(screen) -> None:
    foreground = -1
    background = -1
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            foreground = curses.COLOR_WHITE
            background = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, background)
        curses.init_pair(2, curses.COLOR_YELLOW, background)
        curses.init_pair(3, curses.COLOR_RED, background)
        curses.init_pair(4, curses.COLOR_YELLOW, background)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.idlok(True)
    curses.mouseinterval(0)
    curses.mousemask(
        curses.BUTTON1_PRESSED | curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)
    )
    app = App(screen)
    app.palette.foreground = foreground
    app.palette.background = background
    app.loop()


def run() -> int:
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        pass
    return 0
