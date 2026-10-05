"""Field-selection tests: `--fields` / `--select` on status, list and wait.

The point is token economy for agents: they should never need to pipe --json
into a python parser to read three keys.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

import helpers

JOB = {
    "id": "j-abc123",
    "state": "running",
    "pool": "heavy",
    "kind": "test",
    "elapsed_s": 12.5,
    "out_bytes": 4096,
    "exit_code": None,
    "queue_position": None,
    "eta_run_s": 95.0,
    "kill_reason": "",
    "argv": ["pytest", "-q"],
    "cwd": "/tmp",
}


class FieldCase(unittest.TestCase):
    def run_cli(self, argv, stdout=None):
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer or stdout or io.StringIO()):
            code = cli.main(argv)
        return code, buffer.getvalue()


class TestFieldParsing(unittest.TestCase):
    def test_comma_separated_and_repeated(self):
        from ajq.cli import _field_names

        self.assertEqual(_field_names("a,b,c"), ["a", "b", "c"])
        self.assertEqual(_field_names(["a,b", "c"]), ["a", "b", "c"])
        self.assertEqual(_field_names(" a , b , a "), ["a", "b"])
        self.assertEqual(_field_names(None), [])
        self.assertEqual(_field_names(""), [])

    def test_projection_keeps_requested_keys_only(self):
        from ajq.cli import _project

        got = _project(JOB, ["state", "elapsed_s"])
        self.assertEqual(got, {"state": "running", "elapsed_s": 12.5})

    def test_projection_of_a_missing_key_is_null_not_absent(self):
        from ajq.cli import _project

        self.assertEqual(_project(JOB, ["nope"]), {"nope": None})

    def test_projection_passes_payload_through_without_fields(self):
        from ajq.cli import _project

        self.assertEqual(_project(JOB, []), JOB)

    def test_projection_of_a_job_list_wrapper(self):
        from ajq.cli import _project

        payload = {"jobs": [JOB, dict(JOB, id="j-2")], "count": 2}
        got = _project(payload, ["id", "state"])
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["jobs"], [{"id": "j-abc123", "state": "running"},
                                      {"id": "j-2", "state": "running"}])


class TestScalarFormatting(unittest.TestCase):
    def test_none_and_float_and_list(self):
        from ajq.cli import _scalar

        self.assertEqual(_scalar(None), "-")
        self.assertEqual(_scalar(12.5), "12.5")
        self.assertEqual(_scalar(95.0), "95")
        self.assertEqual(_scalar(["a", "b"]), "a b")


class TestFieldOutput(FieldCase, helpers.AjqTestCase):
    """Drive the real CLI against a stubbed daemon response."""

    def setUp(self):
        super().setUp()
        from ajq import cli

        self.cli = cli
        self._requests = []

        def fake_request(payload, *args, **kwargs):
            self._requests.append(payload)
            op = payload.get("op")
            if op == "status":
                return {"ok": True, "job": dict(JOB)}
            if op == "list":
                return {"ok": True, "jobs": [dict(JOB), dict(JOB, id="j-def456")], "count": 2}
            if op == "wait":
                return {"ok": True, "job": dict(JOB, state="done", exit_code=0)}
            return {"ok": True}

        # capture the originals *before* stubbing, or addCleanup would restore
        # the stub and leak it into every later test module
        original_request = self.cli._request
        original_require = self.cli._require_daemon
        self.addCleanup(setattr, self.cli, "_request", original_request)
        self.addCleanup(setattr, self.cli, "_require_daemon", original_require)
        self.cli._request = fake_request
        self.cli._require_daemon = lambda: True

    def test_status_fields_human_line(self):
        code, out = self.run_cli(["status", "j-abc123", "--fields", "state,elapsed_s,out_bytes"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "state=running elapsed_s=12.5 out_bytes=4096")

    def test_status_fields_json(self):
        code, out = self.run_cli(
            ["status", "j-abc123", "--fields", "state,exit_code", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"exit_code": None, "state": "running"})

    def test_select_is_an_alias_for_fields(self):
        _, out = self.run_cli(["status", "j-abc123", "--select", "state", "--json"])
        self.assertEqual(json.loads(out), {"state": "running"})

    def test_field_output_is_shorter_than_full_json(self):
        _, terse = self.run_cli(["status", "j-abc123", "--fields", "state,elapsed_s"])
        _, full = self.run_cli(["status", "j-abc123", "--json"])
        self.assertLess(len(terse), len(full) / 2)

    def test_list_fields_one_line_per_job(self):
        code, out = self.run_cli(["list", "--fields", "id,state"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.strip().splitlines(),
            ["id=j-abc123 state=running", "id=j-def456 state=running"],
        )

    def test_list_fields_json_keeps_the_wrapper(self):
        _, out = self.run_cli(["list", "--fields", "id", "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["jobs"], [{"id": "j-abc123"}, {"id": "j-def456"}])

    def test_wait_fields(self):
        code, out = self.run_cli(["wait", "j-abc123", "--fields", "state,exit_code"])
        self.assertEqual(out.strip(), "state=done exit_code=0")
        self.assertEqual(code, 0)

    def test_wait_fields_still_signals_failure(self):
        self.cli._request = lambda payload, *a, **k: {"ok": True, "job": dict(JOB, state="failed")}
        code, out = self.run_cli(["wait", "j-abc123", "--fields", "state"])
        self.assertEqual(code, 2)
        self.assertIn("state=failed", out)

    def test_without_fields_the_json_is_unchanged(self):
        _, out = self.run_cli(["status", "j-abc123", "--json"])
        self.assertEqual(json.loads(out)["argv"], ["pytest", "-q"])


if __name__ == "__main__":
    unittest.main()