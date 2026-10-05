"""Store + estimate tests: persistence, state machine, Welford cache."""

from __future__ import annotations

import os
import time
import unittest

import helpers

from ajq import estimate as est
from ajq import paths
from ajq.store import JOB_FIELDS, TERMINAL_STATES, Store, new_job_id

store = helpers.store


class TestStore(helpers.AjqTestCase):
    def setUp(self):
        super().setUp()
        self.store = store()
        self.addCleanup(self.store.close)

    def test_job_roundtrip_has_every_field(self):
        job = self.store.add_job(argv=["echo", "hi"], cwd=self.state_dir, label="x")
        self.assertTrue(job["id"].startswith("j-"))
        self.assertEqual(job["state"], "queued")
        for field in JOB_FIELDS:
            self.assertIn(field, job, field)
        fetched = self.store.get(job["id"])
        self.assertEqual(fetched["id"], job["id"])
        self.assertEqual(fetched["out_path"], paths.job_out_path(job["id"]))
        self.assertIsNone(self.store.get("j-does-not-exist"))

    def test_ids_are_unique(self):
        ids = {new_job_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_state_transitions(self):
        job = self.store.add_job(argv=["true"], cwd=self.state_dir)
        running = self.store.start(job["id"], pid=4242, backend="test-backend")
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["pid"], 4242)
        self.assertIsNotNone(running["started_at"])
        done = self.store.finish(job["id"], "done", exit_code=0, out_bytes=12)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["out_bytes"], 12)
        self.assertIn("done", TERMINAL_STATES)
        self.assertNotIn("running", TERMINAL_STATES)

    def test_finish_records_kill_details(self):
        job = self.store.add_job(argv=["sleep", "9"], cwd=self.state_dir)
        self.store.start(job["id"], pid=1, backend="b")
        killed = self.store.finish(
            job["id"], "timeout", signal=15, kill_reason="timeout", truncated=True
        )
        self.assertEqual(killed["state"], "timeout")
        self.assertEqual(killed["signal"], 15)
        self.assertEqual(killed["kill_reason"], "timeout")
        self.assertTrue(killed["truncated"])

    def test_queue_ordering_is_priority_then_age(self):
        first = self.store.add_job(argv=["a"], cwd=self.state_dir, priority=0)
        time.sleep(0.01)
        second = self.store.add_job(argv=["b"], cwd=self.state_dir, priority=0)
        urgent = self.store.add_job(argv=["c"], cwd=self.state_dir, priority=10)
        order = [job["id"] for job in self.store.queued_jobs()]
        self.assertEqual(order, [urgent["id"], first["id"], second["id"]])

    def test_running_and_counts(self):
        a = self.store.add_job(argv=["a"], cwd=self.state_dir)
        b = self.store.add_job(argv=["b"], cwd=self.state_dir)
        self.store.start(a["id"], pid=1, backend="b")
        self.assertEqual([j["id"] for j in self.store.running_jobs()], [a["id"]])
        self.store.finish(b["id"], "canceled", kill_reason="canceled")
        counts = self.store.count_by_state()
        self.assertEqual(counts.get("running"), 1)
        self.assertEqual(counts.get("canceled"), 1)
        self.assertEqual(counts.get("queued"), 0)

    def test_list_jobs_filters_by_state(self):
        a = self.store.add_job(argv=["a"], cwd=self.state_dir)
        b = self.store.add_job(argv=["b"], cwd=self.state_dir)
        self.store.finish(b["id"], "done", exit_code=0)
        ids = [j["id"] for j in self.store.list_jobs(states=["queued"])]
        self.assertEqual(ids, [a["id"]])

    def test_add_out_bytes_accumulates(self):
        job = self.store.add_job(argv=["a"], cwd=self.state_dir)
        self.store.add_out_bytes(job["id"], 100)
        self.store.add_out_bytes(job["id"], 23)
        self.assertEqual(self.store.get(job["id"])["out_bytes"], 123)

    def test_recover_orphans_marks_running_lost(self):
        a = self.store.add_job(argv=["a"], cwd=self.state_dir)
        b = self.store.add_job(argv=["b"], cwd=self.state_dir)
        self.store.start(a["id"], pid=1, backend="b")
        self.store.start(b["id"], pid=2, backend="b")
        self.assertEqual(self.store.recover_orphans(), 2)
        self.assertEqual(self.store.get(a["id"])["state"], "lost")
        self.assertEqual(self.store.get(a["id"])["kill_reason"], "daemon_restart")
        self.assertEqual(self.store.recover_orphans(), 0)

    def test_prune_keeps_recent_and_drops_old(self):
        old = self.store.add_job(argv=["a"], cwd=self.state_dir)
        self.store.finish(old["id"], "done", exit_code=0)
        recent = self.store.add_job(argv=["b"], cwd=self.state_dir)
        self.store.finish(recent["id"], "done", exit_code=0)
        self.store.update(old["id"], ended_at=time.time() - 60 * 60 * 24 * 30)
        self.assertEqual(self.store.prune(keep_days=14), 1)
        self.assertIsNone(self.store.get(old["id"]))
        self.assertIsNotNone(self.store.get(recent["id"]))

    def test_prune_removes_output_files_too(self):
        from ajq import paths

        old = self.store.add_job(argv=["a"], cwd=self.state_dir)
        paths.ensure_dir(paths.job_dir(old["id"]))
        with open(paths.job_out_path(old["id"]), "w", encoding="utf-8") as handle:
            handle.write("log output")
        self.store.finish(old["id"], "done", exit_code=0)
        self.store.update(old["id"], ended_at=time.time() - 60 * 60 * 24 * 30)
        self.assertEqual(self.store.prune(keep_days=1), 1)
        self.assertFalse(os.path.exists(paths.job_out_path(old["id"])))
        self.assertFalse(os.path.exists(paths.job_dir(old["id"])))

    def test_prune_can_keep_files(self):
        from ajq import paths

        old = self.store.add_job(argv=["a"], cwd=self.state_dir)
        paths.ensure_dir(paths.job_dir(old["id"]))
        with open(paths.job_out_path(old["id"]), "w", encoding="utf-8") as handle:
            handle.write("keep me")
        self.store.finish(old["id"], "done", exit_code=0)
        self.store.update(old["id"], ended_at=time.time() - 60 * 60 * 24 * 30)
        self.assertEqual(self.store.prune(keep_days=1, remove_files=False), 1)
        self.assertTrue(os.path.exists(paths.job_out_path(old["id"])))

    def test_prune_never_touches_running_or_queued_jobs(self):
        queued = self.store.add_job(argv=["a"], cwd=self.state_dir)
        running = self.store.add_job(argv=["b"], cwd=self.state_dir)
        self.store.start(running["id"], pid=3, backend="b")
        self.store.update(queued["id"], enqueued_at=0.0)
        self.store.update(running["id"], started_at=1.0)
        self.assertEqual(self.store.prune(keep_days=0, remove_files=False), 0)
        self.assertIsNotNone(self.store.get(queued["id"]))
        self.assertIsNotNone(self.store.get(running["id"]))

    def test_exit_code_is_null_until_the_job_ends(self):
        """A queued job reporting exit_code 0 reads as "it passed"."""
        job = self.store.add_job(argv=["a"], cwd=self.state_dir)
        self.assertIsNone(job["exit_code"])
        self.assertIsNone(job["signal"])
        running = self.store.start(job["id"], pid=11, backend="b")
        self.assertIsNone(running["exit_code"])
        self.assertIsNone(running["signal"])
        done = self.store.finish(job["id"], "failed", exit_code=2)
        self.assertEqual(done["exit_code"], 2)
        self.assertIsNone(done["signal"])

    def test_per_job_resource_overrides_persist(self):
        job = self.store.add_job(
            argv=["a"], cwd=self.state_dir, tool="pytest", memory_mb=512,
            cpu_percent=150, shell=True, kill_grace_s=7.0,
        )
        again = self.store.get(job["id"])
        self.assertEqual(again["tool"], "pytest")
        self.assertEqual(again["memory_mb"], 512)
        self.assertEqual(again["cpu_percent"], 150)
        self.assertTrue(again["shell"])
        self.assertEqual(again["kill_grace_s"], 7.0)

    def test_schema_migration_adds_new_columns(self):
        """An older DB must gain new fields instead of failing every INSERT."""
        import sqlite3

        from ajq.store import Store

        path = os.path.join(self.state_dir, "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, enqueued_at REAL);"
            "CREATE TABLE estimates (signature TEXT PRIMARY KEY, n INTEGER,"
            " mean REAL, m2 REAL);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        conn.execute("INSERT INTO jobs (id, state, enqueued_at) VALUES ('j-old','queued',1.0)")
        conn.commit()
        conn.close()
        legacy = Store(path)
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.get("j-old")["state"], "queued")
        fresh = legacy.add_job(argv=["x"], cwd=self.state_dir, tool="cargo")
        self.assertEqual(legacy.get(fresh["id"])["tool"], "cargo")


class TestEstimates(helpers.AjqTestCase):
    def setUp(self):
        super().setUp()
        self.store = store()
        self.addCleanup(self.store.close)

    def test_welford_update_matches_arithmetic_mean(self):
        for value in (10.0, 20.0, 30.0, 40.0):
            self.store.record_duration("test|pytest|src/21-100", "test", "pytest", value)
        row = self.store.get_estimate("test|pytest|src/21-100")
        self.assertEqual(row["n"], 4)
        self.assertAlmostEqual(row["mean"], 25.0, places=6)
        expected_var = sum((v - 25.0) ** 2 for v in (10.0, 20.0, 30.0, 40.0)) / 3
        self.assertAlmostEqual(row["m2"] / 3, expected_var, places=6)

    def test_cold_estimate_uses_kind_default(self):
        seconds, source = est.estimate_seconds(self.store, "build", "gradle", self.state_dir)
        self.assertGreater(seconds, 0)
        self.assertTrue(source.startswith("cold:"))
        self.assertIn("build", source)

    def test_warm_estimate_uses_cache_and_reports_source(self):
        files = [f"src/f{i}.py" for i in range(30)]
        for value in (100.0, 100.0, 100.0):
            est.record(self.store, "build", "gradle", self.state_dir, value, files)
        # same file set on both sides: a warm hit requires an identical signature
        seconds, source = est.estimate_seconds(self.store, "build", "gradle", self.state_dir, files)
        self.assertAlmostEqual(seconds, 100.0, places=6)
        self.assertTrue(source.startswith("cache:"), source)

    def test_estimate_is_clamped(self):
        files = ["src/a.py"]
        for value in (100.0, 100.0, 1000.0):  # one huge outlier
            est.record(self.store, "build", "gradle", self.state_dir, value, files)
        seconds, _ = est.estimate_seconds(self.store, "build", "gradle", self.state_dir, files)
        self.assertLessEqual(seconds, 400.0 * 3)

    def test_signature_shape_and_parse(self):
        signature = est.signature_for("test", "pytest", self.state_dir, ["src/a.py", "tests/b.py"])
        self.assertEqual(signature.count("|"), 3)
        kind, tool, dirs, bucket = est.parse_signature(signature)
        self.assertEqual((kind, tool), ("test", "pytest"))
        self.assertIn("2-5", bucket)
        self.assertTrue(dirs)

    def test_file_signature_buckets(self):
        self.assertTrue(est.file_signature(["a.py"]).endswith("/1"))
        self.assertTrue(est.file_signature([f"f{i}.py" for i in range(50)]).endswith("/21-100"))

    def test_changed_files_in_non_git_dir_is_empty(self):
        empty_dir = os.path.join(self.state_dir, "not-a-repo")
        os.makedirs(empty_dir, exist_ok=True)
        self.assertEqual(est.changed_files(empty_dir), [])

    def test_kind_and_tool_classification(self):
        cases = {
            ("npm", "run", "build"): ("build", "npm:build"),
            ("pytest", "-q"): ("test", "pytest"),
            ("cargo", "clippy"): ("typecheck", "cargo"),
            ("prettier", "--check", "."): ("format", "prettier"),
            ("git", "status"): ("check", "git"),
        }
        for argv, (kind, tool) in cases.items():
            self.assertEqual(est.kind_from_command(list(argv)), kind, argv)
            self.assertEqual(est.tool_from_command(list(argv)), tool, argv)

    def test_record_returns_signature_and_populates_cache(self):
        signature = est.record(self.store, "test", "pytest", self.state_dir, 12.0, ["src/a.py"])
        self.assertIsNotNone(self.store.get_estimate(signature))

    def test_accuracy_reports_mape(self):
        signature = est.signature_for("test", "pytest", self.state_dir, ["src/a.py"])
        for value in (10.0, 12.0):
            est.record(self.store, "test", "pytest", self.state_dir, value, ["src/a.py"])
        job = self.store.add_job(
            argv=["pytest"], cwd=self.state_dir, signature=signature, est_seconds=20.0
        )
        self.store.start(job["id"], pid=1, backend="b")
        self.store.update(job["id"], started_at=time.time() - 10.0)
        self.store.finish(job["id"], "done", exit_code=0)
        rows = self.store.estimate_rows()
        self.assertEqual(len(rows), 1)
        report = est.accuracy(self.store)
        self.assertEqual(len(report), 1)
        self.assertAlmostEqual(report[0]["mape_pct"], 100.0, places=0)  # est 20 vs actual ~10

    def test_clear_estimates(self):
        self.store.record_duration("test|pytest|a/1", "test", "pytest", 5.0)
        self.assertEqual(self.store.clear_estimates(), 1)
        self.assertEqual(self.store.list_estimates(), [])


if __name__ == "__main__":
    unittest.main()