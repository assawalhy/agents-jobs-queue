"""Protocol, paths, platform and CLI smoke tests."""

from __future__ import annotations

import json
import os
import socket
import unittest

import helpers

from ajq import paths, platform, protocol


class TestProtocol(unittest.TestCase):
    def test_encode_is_one_line(self):
        raw = protocol.encode({"op": "submit", "argv": ["echo", "hi"]})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)

    def test_request_over_a_real_socket(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind("/tmp/ajq-test-%d.sock" % os.getpid())
        server.listen(1)

        import threading

        def handle():
            conn, _ = server.accept()
            protocol.serve(conn, lambda payload: {"ok": True, "echo": payload["op"]})
            conn.close()

        path = server.getsockname()
        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        response = protocol.request(path, {"op": "ping"}, timeout=5)
        thread.join(timeout=5)
        server.close()
        os.unlink(path)
        self.assertEqual(response, {"ok": True, "echo": "ping"})

    def test_handler_exception_becomes_an_error_response(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = "/tmp/ajq-test-err-%d.sock" % os.getpid()
        server.bind(path)
        server.listen(1)

        import threading

        def boom(_payload):
            raise RuntimeError("kaboom")

        def handle():
            conn, _ = server.accept()
            protocol.serve(conn, boom)
            conn.close()

        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        response = protocol.request(path, {"op": "status", "id": "j-x"}, timeout=5)
        thread.join(timeout=5)
        server.close()
        os.unlink(path)
        self.assertFalse(response["ok"])
        self.assertIn("kaboom", response["error"])

    def test_missing_socket_raises_daemon_unavailable(self):
        with self.assertRaises(protocol.DaemonUnavailable):
            protocol.request("/tmp/ajq-not-here-%d.sock" % os.getpid(), {"op": "ping"}, timeout=2)


class TestPathsAndPlatform(helpers.AjqTestCase):
    def test_job_paths_live_under_the_state_dir(self):
        job_id = "j-abc123"
        self.assertTrue(paths.job_out_path(job_id).startswith(self.state_dir))
        self.assertTrue(paths.job_dir(job_id).startswith(paths.jobs_dir()))
        self.assertTrue(paths.job_meta_path(job_id).endswith("meta.json"))

    def test_ensure_dir_is_private(self):
        target = os.path.join(self.state_dir, "nested", "deeper")
        paths.ensure_dir(target)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)

    def test_interpreter_path_is_absolute_and_exists(self):
        path = platform.interpreter_path()
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(os.path.exists(path))

    def test_memory_probes_are_plausible(self):
        total = platform.total_memory_mb()
        available = platform.available_memory_mb()
        self.assertGreater(total, 256)
        self.assertGreaterEqual(available, 0)
        self.assertLessEqual(available, total)

    def test_cpu_count_is_positive(self):
        self.assertGreaterEqual(platform.cpu_count(), 1)

    def test_rss_of_self_is_sane(self):
        rss = platform.rss_mb(os.getpid())
        self.assertIsNotNone(rss)
        self.assertGreater(rss, 0)
        self.assertIsNone(platform.rss_mb(999999))

    def test_set_nice_does_not_raise(self):
        platform.set_nice(os.getpid(), 5)  # may fail for non-root; must not raise


class TestCli(helpers.AjqTestCase):
    def run_cli(self, argv):
        from ajq import cli

        return cli.main(argv)

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_version(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_config_print_default_is_valid_json(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.run_cli(["config", "--print-default"])
        self.assertEqual(code, 0)
        self.assertIn("max_concurrent", buffer.getvalue())
        json.loads(buffer.getvalue())

    def test_guard_explain(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.run_cli(["guard", "--explain", "npm run build"])
        self.assertEqual(code, 0)
        self.assertIn("ajq", buffer.getvalue())

    def test_command_without_daemon_fails_cleanly(self):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.run_cli(["list"])
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().strip() or out.getvalue().strip())
        self.assertNotIn("Traceback", err.getvalue())


class TestWaitTail(helpers.AjqTestCase):
    def _write_log(self, job_id, body):
        from ajq import paths

        paths.ensure_dir(paths.job_dir(job_id))
        out = paths.job_out_path(job_id)
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(body)
        return out

    def test_parser_accepts_tail(self):
        from ajq import cli

        args = cli._build_parser().parse_args(["wait", "j-x", "--tail", "5"])
        self.assertEqual(args.tail, 5)
        args = cli._build_parser().parse_args(["wait", "j-x", "-n", "3"])
        self.assertEqual(args.tail, 3)

    def test_tail_count_accepts_scientific_notation(self):
        import argparse

        from ajq import cli

        self.assertEqual(cli._tail_count("80"), 80)
        self.assertEqual(cli._tail_count("4e+24"), 1_000_000)  # capped
        self.assertEqual(cli._tail_count("-5"), 0)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._tail_count("nope")

    def test_job_tail_reads_the_last_lines(self):
        from ajq import cli

        job_id = "j-waittail"
        out = self._write_log(job_id, "a\nb\nc\nd\n")
        job = {"id": job_id, "out_path": out}
        self.assertEqual(cli._job_tail(job, 2), ["c", "d"])
        self.assertEqual(cli._job_tail(job, 0), [])
        self.assertEqual(cli._job_tail({"id": "j-missing"}, 3), [])

    def test_wait_tail_prints_state_and_log(self):
        import argparse
        import contextlib
        import io

        from ajq import cli

        job_id = "j-waittail2"
        out = self._write_log(job_id, "l1\nl2\nl3\n")
        job = {"id": job_id, "state": "failed", "elapsed_s": 1.0, "out_path": out}
        saved = cli._wait_for
        cli._wait_for = lambda _id, _timeout: job
        try:
            args = argparse.Namespace(id=job_id, timeout_s=None, tail=2, json=False, fields=None)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli._cmd_wait(args)
        finally:
            cli._wait_for = saved
        self.assertEqual(code, 2)  # a non-`done` state exits 2
        printed = buffer.getvalue()
        self.assertIn("failed", printed)
        self.assertIn("l2", printed)
        self.assertIn("l3", printed)
        self.assertNotIn("l1", printed)


class TestOutputRange(helpers.AjqTestCase):
    def _write_log(self, job_id, body):
        from ajq import paths

        paths.ensure_dir(paths.job_dir(job_id))
        out = paths.job_out_path(job_id)
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(body)
        return out

    def _run_output(self, job_id, out, argv):
        import contextlib
        import io

        from ajq import cli

        saved = cli._request
        cli._request = lambda payload, timeout=30.0: {
            "ok": True,
            "job": {"id": job_id, "state": "done", "out_path": out},
        }
        try:
            args = cli._build_parser().parse_args(["output", job_id] + argv)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli._cmd_output(args)
        finally:
            cli._request = saved
        return code, buffer.getvalue()

    def test_head_reads_the_first_lines(self):
        out = self._write_log("j-head", "a\nb\nc\nd\ne\n")
        code, printed = self._run_output("j-head", out, ["--head", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(printed, "a\nb\n")

    def test_offset_tail_reads_a_window(self):
        out = self._write_log("j-win", "a\nb\nc\nd\ne\n")
        _code, printed = self._run_output("j-win", out, ["--offset", "1", "--tail", "2"])
        self.assertEqual(printed, "b\nc\n")

    def test_offset_head_reads_a_window(self):
        out = self._write_log("j-win2", "a\nb\nc\nd\ne\n")
        _code, printed = self._run_output("j-win2", out, ["--offset", "2", "--head", "2"])
        self.assertEqual(printed, "c\nd\n")

    def test_default_tail_is_unchanged(self):
        out = self._write_log("j-def", "a\nb\nc\n")
        _code, printed = self._run_output("j-def", out, [])
        self.assertEqual(printed, "a\nb\nc\n")

    def test_offset_past_eof_is_empty(self):
        out = self._write_log("j-past", "a\nb\n")
        _code, printed = self._run_output("j-past", out, ["--offset", "10"])
        self.assertEqual(printed, "")

    def test_from_start_ignores_head_and_offset(self):
        out = self._write_log("j-all", "a\nb\nc\n")
        _code, printed = self._run_output(
            "j-all", out, ["--from-start", "--head", "1", "--offset", "1"]
        )
        self.assertEqual(printed, "a\nb\nc\n")


class TestCancelCli(helpers.AjqTestCase):
    def test_kill_alias_parses(self):
        from ajq import cli

        args = cli._build_parser().parse_args(["kill", "j-x"])
        self.assertEqual(args.id, "j-x")
        self.assertFalse(args.no_wait)

    def test_no_wait_flag_parses(self):
        from ajq import cli

        args = cli._build_parser().parse_args(["cancel", "j-x", "--no-wait"])
        self.assertTrue(args.no_wait)

    def test_cancel_waits_and_reports_the_final_state(self):
        import argparse
        import contextlib
        import io

        from ajq import cli

        saved_req, saved_wait = cli._request, cli._wait_for
        cli._request = lambda payload, timeout=30.0: {
            "ok": True,
            "job": {"id": "j-c", "state": "running", "kill_grace_s": 1.0},
        }
        cli._wait_for = lambda job_id, timeout: {
            "id": job_id,
            "state": "canceled",
            "kill_reason": "canceled",
            "signal": 15,
        }
        try:
            args = argparse.Namespace(id="j-c", no_wait=False, json=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli._cmd_cancel(args)
        finally:
            cli._request, cli._wait_for = saved_req, saved_wait
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("canceled", printed)
        self.assertIn("signal 15", printed)

    def test_cancel_no_wait_reports_still_running(self):
        import argparse
        import contextlib
        import io

        from ajq import cli

        saved = cli._request
        cli._request = lambda payload, timeout=30.0: {
            "ok": True,
            "job": {"id": "j-nw", "state": "running", "kill_grace_s": 1.0},
        }
        try:
            args = argparse.Namespace(id="j-nw", no_wait=True, json=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli._cmd_cancel(args)
        finally:
            cli._request = saved
        self.assertEqual(code, 2)
        self.assertIn("running", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()