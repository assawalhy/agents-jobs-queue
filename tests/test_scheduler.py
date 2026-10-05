"""Scheduler tests: pool caps, worktree serialization, RAM admission, metadata."""

from __future__ import annotations

import os
import time
import unittest

import helpers

from ajq import paths
from ajq.backends.macos import MacosBackend
from ajq.exec import JobRunner
from ajq.scheduler import Scheduler

PY = "python3"
SHORT = [PY, "-c", "import time; time.sleep(3)"]


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


class TestSubmit(helpers.AjqTestCase):
    def test_submit_fills_metadata_and_defaults(self):
        scheduler, _ = make_scheduler(self)
        job = scheduler.submit(argv=SHORT, cwd=self.state_dir, label="sleep")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["label"], "sleep")
        self.assertEqual(job["timeout_s"], 1800)
        self.assertEqual(job["max_output_bytes"], 8 * 1024 * 1024)
        self.assertGreater(job["est_seconds"], 0)
        self.assertTrue(job["est_source"])
        self.assertTrue(job["out_path"].endswith("out.log"))

    def test_submit_classifies_kind_and_pool(self):
        scheduler, _ = make_scheduler(self)
        job = scheduler.submit(argv=["npm", "run", "build"], cwd=self.state_dir)
        self.assertEqual(job["kind"], "build")
        self.assertEqual(job["pool"], "heavy")

    def test_submit_honours_explicit_overrides(self):
        scheduler, _ = make_scheduler(self)
        job = scheduler.submit(
            argv=SHORT, cwd=self.state_dir, timeout_s=5, priority=7, pool="service", kind="test"
        )
        self.assertEqual(job["timeout_s"], 5)
        self.assertEqual(job["priority"], 7)
        self.assertEqual(job["pool"], "service")
        self.assertEqual(job["kind"], "test")

    def test_config_defaults_apply(self):
        scheduler, _ = make_scheduler(self, defaults={"timeout_s": 42})
        job = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        self.assertEqual(job["timeout_s"], 42)

    def test_serial_key_auto_uses_worktree(self):
        scheduler, _ = make_scheduler(self)
        default = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        self.assertTrue(default["serial_key"])
        self.assertTrue(os.path.isabs(default["serial_key"]))
        disabled = scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="none")
        self.assertEqual(disabled["serial_key"], "")

    def test_enqueue_metadata_is_complete(self):
        scheduler, _ = make_scheduler(self)
        job = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        for key in (
            "queue_position",
            "elapsed_s",
            "eta_start_s",
            "eta_run_s",
            "eta_total_s",
            "est_source",
        ):
            self.assertIn(key, job, key)
        self.assertEqual(job["queue_position"], 1)
        self.assertGreaterEqual(job["eta_run_s"], 0)
        self.assertGreaterEqual(job["eta_total_s"], job["eta_start_s"])


class TestScheduling(helpers.AjqTestCase):
    def test_pool_cap_is_enforced(self):
        scheduler, store = make_scheduler(self, limits={"max_concurrent": 8, "pools": {"heavy": 2}})
        jobs = [
            scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key=f"w{i}")
            for i in range(4)
        ]
        started = scheduler.tick()
        self.assertEqual(len(started), 2)
        states = [store.get(job["id"])["state"] for job in jobs]
        self.assertEqual(states.count("running"), 2)
        self.assertEqual(states.count("queued"), 2)

    def test_global_cap_is_enforced(self):
        scheduler, store = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"heavy": 9}})
        for i in range(3):
            scheduler.submit(
                argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key=f"w{i}"
            )
        self.assertEqual(len(scheduler.tick()), 1)
        self.assertEqual(store.count_by_state().get("running"), 1)

    def test_same_worktree_never_runs_concurrently(self):
        scheduler, store = make_scheduler(self, limits={"max_concurrent": 8, "pools": {"heavy": 8}})
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="wt-a")
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="wt-a")
        self.assertEqual(len(scheduler.tick()), 1)
        states = {first["id"]: store.get(first["id"])["state"],
                  second["id"]: store.get(second["id"])["state"]}
        self.assertEqual(sorted(states.values()), ["queued", "running"])

    def test_memory_admission_blocks_when_box_is_full(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"heavy": 8}},
            resources={"memory_mb": 8 * 1024 * 1024, "memory_headroom": 1.2},
        )
        scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="w0")
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(store.count_by_state().get("queued"), 1)

    def test_queue_position_counts_ahead(self):
        scheduler, _ = make_scheduler(self)
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        time.sleep(0.01)
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        self.assertEqual(scheduler.compute_position(first), 1)
        self.assertEqual(scheduler.compute_position(second), 2)

    def test_eta_start_grows_with_queue(self):
        scheduler, _ = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"heavy": 9}})
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="a")
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="b")
        # a queued job ahead of you delays you: that is the contract definition
        self.assertGreater(second["eta_start_s"], 0)
        self.assertGreater(first["est_seconds"], 0)
        scheduler.tick()
        refreshed = scheduler.enrich(scheduler.store.get(second["id"]))
        self.assertGreater(refreshed["eta_start_s"], 0)

    def test_priority_runs_first(self):
        scheduler, store = make_scheduler(self, limits={"max_concurrent": 1, "pools": {"heavy": 9}})
        normal = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="a")
        time.sleep(0.01)
        urgent = scheduler.submit(
            argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="b", priority=5
        )
        scheduler.tick()
        self.assertEqual(store.get(urgent["id"])["state"], "running")
        self.assertEqual(store.get(normal["id"])["state"], "queued")


class TestLifecycle(helpers.AjqTestCase):
    def test_job_runs_to_completion(self):
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(argv=[PY, "-c", "print('done')"], cwd=self.state_dir)
        scheduler.tick()
        self.assertTrue(wait_for_state(scheduler, job["id"], {"done", "failed"}))
        final = store.get(job["id"])
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["exit_code"], 0)
        self.assertTrue(final["backend"].startswith("macos"), final["backend"])
        self.assertGreater(final["out_bytes"], 0)
        with open(paths.job_out_path(job["id"]), encoding="utf-8") as handle:
            self.assertIn("done", handle.read())

    def test_timeout_job_records_kill_reason(self):
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(
            argv=[PY, "-c", "import time; time.sleep(30)"], cwd=self.state_dir, timeout_s=1
        )
        scheduler.tick()
        self.assertTrue(wait_for_state(scheduler, job["id"], {"timeout", "failed"}))
        final = store.get(job["id"])
        self.assertEqual(final["state"], "timeout")
        self.assertEqual(final["kill_reason"], "timeout")

    def test_cancel_queued_job(self):
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(argv=SHORT, cwd=self.state_dir)
        scheduler.cancel(job["id"])
        self.assertEqual(store.get(job["id"])["state"], "canceled")

    def test_cancel_running_job(self):
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(
            argv=[PY, "-c", "import time; time.sleep(30)"], cwd=self.state_dir
        )
        scheduler.tick()
        self.assertEqual(store.get(job["id"])["state"], "running")
        scheduler.cancel(job["id"])
        self.assertTrue(wait_for_state(scheduler, job["id"], {"canceled", "timeout", "failed"}))
        self.assertEqual(store.get(job["id"])["kill_reason"], "canceled")

    def test_finish_populates_estimate_cache(self):
        from ajq import estimate as est

        scheduler, store = make_scheduler(self)
        job = scheduler.submit(argv=[PY, "-c", "print('x')"], cwd=self.state_dir, kind="test")
        scheduler.tick()
        self.assertTrue(wait_for_state(scheduler, job["id"], {"done"}))
        rows = store.estimate_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["signature"])

    def test_elapsed_freezes_when_the_job_ends(self):
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(argv=[PY, "-c", "print('x')"], cwd=self.state_dir)
        scheduler.tick()
        self.assertTrue(wait_for_state(scheduler, job["id"], {"done"}))
        first = scheduler.enrich(store.get(job["id"]))["elapsed_s"]
        time.sleep(0.4)
        later = scheduler.enrich(store.get(job["id"]))["elapsed_s"]
        self.assertEqual(first, later, "a finished job's elapsed must be its runtime")

    def test_meta_sidecar_is_written(self):
        scheduler, _ = make_scheduler(self)
        job = scheduler.submit(argv=[PY, "-c", "print('x')"], cwd=self.state_dir)
        scheduler.tick()
        self.assertTrue(wait_for_state(scheduler, job["id"], {"done"}))
        import json

        meta_path = paths.job_meta_path(job["id"])
        self.assertTrue(os.path.exists(meta_path), meta_path)
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["id"], job["id"])
        self.assertEqual(meta["state"], "done")
        self.assertEqual(os.stat(meta_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()