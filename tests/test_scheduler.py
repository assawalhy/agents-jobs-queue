"""Scheduler tests: pool caps, worktree serialization, RAM admission, metadata."""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import helpers

from ajq import paths, platform
from ajq.backends.macos import MacosBackend
from ajq.exec import JobRunner
from ajq.scheduler import Scheduler

PY = "python3"
SHORT = [PY, "-c", "import time; time.sleep(3)"]


_CALM = {
    "swap_credit": 0,
    "admission": {"psi_memory_max": 0, "psi_cpu_max": 0, "temp_max_c": 0},
}


def make_scheduler(self, **overrides):
    store = helpers.store()
    self.addCleanup(store.close)
    # Pressure gates and zram credit read the host; tests opt in to them explicitly.
    overrides["resources"] = {**_CALM, **overrides.get("resources", {})}
    config = helpers.load_config(**overrides)
    backend = MacosBackend()
    runner = JobRunner(backend, poll_interval=0.05)
    return Scheduler(store, config, runner, backend), store


def wait_for_state(scheduler, job_id, states, timeout=15.0):
    return helpers.wait_until(
        lambda: (scheduler.store.get(job_id) or {}).get("state") in states, timeout=timeout
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="w0", memory_mb=1)
        self.assertEqual(len(scheduler.tick()), 1)
        big = scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key="w1")
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(store.get(big["id"])["state"], "queued")
        self.assertTrue(scheduler.enrich(store.get(big["id"]))["blocked"].startswith("memory "))

    def test_pool_memory_is_resolved_on_submit(self):
        scheduler, _ = make_scheduler(
            self,
            resources={"memory_mb": 2048, "pool_memory_mb": {"heavy": 6144, "light": 1024}},
        )
        heavy = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy")
        light = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="lint", pool="light")
        other = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="service")
        explicit = scheduler.submit(
            argv=SHORT, cwd=self.state_dir, kind="lint", pool="light", memory_mb=4096
        )
        self.assertEqual(heavy["memory_mb"], 6144)
        self.assertEqual(light["memory_mb"], 1024)
        self.assertEqual(other["memory_mb"], 2048)
        self.assertEqual(explicit["memory_mb"], 4096)

    def test_pool_memory_blocks_heavy_but_admits_light(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"heavy": 8, "light": 8}},
            resources={
                "memory_mb": 2048,
                "memory_headroom": 1.2,
                "pool_memory_mb": {"heavy": 8 * 1024 * 1024, "light": 1},
            },
        )
        light = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="lint", pool="light", serial_key="w1")
        heavy = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="w0")
        started = scheduler.tick()
        self.assertEqual(started, [light["id"]])
        self.assertEqual(store.get(heavy["id"])["state"], "queued")
        self.assertEqual(store.get(light["id"])["state"], "running")

    def test_progress_rule_starts_head_job_when_reservation_exceeds_budget(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"heavy": 2}},
            resources={"pool_memory_mb": {"heavy": 8 * 1024 * 1024}},
        )
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="w0")
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, kind="build", pool="heavy", serial_key="w1")
        self.assertEqual(scheduler.tick(), [first["id"]])
        self.assertEqual(store.get(second["id"])["state"], "queued")

    def test_pressure_never_holds_the_head_job_when_nothing_runs(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"normal": 8}},
            resources={"admission": {"psi_memory_max": 5, "psi_cpu_max": 0, "temp_max_c": 0}},
        )
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w0")
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w1")
        with mock.patch.object(platform, "psi_avg10", return_value=25.0):
            self.assertEqual(scheduler.tick(), [first["id"]])
            self.assertEqual(scheduler.enrich(store.get(second["id"]))["blocked"], "psi-mem 25%>5%")
        self.assertEqual(scheduler.tick(), [second["id"]])

    def test_cpu_pressure_and_temperature_hold_only_a_second_job(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"normal": 8}},
            resources={"admission": {"psi_memory_max": 0, "psi_cpu_max": 70, "temp_max_c": 90}},
        )
        first = scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w0")
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w1")
        with mock.patch.object(platform, "psi_avg10", return_value=0.0), \
                mock.patch.object(platform, "cpu_temp_c", return_value=97.0):
            scheduler._temp_ema = None
            self.assertEqual(scheduler.tick(), [first["id"]])
            self.assertEqual(scheduler.enrich(store.get(second["id"]))["blocked"], "temp 97C>90C")
        with mock.patch.object(platform, "psi_avg10", return_value=85.0), \
                mock.patch.object(platform, "cpu_temp_c", return_value=None):
            self.assertEqual(scheduler.tick(), [])
            self.assertEqual(scheduler.enrich(store.get(second["id"]))["blocked"], "psi-cpu 85%>70%")
            self.assertIsNone(scheduler._temp_ema)

    def test_enrich_many_probes_the_host_once(self):
        scheduler, store = make_scheduler(self)
        for index in range(5):
            scheduler.submit(argv=SHORT, cwd=self.state_dir, serial_key=f"w{index}")
        with mock.patch.object(scheduler, "admission_state", wraps=scheduler.admission_state) as probe:
            jobs = scheduler.enrich_many(store.queued_jobs())
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(jobs), 5)

    def test_zram_credit_extends_the_memory_budget(self):
        scheduler, store = make_scheduler(
            self,
            limits={"max_concurrent": 8, "pools": {"normal": 8}},
            resources={"swap_credit": 1.0, "memory_headroom": 1.0},
        )
        scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w0", memory_mb=1)
        second = scheduler.submit(argv=SHORT, cwd=self.state_dir, pool="normal", serial_key="w1", memory_mb=5000)
        with mock.patch.object(platform, "available_memory_mb", return_value=3000), \
                mock.patch.object(platform, "zram_credit_mb", return_value=0):
            self.assertEqual(len(scheduler.tick()), 1)
        self.assertEqual(store.get(second["id"])["state"], "queued")
        with mock.patch.object(platform, "available_memory_mb", return_value=3000), \
                mock.patch.object(platform, "zram_credit_mb", return_value=2500):
            self.assertEqual(scheduler.tick(), [second["id"]])

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

    def test_cancel_without_a_local_runner_kills_the_recorded_pid(self):
        """A job started by another daemon (no local cancel event) still dies."""
        scheduler, store = make_scheduler(self)
        job = scheduler.submit(
            argv=[PY, "-c", "import time; time.sleep(30)"], cwd=self.state_dir
        )
        scheduler.tick()
        pid = store.get(job["id"])["pid"]
        self.assertGreater(pid, 0)
        # simulate the other-daemon case: this scheduler has no event for it
        with scheduler._lock:
            scheduler._running.pop(job["id"], None)
        scheduler.cancel(job["id"])
        self.assertTrue(wait_for_state(scheduler, job["id"], {"canceled", "failed", "timeout"}))
        self.assertTrue(helpers.wait_until(lambda: not _pid_alive(pid), timeout=5.0))

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