"""Execution tests: real child processes, timeout, output cap, memory kill.

These use the POSIX backend (direct spawn) rather than systemd scopes, so they
assert the platform-independent guarantees: exit code, timeout kill, output cap,
truncation marker, and the RSS watchdog.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

import helpers

from ajq.backends.base import Handle
from ajq.exec import JobRunner

PY = "python3"


def make_job(state_dir, job_id, argv, **overrides):
    from ajq import paths

    job = {
        "id": job_id,
        "argv": list(argv),
        "cwd": state_dir,
        "timeout_s": 30,
        "max_output_bytes": 8 * 1024 * 1024,
        "kill_grace_s": 1,
        "memory_mb": None,
    }
    job.update(overrides)
    os.makedirs(paths.job_dir(job_id), exist_ok=True)
    return job


class TestJobRunner(helpers.AjqTestCase):
    def runner(self):
        from ajq.backends.macos import MacosBackend

        return JobRunner(MacosBackend(), poll_interval=0.05)

    def out(self, job_id):
        from ajq import paths

        with open(paths.job_out_path(job_id), encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_success_records_exit_code_and_output(self):
        job = make_job(self.state_dir, "j-ok", [PY, "-c", "print('hello')"])
        result = self.runner().run(job, threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.kill_reason)
        self.assertFalse(result.truncated)
        self.assertIn("hello", self.out("j-ok"))
        self.assertGreater(result.out_bytes, 0)

    def test_nonzero_exit_is_reported(self):
        job = make_job(self.state_dir, "j-fail", [PY, "-c", "import sys; sys.exit(3)"])
        result = self.runner().run(job, threading.Event())
        self.assertEqual(result.exit_code, 3)
        self.assertIsNone(result.kill_reason)

    def test_timeout_kills_and_reports(self):
        job = make_job(self.state_dir, "j-to", [PY, "-c", "import time; time.sleep(30)"], timeout_s=1)
        result = self.runner().run(job, threading.Event())
        self.assertEqual(result.kill_reason, "timeout")
        self.assertLess(result.elapsed_s, 15)
        self.assertTrue(result.exit_code != 0)

    def test_output_cap_terminates_and_marks_truncated(self):
        job = make_job(
            self.state_dir,
            "j-big",
            [PY, "-c", "print('x' * 500000)"],
            max_output_bytes=2000,
        )
        result = self.runner().run(job, threading.Event())
        self.assertEqual(result.kill_reason, "output_limit")
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(self.out("j-big").encode()), 2000 + 4096)

    def test_cancel_event_stops_the_job(self):
        job = make_job(self.state_dir, "j-cancel", [PY, "-c", "import time; time.sleep(30)"])
        cancel = threading.Event()
        runner = self.runner()

        def trip():
            time.sleep(0.5)
            cancel.set()

        threading.Thread(target=trip, daemon=True).start()
        result = runner.run(job, cancel)
        self.assertEqual(result.kill_reason, "canceled")

    def test_memory_limit_kills_a_greedy_child(self):
        job = make_job(
            self.state_dir,
            "j-mem",
            [
                PY,
                "-c",
                "import time\n"
                "ballast = []\n"
                "for _ in range(600):\n"
                "    ballast.append(bytearray(1024 * 1024))\n"
                "    time.sleep(0.02)\n"
                "time.sleep(30)\n",
            ],
            memory_mb=200,
            timeout_s=25,
        )
        result = self.runner().run(job, threading.Event())
        self.assertEqual(result.kill_reason, "memory_limit")


class TestBackends(helpers.AjqTestCase):
    def test_get_backend_auto_is_platform_correct(self):
        from ajq import platform
        from ajq.backends import get_backend

        backend = get_backend("auto")
        expected = "linux" if platform.IS_LINUX else "macos"
        self.assertTrue(backend.name.startswith(expected), backend.name)

    def test_get_backend_rejects_nonsense(self):
        from ajq.backends import get_backend

        with self.assertRaises(ValueError):
            get_backend("plan9")

    def test_posix_backend_spawn_and_kill(self):
        import signal

        from ajq.backends.macos import MacosBackend

        backend = MacosBackend()
        handle = backend.spawn(
            {"id": "j-kill", "memory_mb": 100, "cpu_percent": 50}, [PY, "-c", "import time; time.sleep(30)"]
        )
        self.assertIsInstance(handle, Handle)
        self.assertGreaterEqual(handle.pid, 0)
        time.sleep(0.3)
        backend.kill(handle, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if backend.rss_mb(handle.pid) is None:
                break
            time.sleep(0.1)
        self.assertIsNone(backend.rss_mb(handle.pid))
        handle.release()


if __name__ == "__main__":
    unittest.main()