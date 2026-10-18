"""`ajq start`: the forced start of the whole queue, and the `list` command column.

The scheduler tests drive real short-lived processes through the posix backend;
the CLI tests stub `_request` so no daemon is needed.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest

import helpers

from ajq.backends.macos import MacosBackend
from ajq.exec import JobRunner
from ajq.scheduler import Scheduler

PY = "python3"
SLEEP = [PY, "-c", "import time; time.sleep(1)"]

PLAN = {
    "total": 3,
    "startable": 2,
    "blocked_by_serial": 1,
    "memory_mb": 12288,
    "cpu_percent": 400,
    "available_memory_mb": 8000,
    "total_memory_mb": 32000,
    "cpu_count": 8,
    "max_concurrent": 8,
    "memory_headroom": 1.2,
    "pool_counts": {"heavy": 2},
    "pool_caps": {"heavy": 2, "light": 6},
    "jobs": [
        {
            "id": "j-1a2b3c4d5e6f",
            "state": "queued",
            "pool": "heavy",
            "memory_mb": 6144,
            "cpu_percent": 200,
            "argv": ["pytest", "tests/api", "-q", "--maxfail=1"],
        },
        {
            "id": "j-2a2b3c4d5e6f",
            "state": "queued",
            "pool": "heavy",
            "memory_mb": 6144,
            "cpu_percent": 200,
            "argv": ["npm", "run", "build"],
        },
    ],
}


def make_scheduler(self, **overrides):
    store = helpers.store()
    self.addCleanup(store.close)
    config = helpers.load_config(**overrides)
    backend = MacosBackend()
    runner = JobRunner(backend, poll_interval=0.05)
    return Scheduler(store, config, runner, backend), store


def wait_for_state(scheduler, job_id, states, timeout=15.0):
    return helpers.wait_until(
        lambda: (scheduler.store.get(job_id) or {}).get("state") in states, timeout=timeout
    )


def subdir(root, name):
    """A fresh directory with its own worktree serial key."""
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    return path


class TestStartAllPlan(helpers.AjqTestCase):
    def test_plan_counts_memory_and_cpu_from_the_pools(self):
        scheduler, _ = make_scheduler(
            self,
            resources={"memory_mb": 2048, "pool_memory_mb": {"heavy": 4096}, "cpu_percent": 200},
        )
        scheduler.submit(argv=SLEEP, cwd=subdir(self.state_dir, "a"), pool="heavy")
        scheduler.submit(argv=SLEEP, cwd=subdir(self.state_dir, "b"), pool="light")
        plan = scheduler.start_all_plan()
        self.assertEqual(plan["total"], 2)
        self.assertEqual(plan["startable"], 2)
        self.assertEqual(plan["blocked_by_serial"], 0)
        self.assertEqual(plan["memory_mb"], 4096 + 2048)
        self.assertEqual(plan["cpu_percent"], 400)
        self.assertEqual(plan["pool_counts"], {"heavy": 1, "light": 1})
        self.assertEqual(plan["pool_caps"]["heavy"], 2)
        self.assertEqual([row["memory_mb"] for row in plan["jobs"]], [4096, 2048])

    def test_a_shared_worktree_job_is_blocked_unless_ignored(self):
        scheduler, _ = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"heavy": 1}})
        first = scheduler.submit(argv=SLEEP, cwd=self.state_dir)
        second = scheduler.submit(argv=SLEEP, cwd=self.state_dir)
        self.assertEqual(first["serial_key"], second["serial_key"])
        self.assertEqual(len(scheduler.tick()), 1)
        wait_for_state(scheduler, first["id"], {"running"})
        plan = scheduler.start_all_plan()
        self.assertEqual(plan["startable"], 0)
        self.assertEqual(plan["blocked_by_serial"], 1)
        self.assertEqual(plan["total"], 1)
        self.assertEqual(scheduler.start_all()["started"], [])
        ignored = scheduler.start_all_plan(ignore_serial=True)
        self.assertEqual(ignored["startable"], 1)
        self.assertTrue(ignored["ignore_serial"])

    def test_an_empty_queue_plans_nothing(self):
        scheduler, _ = make_scheduler(self)
        plan = scheduler.start_all_plan()
        self.assertEqual(plan["total"], 0)
        self.assertEqual(plan["startable"], 0)
        self.assertEqual(plan["memory_mb"], 0)
        self.assertEqual(scheduler.start_all()["started"], [])

    def test_plan_ignores_terminal_and_running_jobs(self):
        scheduler, _ = make_scheduler(self)
        done = scheduler.submit(argv=[PY, "-c", "pass"], cwd=self.state_dir)
        scheduler.tick()
        wait_for_state(scheduler, done["id"], {"done", "failed"})
        plan = scheduler.start_all_plan()
        self.assertEqual(plan["total"], 0)
        self.assertEqual(plan["jobs"], [])


class TestStartAllExecution(helpers.AjqTestCase):
    def test_start_all_ignores_the_pool_caps(self):
        scheduler, _ = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"normal": 1}})
        job_ids = [
            scheduler.submit(argv=SLEEP, cwd=subdir(self.state_dir, name), pool="normal")["id"]
            for name in ("a", "b", "c")
        ]
        running = scheduler.tick()
        self.assertEqual(len(running), 1)
        result = scheduler.start_all()
        self.assertEqual(sorted(result["started"]), sorted(set(job_ids) - set(running)))
        for job_id in job_ids:
            self.assertEqual(scheduler.store.get(job_id)["state"], "running")

    def test_start_all_ignores_the_memory_admission_gate(self):
        scheduler, _ = make_scheduler(self, resources={"memory_headroom": 100000})
        job = scheduler.submit(argv=SLEEP, cwd=subdir(self.state_dir, "a"), pool="heavy")
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.start_all_plan()["startable"], 1)
        self.assertEqual(scheduler.start_all()["started"], [job["id"]])

    def test_a_started_job_is_not_started_twice(self):
        scheduler, _ = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"normal": 1}})
        job = scheduler.submit(argv=SLEEP, cwd=subdir(self.state_dir, "a"), pool="normal")
        scheduler.start_all()
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.start_all()["started"], [])
        self.assertEqual(scheduler.store.get(job["id"])["state"], "running")


class StartCase(helpers.AjqTestCase):
    """Drive the real CLI against a stubbed daemon."""

    def setUp(self):
        super().setUp()
        from ajq import cli

        self.cli = cli
        self.requests = []
        self.plan = dict(PLAN)

        def fake_request(payload, *args, **kwargs):
            self.requests.append(payload)
            plan = dict(self.plan)
            if payload.get("op") == "start_all":
                if payload.get("dry_run"):
                    return {"ok": True, "plan": plan, "started": [], "jobs": []}
                started = [dict(job, state="running") for job in plan["jobs"]]
                return {
                    "ok": True,
                    "plan": plan,
                    "started": [job["id"] for job in started],
                    "jobs": started,
                }
            return {"ok": True}

        self.addCleanup(setattr, self.cli, "_request", self.cli._request)
        self.cli._request = fake_request
        original_require = self.cli._require_daemon
        self.addCleanup(setattr, self.cli, "_require_daemon", original_require)
        self.cli._require_daemon = lambda: True

    def run_cli(self, argv, stdin=""):
        import sys

        buffer = io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buffer):
                code = self.cli.main(argv)
        finally:
            sys.stdin = original_stdin
        return code, buffer.getvalue()


class TestStartParsing(unittest.TestCase):
    def test_start_and_alias_parse_the_same_flags(self):
        from ajq import cli

        for name in ("start", "start-all"):
            args = cli._build_parser().parse_args(
                [name, "--yes", "--dry-run", "--ignore-serial", "--cmd-width", "12"]
            )
            self.assertIs(args.func, cli._cmd_start)
            self.assertTrue(args.yes)
            self.assertTrue(args.dry_run)
            self.assertTrue(args.ignore_serial)
            self.assertEqual(args.cmd_width, 12)

    def test_defaults(self):
        from ajq import cli

        args = cli._build_parser().parse_args(["start"])
        self.assertFalse(args.yes)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.ignore_serial)
        self.assertEqual(args.cmd_width, cli._CMD_WIDTH)


class TestTrim(unittest.TestCase):
    def test_a_short_command_is_untouched(self):
        from ajq.cli import _trim

        self.assertEqual(_trim("npm run build", 48), "npm run build")

    def test_a_long_command_keeps_its_start_and_gains_an_ellipsis(self):
        from ajq.cli import _trim

        trimmed = _trim("pytest tests/api -q --maxfail=1 --timeout=300", 24)
        self.assertEqual(len(trimmed), 24)
        self.assertTrue(trimmed.endswith("..."))
        self.assertTrue(trimmed.startswith("pytest tests/api"))

    def test_width_zero_means_no_cut(self):
        from ajq.cli import _trim

        command = "a" * 200
        self.assertEqual(_trim(command, 0), command)


class TestStartCLI(StartCase):
    def test_prompt_prints_the_cost_and_the_trimmed_commands(self):
        code, out = self.run_cli(["start", "--dry-run", "--cmd-width", "20"])
        self.assertEqual(code, 0)
        self.assertIn("2 of 3 queued job(s) start now", out)
        self.assertIn("1 job(s) stay queued", out)
        self.assertIn("12288MB reserved against 8000MB available", out)
        self.assertIn("400% against 800% (8 cpus)", out)
        self.assertIn("risk", out)
        self.assertIn("pytest tests/api...", out)
        self.assertIn("dry run: nothing started", out)
        self.assertEqual([p.get("op") for p in self.requests], ["start_all"])
        self.assertTrue(self.requests[0]["dry_run"])

    def test_a_declined_prompt_starts_nothing(self):
        code, out = self.run_cli(["start"], stdin="n\n")
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        self.assertEqual([p.get("op") for p in self.requests], ["start_all"])

    def test_an_accepted_prompt_sends_the_real_start(self):
        code, out = self.run_cli(["start"], stdin="y\n")
        self.assertEqual(code, 0)
        self.assertEqual([p.get("op") for p in self.requests], ["start_all", "start_all"])
        self.assertFalse(self.requests[1].get("dry_run"))
        self.assertIn("started 2 job(s)", out)

    def test_yes_skips_the_prompt(self):
        code, out = self.run_cli(["start", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("started 2 job(s)", out)

    def test_json_without_yes_only_reports_the_plan(self):
        code, out = self.run_cli(["start", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([p.get("op") for p in self.requests], ["start_all"])
        self.assertIn('"started": []', out)

    def test_ignore_serial_reaches_the_daemon(self):
        self.run_cli(["start", "--yes", "--ignore-serial"])
        self.assertTrue(all(p.get("ignore_serial") for p in self.requests))

    def test_a_serial_blocked_plan_is_named_and_not_prompted(self):
        self.plan = {
            "total": 2,
            "startable": 0,
            "blocked_by_serial": 2,
            "memory_mb": 0,
            "cpu_percent": 0,
            "available_memory_mb": 8000,
            "total_memory_mb": 32000,
            "cpu_count": 8,
            "max_concurrent": 8,
            "pool_counts": {},
            "pool_caps": {"heavy": 2},
            "jobs": [],
        }
        code, out = self.run_cli(["start", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to start", out)
        self.assertIn("--ignore-serial", out)
        self.assertNotIn("started", out)
        self.assertEqual([p.get("op") for p in self.requests], ["start_all"])

    def test_the_serial_line_names_the_override(self):
        code, out = self.run_cli(["start", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("1 job(s) stay queued", out)
        self.assertIn("--ignore-serial starts them too", out)

    def test_an_empty_queue_prints_one_line(self):
        from ajq import cli

        self.cli._request = lambda payload, *a, **k: {"ok": True, "plan": {"total": 0, "jobs": []}}
        code, out = self.run_cli(["start"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no queued jobs")
        self.assertIsNot(cli, None)


class TestListCommandColumn(unittest.TestCase):
    def test_the_row_shows_the_start_of_the_command(self):
        from ajq.cli import _list_row

        row = _list_row(PLAN["jobs"][0], 20)
        self.assertTrue(row.endswith("pytest tests/api..."))
        self.assertIn("j-1a2b3c4d5e6f", row)
        self.assertIn("queued", row)

    def test_a_missing_command_is_not_an_error(self):
        from ajq.cli import _list_row

        row = _list_row({"id": "j-1", "state": "queued", "argv": []}, 20)
        self.assertTrue(row.endswith("(no command)"))

    def test_the_full_command_shows_with_width_zero(self):
        from ajq.cli import _list_row

        command = " ".join(PLAN["jobs"][0]["argv"])
        self.assertTrue(_list_row(PLAN["jobs"][0], 0).endswith(command))


SHELL_JOB = {
    "id": "j-3a2b3c4d5e6f",
    "state": "queued",
    "pool": "heavy",
    "kind": "test",
    "argv": [
        "/bin/sh",
        "-c",
        "cd /work/CRM/apps/web && bunx vitest run tests/x.test.tsx",
    ],
}


class TestCommandRendering(unittest.TestCase):
    """A row must say what the job runs, not how it was wrapped."""

    def test_a_shell_job_drops_its_cd_prefix(self):
        from ajq.cli import _cmd_text

        self.assertEqual(_cmd_text(SHELL_JOB), "bunx vitest run tests/x.test.tsx")

    def test_a_quoted_directory_is_dropped_too(self):
        from ajq.cli import _cmd_text

        job = dict(SHELL_JOB, argv=["/bin/bash", "-c", "cd '/work/with space/app' && npm run build"])
        self.assertEqual(_cmd_text(job), "npm run build")

    def test_a_chain_of_cd_segments_is_dropped(self):
        from ajq.cli import _cmd_text

        job = dict(SHELL_JOB, argv=["sh", "-c", "cd /a && cd /b && make test"])
        self.assertEqual(_cmd_text(job), "make test")

    def test_everything_after_the_prefix_stays_verbatim(self):
        from ajq.cli import _cmd_text

        job = dict(SHELL_JOB, argv=["sh", "-c", "cd /a && bunx vitest run x > /tmp/x.log 2>&1"])
        self.assertEqual(_cmd_text(job), "bunx vitest run x > /tmp/x.log 2>&1")

    def test_a_shell_job_without_a_prefix_keeps_its_script(self):
        from ajq.cli import _cmd_text

        job = dict(SHELL_JOB, argv=["sh", "-c", "for i in 1 2 3; do bunx vitest run x; done"])
        self.assertEqual(_cmd_text(job), "for i in 1 2 3; do bunx vitest run x; done")

    def test_a_plain_command_is_joined_verbatim(self):
        from ajq.cli import _cmd_text

        job = dict(SHELL_JOB, argv=["pytest", "tests/api", "-k", "leads and sales"])
        self.assertEqual(_cmd_text(job), "pytest tests/api -k 'leads and sales'")

    def test_a_missing_command_is_not_an_error(self):
        from ajq.cli import _cmd_text

        self.assertEqual(_cmd_text({}), "(no command)")

    def test_the_verbose_block_keeps_the_exact_argv(self):
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_job_block(SHELL_JOB)
        out = buffer.getvalue()
        self.assertIn("cmd bunx vitest run tests/x.test.tsx", out)
        self.assertIn(
            "raw /bin/sh -c 'cd /work/CRM/apps/web && bunx vitest run tests/x.test.tsx'", out
        )

    def test_a_plain_command_has_no_raw_line(self):
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_job_block(PLAN["jobs"][0])
        self.assertNotIn("raw ", buffer.getvalue())

    def test_the_start_plan_prints_the_rendered_command(self):
        job = dict(SHELL_JOB, memory_mb=6144, cpu_percent=200)
        plan = dict(PLAN, total=1, startable=1, blocked_by_serial=0, jobs=[job], pool_counts={"heavy": 1})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            from ajq import cli

            cli._print_start_plan(plan, 68)
        out = buffer.getvalue()
        self.assertIn("bunx vitest run tests/x.test.tsx", out)
        self.assertNotIn("/bin/sh", out)


class TestSelectCmd(unittest.TestCase):
    """`cmd` is derived, so `--select cmd` answers the same question as the column."""

    def test_a_list_projection_derives_cmd(self):
        from ajq.cli import _project

        got = _project({"jobs": [SHELL_JOB], "count": 1}, ["cmd"])
        self.assertEqual(got, {"jobs": [{"cmd": "bunx vitest run tests/x.test.tsx"}], "count": 1})

    def test_a_single_job_projection_derives_cmd(self):
        from ajq.cli import _project

        got = _project(SHELL_JOB, ["state", "cmd"])
        self.assertEqual(got, {"state": "queued", "cmd": "bunx vitest run tests/x.test.tsx"})

    def test_the_selected_cmd_is_not_trimmed(self):
        from ajq.cli import _project

        job = dict(SHELL_JOB, argv=["sh", "-c", "cd /a && " + "pytest tests/api -q " * 10])
        got = _project(job, ["cmd"])
        self.assertEqual(got["cmd"], job["argv"][2][len("cd /a && ") :])

    def test_an_unknown_key_is_still_null(self):
        from ajq.cli import _project

        self.assertEqual(_project(SHELL_JOB, ["nope"]), {"nope": None})

    def test_an_explicit_cmd_key_still_wins(self):
        from ajq.cli import _project

        self.assertEqual(_project({"cmd": "mine", "argv": ["pytest"]}, ["cmd"]), {"cmd": "mine"})

    def test_the_plain_key_value_form_derives_cmd_too(self):
        """`ajq list --select cmd` must not print `cmd=-` while --json has a value."""
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_fields({"jobs": [SHELL_JOB]}, ["state", "cmd"])
        self.assertEqual(buffer.getvalue().strip(), "state=queued cmd=bunx vitest run tests/x.test.tsx")

    def test_the_plain_form_still_shows_an_unknown_key_as_a_dash(self):
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_fields(SHELL_JOB, ["nope"])
        self.assertEqual(buffer.getvalue().strip(), "nope=-")

    def test_the_plain_form_keeps_the_requested_key_order(self):
        from ajq import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_fields(SHELL_JOB, ["cmd", "state"])
        self.assertEqual(buffer.getvalue().strip(), "cmd=bunx vitest run tests/x.test.tsx state=queued")


if __name__ == "__main__":
    unittest.main()
