"""Interactive terminal view of the queue, opened by a bare `ajq` on a tty.

The job table polls the daemon once a second. Elapsed time is recomputed on
every redraw from the job's timestamps, with the same formula as
`Scheduler._enrich`, so the counter ticks between polls.
"""

from __future__ import annotations

import curses
import os
import re
import time
from typing import Any, Optional

from . import daemon, paths
from .cli import _cmd_text
from .protocol import request
from .store import TERMINAL_STATES

_POLL_S = 1.0
_FRAME_MS = 250
_MAX_LINES = 50_000
_READ_LIMIT = 4 * 1024 * 1024
_FINISHED_LIMIT = 15
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


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


def clean(line: str) -> str:
    """A captured output line as one printable row: no ANSI, no carriage-return history."""
    line = _ANSI.sub("", line)
    if "\r" in line:
        line = line.rstrip("\r").rsplit("\r", 1)[-1]
    return _CONTROL.sub("", line.expandtabs(4))


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
    """The tail of one job's out.log, read incrementally by byte offset."""

    def __init__(self, job: dict) -> None:
        self.job_id = str(job.get("id") or "")
        self.path = job.get("out_path") or paths.job_out_path(self.job_id)
        self.offset = 0
        self.partial = ""
        self.lines: list[str] = []
        self.dropped = 0

    def read(self) -> bool:
        """Append what was written since the last read; True when lines changed."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:
            self.offset, self.partial, self.lines, self.dropped = 0, "", [], 0
        if size == self.offset:
            return False
        if not self.offset and size > _READ_LIMIT:
            self.offset = size - _READ_LIMIT
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(_READ_LIMIT)
        self.offset += len(data)
        chunks = (self.partial + data.decode("utf-8", errors="replace")).split("\n")
        if self.lines and self.partial:
            self.lines.pop()
        self.partial = chunks[-1]
        self.lines.extend(clean(chunk) for chunk in chunks[:-1])
        if self.partial:
            self.lines.append(clean(self.partial))
        overflow = len(self.lines) - _MAX_LINES
        if overflow > 0:
            del self.lines[:overflow]
            self.dropped += overflow
        return True


class App:
    def __init__(self, screen) -> None:
        self.screen = screen
        self.socket = daemon.resolve_socket_path()
        self.jobs: list[dict] = []
        self.selected_id: Optional[str] = None
        self.cursor = 0
        self.table_top = 0
        self.show_finished = False
        self.focus = "jobs"
        self.output: Optional[Output] = None
        self.out_top = 0
        self.follow = True
        self.prompt: Optional[str] = None
        self.query = ""
        self.matches: list[int] = []
        self.match_index = -1
        self.message = ""
        self.last_poll = 0.0
        self.out_height = 1

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
            self.cursor, self.selected_id, self.output = 0, None, None
            return
        self.cursor = max(0, min(index, len(self.jobs) - 1))
        job = self.jobs[self.cursor]
        self.selected_id = job.get("id")
        if self.output is None or self.output.job_id != self.selected_id:
            self.output = Output(job)
            self.out_top, self.follow, self.match_index = 0, True, -1
            self.output.read()
            self.research()

    def research(self) -> None:
        lines = self.output.lines if self.output else []
        self.matches = find_matches(lines, self.query)
        if self.match_index >= len(self.matches):
            self.match_index = len(self.matches) - 1

    # -- search -------------------------------------------------------------

    def jump(self, step: int) -> None:
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
        if key == "\t":
            self.focus = "output" if self.focus == "jobs" else "jobs"
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
        elif self.focus == "jobs":
            if key in ("j", curses.KEY_DOWN):
                self.select(self.cursor + 1)
            elif key in ("k", curses.KEY_UP):
                self.select(self.cursor - 1)
            elif key in ("g", curses.KEY_HOME):
                self.select(0)
            elif key in ("G", curses.KEY_END):
                self.select(len(self.jobs) - 1)
            elif key in ("\n", "\r", curses.KEY_ENTER, "l", curses.KEY_RIGHT):
                self.focus = "output"
        else:
            if key in ("j", curses.KEY_DOWN):
                self.scroll(1)
            elif key in ("k", curses.KEY_UP):
                self.scroll(-1)
            elif key in ("g", curses.KEY_HOME):
                self.scroll(-(1 << 30))
            elif key in ("G", curses.KEY_END):
                self.scroll(1 << 30)
            elif key in ("h", curses.KEY_LEFT):
                self.focus = "jobs"
        return True

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

        table_rows = max(2, min(len(self.jobs) or 1, (height - 4) * 2 // 5))
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
                attr |= curses.A_REVERSE if self.focus == "jobs" else curses.A_BOLD
            self.put(2 + row, 0, job_row(job, now, width).ljust(width), attr)

        bar_y = 2 + table_rows
        out_y = bar_y + 1
        self.out_height = max(1, height - out_y - 1)
        lines = self.output.lines if self.output else []
        if self.follow:
            self.out_top = max(0, len(lines) - self.out_height)
        job = self.jobs[self.cursor] if self.jobs else {}
        bar = f"─ {job.get('id', '')} {state_label(job) if job else ''} "
        bar += "following " if self.follow else f"line {self.out_top + 1}/{len(lines)} "
        if self.output and self.output.dropped:
            bar += f"({self.output.dropped} earlier lines dropped) "
        self.put(bar_y, 0, bar.ljust(width, "─"), curses.A_BOLD if self.focus == "output" else curses.A_DIM)
        if job and not lines:
            self.put(out_y, 1, "(no output yet)", curses.A_DIM)
        current = self.matches[self.match_index] if 0 <= self.match_index < len(self.matches) else -1
        query = self.prompt if self.prompt is not None else self.query
        for row in range(self.out_height):
            index = self.out_top + row
            if index >= len(lines):
                break
            line = lines[index]
            self.put(out_y + row, 0, line)
            for start, end in spans(line, query):
                attr = curses.A_REVERSE | (curses.A_BOLD if index == current else 0)
                self.put(out_y + row, start, line[start:end], attr | _pair(4))

        status_y = height - 1
        if self.prompt is not None:
            hits = len(find_matches(lines, self.prompt))
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
                self.put(status_y, 0, "j/k move  tab pane  / search  g/G top/bottom  a finished  q quit", curses.A_DIM)
        screen.refresh()

    # -- loop ---------------------------------------------------------------

    def loop(self) -> None:
        self.screen.timeout(_FRAME_MS)
        self.poll()
        while True:
            if time.monotonic() - self.last_poll >= _POLL_S:
                self.poll()
            if self.output and self.output.read():
                self.research()
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if key == curses.KEY_RESIZE:
                continue
            if not self.on_key(key):
                return


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
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, background)
        curses.init_pair(2, curses.COLOR_YELLOW, background)
        curses.init_pair(3, curses.COLOR_RED, background)
        curses.init_pair(4, curses.COLOR_YELLOW, background)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    App(screen).loop()


def run() -> int:
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        pass
    return 0
