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


class ParseTest(unittest.TestCase):
    def test_a_plain_line_has_one_default_segment(self):
        text, segments = tui.parse_line("hello")
        self.assertEqual(text, "hello")
        self.assertEqual(segments, [("hello", tui.Style())])

    def test_the_colour_runs(self):
        text, segments = tui.parse_line("\x1b[31mred\x1b[0m plain")
        self.assertEqual(text, "red plain")
        self.assertEqual(segments[0], ("red", tui.Style(fg=("base", 1))))
        self.assertEqual(segments[1], (" plain", tui.Style()))

    def test_the_attributes(self):
        _, segments = tui.parse_line("\x1b[1;2;3;4;7mx")
        style = segments[0][1]
        self.assertTrue(style.bold)
        self.assertTrue(style.dim)
        self.assertTrue(style.italic)
        self.assertTrue(style.underline)
        self.assertTrue(style.reverse)

    def test_the_bright_colours(self):
        _, segments = tui.parse_line("\x1b[92;104mx")
        self.assertEqual(segments[0][1].fg, ("base", 10))
        self.assertEqual(segments[0][1].bg, ("base", 12))

    def test_the_background_colours(self):
        _, segments = tui.parse_line("\x1b[44mx")
        self.assertEqual(segments[0][1].bg, ("base", 4))

    def test_the_256_colour(self):
        _, segments = tui.parse_line("\x1b[38;5;196mx")
        self.assertEqual(segments[0][1].fg, ("x256", 196))

    def test_the_true_colour(self):
        _, segments = tui.parse_line("\x1b[38;2;10;20;30mx")
        self.assertEqual(segments[0][1].fg, ("rgb", (10, 20, 30)))

    def test_the_reset(self):
        _, segments = tui.parse_line("\x1b[1;31mx\x1b[0my")
        self.assertEqual(segments[0][1], tui.Style(bold=True, fg=("base", 1)))
        self.assertEqual(segments[1][1], tui.Style())

    def test_an_empty_parameter_list_is_a_reset(self):
        _, segments = tui.parse_line("\x1b[1;31mx\x1b[my")
        self.assertEqual(segments[1][1], tui.Style())

    def test_the_sequences_that_are_not_sgr_are_dropped(self):
        text, segments = tui.parse_line("\x1b[2Jcleared\x1b[?25l")
        self.assertEqual(text, "cleared")
        self.assertEqual(segments, [("cleared", tui.Style())])

    def test_the_carriage_return_rebases_the_style(self):
        text, segments = tui.parse_line("\x1b[32m10%\r50%\r100%")
        self.assertEqual(text, "100%")
        self.assertEqual(segments, [("100%", tui.Style(fg=("base", 2)))])

    def test_the_style_after_the_carriage_return_wins(self):
        text, segments = tui.parse_line("\x1b[32m10%\r\x1b[31m50%")
        self.assertEqual(text, "50%")
        self.assertEqual(segments, [("50%", tui.Style(fg=("base", 1)))])

    def test_the_plain_text_is_the_segments(self):
        text, segments = tui.parse_line("\x1b[1;32mok\x1b[0m done")
        self.assertEqual(text, "".join(piece for piece, _ in segments))

    def test_the_colon_form_of_the_parameters(self):
        text, segments = tui.parse_line("\x1b[38:2::10:20:30mx")
        self.assertEqual(text, "x")
        self.assertEqual(segments[0][1].fg, ("rgb", (10, 20, 30)))

    def test_the_colon_form_of_the_palette(self):
        _, segments = tui.parse_line("\x1b[38:5:196mx")
        self.assertEqual(segments[0][1].fg, ("x256", 196))


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


class ColorOutputTest(unittest.TestCase):
    def make_job(self, root):
        out_path = os.path.join(root, "out.log")
        color_path = os.path.join(root, "color.log")
        return {"id": "j-1", "out_path": out_path, "color_path": color_path}, out_path, color_path

    def test_reads_the_color_log_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as root:
            job, out_path, color_path = self.make_job(root)
            with open(out_path, "w") as handle:
                handle.write("plain\n")
            with open(color_path, "w") as handle:
                handle.write("\x1b[31mred\n")
            output = tui.Output(job)
            self.assertTrue(output.read())
            self.assertEqual(output.path, color_path)
            self.assertEqual(output.lines, ["red"])
            self.assertEqual(output.segments[0][0][1].fg, ("base", 1))

    def test_reads_the_plain_log_when_there_is_no_color_log(self):
        with tempfile.TemporaryDirectory() as root:
            job, out_path, color_path = self.make_job(root)
            with open(out_path, "w") as handle:
                handle.write("plain\n")
            output = tui.Output(job)
            self.assertTrue(output.read())
            self.assertEqual(output.path, out_path)
            self.assertEqual(output.lines, ["plain"])
            self.assertEqual(output.segments, [[("plain", tui.Style())]])

    def test_switches_to_the_color_log_when_it_appears(self):
        with tempfile.TemporaryDirectory() as root:
            job, out_path, color_path = self.make_job(root)
            with open(out_path, "w") as handle:
                handle.write("plain\n")
            output = tui.Output(job)
            self.assertTrue(output.read())
            self.assertEqual(output.lines, ["plain"])
            with open(color_path, "w") as handle:
                handle.write("\x1b[31mred\n")
            self.assertTrue(output.read())
            self.assertEqual(output.path, color_path)
            self.assertEqual(output.lines, ["red"])
            self.assertEqual(output.segments[0][0][1].fg, ("base", 1))


class FakeScreen:
    def __init__(self, keys):
        self.pending = list(keys)
        self.timeouts = []

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        if not self.pending:
            raise tui.curses.error("no input")
        return self.pending.pop(0)


class AppTest(unittest.TestCase):
    def test_keys_drains_a_burst(self):
        screen = FakeScreen(["j", "j", "k"])
        app = tui.App(screen)
        self.assertEqual(app.keys(), ["j", "j", "k"])
        self.assertEqual(screen.timeouts, [tui._FRAME_MS, 0, 0, 0])

    def test_moving_the_cursor_reads_only_the_job_it_stops_on(self):
        app = tui.App(FakeScreen([]))
        app.jobs = [{"id": f"j-{n}", "state": "running"} for n in range(3)]
        app.select(0)
        with mock.patch.object(tui, "Output") as output_class:
            output_class.side_effect = lambda job: mock.Mock(job_id=job["id"], lines=[])
            app.select(1)
            app.select(2)
            app.sync_output()
            app.select(0)
            app.sync_output()
            app.select(2)
            app.sync_output()
        self.assertEqual([c.args[0]["id"] for c in output_class.call_args_list], ["j-2", "j-0"])

    def test_wheel_scrolls_the_pane_under_the_pointer(self):
        app = tui.App(FakeScreen([]))
        app.jobs = [{"id": f"j-{n}", "state": "running"} for n in range(3)]
        app.select(0)
        app.table_rows, app.out_y, app.out_height = 3, 6, 5
        app.output = tui.Output({"id": "j-0", "out_path": "/nonexistent"})
        app.output.lines = [str(n) for n in range(50)]
        app.out_top = 45
        down = getattr(tui.curses, "BUTTON5_PRESSED", 0)
        with mock.patch.object(tui.curses, "getmouse", return_value=(0, 0, 10, 0, tui.curses.BUTTON4_PRESSED)):
            app.on_mouse()
        self.assertEqual((app.cursor, app.out_top, app.follow), (0, 45 - tui._WHEEL_LINES, False))
        if down:
            with mock.patch.object(tui.curses, "getmouse", return_value=(0, 0, 3, 0, down)):
                app.on_mouse()
            self.assertEqual(app.cursor, 1)


class KeyTest(unittest.TestCase):
    def make_app(self):
        app = tui.App(FakeScreen([]))
        app.jobs = [{"id": f"j-{n}", "state": "running"} for n in range(3)]
        app.select(0)
        app.output = tui.Output({"id": "j-0", "out_path": "/nonexistent/out.log"})
        app.output.lines = [str(n) for n in range(50)]
        app.out_height = 10
        app.out_top = 5
        return app

    def test_the_arrows_move_the_job_selection(self):
        app = self.make_app()
        app.on_key(tui.curses.KEY_DOWN)
        self.assertEqual(app.cursor, 1)
        self.assertEqual(app.out_top, 5)
        app.on_key(tui.curses.KEY_UP)
        self.assertEqual(app.cursor, 0)
        self.assertEqual(app.out_top, 5)

    def test_j_and_k_scroll_the_log(self):
        app = self.make_app()
        app.on_key("j")
        self.assertEqual(app.out_top, 6)
        self.assertEqual(app.cursor, 0)
        app.on_key("k")
        self.assertEqual(app.out_top, 5)
        self.assertEqual(app.cursor, 0)

    def test_g_goes_to_the_top_of_the_log(self):
        app = self.make_app()
        app.on_key("g")
        self.assertEqual(app.out_top, 0)
        self.assertFalse(app.follow)

    def test_G_goes_to_the_bottom_and_follows(self):
        app = self.make_app()
        app.on_key("G")
        self.assertEqual(app.out_top, 40)
        self.assertTrue(app.follow)

    def test_the_removed_keys_do_nothing(self):
        app = self.make_app()
        removed = ("\t", "h", "l", tui.curses.KEY_LEFT, tui.curses.KEY_RIGHT)
        removed += (tui.curses.KEY_HOME, tui.curses.KEY_END)
        for key in removed:
            app.on_key(key)
            self.assertEqual(app.cursor, 0)
            self.assertEqual(app.out_top, 5)
        self.assertFalse(hasattr(app, "focus"))

    def test_the_page_keys_move_the_log(self):
        app = self.make_app()
        app.on_key(" ")
        self.assertEqual(app.out_top, 14)
        app.on_key(tui.curses.KEY_PPAGE)
        self.assertEqual(app.out_top, 5)
        self.assertEqual(app.cursor, 0)


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
