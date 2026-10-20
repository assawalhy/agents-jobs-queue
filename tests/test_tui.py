"""TUI helpers and the bare `ajq` tty gate. The curses loop itself is not driven here."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401

from ajq import cli, tui


class ClockTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(tui.clock(None), "-")
        self.assertEqual(tui.clock(0), "0s")
        self.assertEqual(tui.clock(59.9), "59s")
        self.assertEqual(tui.clock(62), "1m 2s")
        self.assertEqual(tui.clock(3600), "1h 0m 0s")
        self.assertEqual(tui.clock(3723), "1h 2m 3s")
        self.assertEqual(tui.clock(-4), "0s")


class ElapsedTest(unittest.TestCase):
    def test_running_counts_from_start(self):
        job = {"state": "running", "enqueued_at": 100.0, "started_at": 150.0}
        self.assertEqual(tui.elapsed_of(job, 212.0), 62.0)

    def test_queued_counts_from_enqueue(self):
        self.assertEqual(tui.elapsed_of({"enqueued_at": 100.0}, 130.0), 30.0)

    def test_finished_is_runtime(self):
        job = {"started_at": 100.0, "ended_at": 160.0}
        self.assertEqual(tui.elapsed_of(job, 9999.0), 60.0)

    def test_no_timestamps_falls_back_to_payload(self):
        self.assertEqual(tui.elapsed_of({"elapsed_s": 7.0}, 1.0), 7.0)


class SearchTest(unittest.TestCase):
    LINES = ["BUILD FAILED", "error: boom", "Error: Boom", "ok"]

    def test_lowercase_query_ignores_case(self):
        self.assertEqual(tui.find_matches(self.LINES, "error"), [1, 2])

    def test_uppercase_query_is_case_sensitive(self):
        self.assertEqual(tui.find_matches(self.LINES, "Error"), [2])

    def test_empty_query(self):
        self.assertEqual(tui.find_matches(self.LINES, ""), [])

    def test_spans(self):
        self.assertEqual(tui.spans("a-ab-AB", "ab"), [(2, 4), (5, 7)])
        self.assertEqual(tui.spans("a-ab-AB", "AB"), [(5, 7)])


class CleanTest(unittest.TestCase):
    def test_strips_ansi_and_carriage_return_history(self):
        self.assertEqual(tui.clean("\x1b[32mok\x1b[0m"), "ok")
        self.assertEqual(tui.clean("10%\r50%\r100%"), "100%")
        self.assertEqual(tui.clean("a\tb\x07"), "a   b")


class OrderTest(unittest.TestCase):
    def test_running_then_queued_then_finished(self):
        active = [
            {"id": "q2", "state": "queued", "queue_position": 2},
            {"id": "r2", "state": "running", "started_at": 20.0},
            {"id": "q1", "state": "queued", "queue_position": 1},
            {"id": "r1", "state": "running", "started_at": 10.0},
        ]
        finished = [{"id": "d1", "ended_at": 1.0}, {"id": "d2", "ended_at": 2.0}]
        ids = [job["id"] for job in tui.order_jobs(active, finished)]
        self.assertEqual(ids, ["r1", "r2", "q1", "q2", "d2", "d1"])


class OutputTest(unittest.TestCase):
    def test_incremental_read_joins_partial_lines(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.log")
            with open(path, "w") as handle:
                handle.write("one\ntw")
            output = tui.Output({"id": "j-1", "out_path": path})
            self.assertTrue(output.read())
            self.assertEqual(output.lines, ["one", "tw"])
            self.assertFalse(output.read())
            with open(path, "a") as handle:
                handle.write("o\nthree\n")
            self.assertTrue(output.read())
            self.assertEqual(output.lines, ["one", "two", "three"])

    def test_missing_file(self):
        output = tui.Output({"id": "j-1", "out_path": "/nonexistent/out.log"})
        self.assertFalse(output.read())
        self.assertEqual(output.lines, [])


class BareCommandTest(unittest.TestCase):
    def test_non_tty_prints_help(self):
        buffer = io.StringIO()
        with mock.patch.object(tui, "run") as run, contextlib.redirect_stdout(buffer):
            self.assertEqual(cli.main([]), 0)
        run.assert_not_called()
        self.assertIn("usage", buffer.getvalue())

    def test_tty_opens_tui(self):
        with mock.patch.object(tui, "run", return_value=0) as run, mock.patch.object(
            cli, "_require_daemon", return_value=True
        ), mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "sys.stdout.isatty", return_value=True
        ):
            self.assertEqual(cli.main([]), 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
