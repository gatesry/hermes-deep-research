import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from deep_research.contract import ContractError, normalize_request
from deep_research.jobs import JobManager
from deep_research.store import Store
from deep_research.tool import handle
from tests.fakes import FakeIO


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class JobTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dr-test-")
        self.path = os.path.join(self.dir, "jobs.db")
        self.io = FakeIO()
        self.managers = []
        self.manager = self.make_manager()

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown(10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_manager(self, io=None, clock=time.time, **kw):
        store = Store(lambda: sqlite3.connect(self.path, check_same_thread=False), clock=clock)
        manager = JobManager(store, io_factory=lambda: io or self.io, clock=clock, **kw)
        self.managers.append(manager)
        return manager

    def start(self, key="k1", question="How many widgets?", manager=None, **request):
        manager = manager or self.manager
        return manager.start("conv-1", key, normalize_request({"question": question, **request}))


class IdempotencyTests(JobTestBase):
    def test_same_key_same_request_returns_same_job_without_new_work(self):
        first = self.start()
        self.assertEqual(first["state"], "partial")
        calls = dict(self.io.calls)
        again = self.start()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.io.calls, calls)

    def test_same_key_different_request_rejected(self):
        self.start()
        with self.assertRaises(ContractError):
            self.start(question="Something else")

    def test_job_ids_are_scoped_to_conversation(self):
        job = self.start()
        with self.assertRaises(ContractError):
            self.manager.get("conv-2", job["id"])
        other = self.manager.start("conv-2", "k1", normalize_request({"question": "How many widgets?"}))
        self.assertNotEqual(other["id"], job["id"])


class ResultStabilityTests(JobTestBase):
    def test_repeated_result_reads_are_identical(self):
        job = self.start()
        args = {"action": "result", "jobId": job["id"]}
        first = handle(self.manager, args, conversation_id="conv-1")
        second = handle(self.manager, args, conversation_id="conv-1")
        self.assertEqual(first, second)
        body = json.loads(first)
        self.assertIn("## Unresolved", body["result"]["report"])
        self.assertIn("text", body["result"]["sources"][0])

    def test_start_returns_compact_result(self):
        body = json.loads(handle(self.manager, {"action": "start", "idempotencyKey": "x",
                                                "request": {"question": "How many widgets?"}},
                                 conversation_id="conv-1"))
        self.assertNotIn("text", body["result"]["sources"][0])
        self.assertIn("fullResult", body)

    def test_capabilities_and_errors(self):
        caps = json.loads(handle(self.manager, {"action": "capabilities"}))
        self.assertEqual(caps["portable"]["budget"]["maximum"]["modelCalls"], 30)
        self.assertFalse(caps["managed"]["available"])
        err = json.loads(handle(self.manager, {"action": "status", "jobId": "nope"}, conversation_id="conv-1"))
        self.assertIn("error", err)


class CapacityAndRetentionTests(JobTestBase):
    def test_32_jobs_per_conversation(self):
        clock = Clock()
        manager = self.make_manager(clock=clock, foreground_wait=0)
        store = manager.store
        for i in range(32):
            store.insert_job(f"id{i}", "conv-1", f"k{i}", "{}", clock.now)
        with self.assertRaises(ContractError) as err:
            self.start(key="k-new", manager=manager)
        self.assertIn("32", str(err.exception))
        # Other conversations are unaffected.
        manager.start("conv-2", "k", normalize_request({"question": "q"}))

    def test_seven_day_retention(self):
        clock = Clock()
        manager = self.make_manager(clock=clock)
        job = self.start(manager=manager)
        clock.now += 7 * 86400 - 60
        self.assertEqual(manager.get("conv-1", job["id"])["id"], job["id"])
        clock.now += 120
        with self.assertRaises(ContractError):
            manager.get("conv-1", job["id"])
        self.assertEqual(manager.store.count_steps(job["id"]), 0)


class RecoveryTests(JobTestBase):
    def test_restart_marks_stale_job_paused_and_resume_does_not_repeat_unknown_work(self):
        clock = Clock()
        manager = self.make_manager(clock=clock, foreground_wait=0)
        req = normalize_request({"question": "How many widgets?"})
        # Simulate a process that claimed the job, recorded receipts, then died mid-fetch.
        job_id = manager.job_id("conv-1", "k1")
        manager.store.insert_job(job_id, "conv-1", "k1", json.dumps(req, sort_keys=True), clock.now)
        self.assertTrue(manager.store.claim(job_id, clock.now, clock.now + 180))
        from deep_research.engine import Engine  # noqa: F401  (documents the step key scheme)
        import hashlib
        fetch_step = hashlib.sha256("fetches\0fetch:https://a.example/report".encode()).hexdigest()
        manager.store.put_step(job_id, fetch_step, "started", None)
        # "Restart": a fresh store/manager on the same database, 2 minutes later.
        clock.now += 120
        io = FakeIO()
        restarted = self.make_manager(io=io, clock=clock, foreground_wait=5)
        status = restarted.get("conv-1", job_id)
        self.assertEqual(status["state"], "paused")
        clock_runner = time.time  # the runner thread needs real time for its deadline
        restarted.clock = clock_runner
        restarted.store.clock = clock_runner
        restarted.store.set_deadline(job_id, time.time() + 60)
        result = restarted.resume("conv-1", job_id)
        self.assertEqual(result["state"], "indeterminate")
        self.assertEqual(io.calls["fetch"], 1)  # only the never-attempted URL
        self.assertTrue(any("not repeated" in g for g in result["result"]["gaps"]))

    def test_live_heartbeat_is_not_stale(self):
        clock = Clock()
        manager = self.make_manager(clock=clock, foreground_wait=0)
        job_id = manager.job_id("conv-1", "k1")
        manager.store.insert_job(job_id, "conv-1", "k1", json.dumps(normalize_request({"question": "q"})), clock.now)
        manager.store.claim(job_id, clock.now, clock.now + 180)
        clock.now += 30
        self.assertEqual(manager.get("conv-1", job_id)["state"], "running")

    def test_terminal_job_is_never_rerun(self):
        job = self.start()
        calls = dict(self.io.calls)
        again = self.manager.resume("conv-1", job["id"])
        self.assertEqual(again["state"], job["state"])
        self.assertEqual(self.io.calls, calls)


class BackgroundAndCancelTests(JobTestBase):
    def test_foreground_wait_returns_running_then_completes_in_background(self):
        gate = threading.Event()
        io = FakeIO(fetch_gate=gate)
        manager = self.make_manager(io=io, foreground_wait=0.2)
        job = self.start(manager=manager)
        self.assertEqual(job["state"], "running")
        gate.set()
        final = manager.wait(job["id"], 10)
        self.assertIn(final["state"], ("partial", "completed"))

    def test_cancel_running_job(self):
        gate = threading.Event()
        io = FakeIO(fetch_gate=gate)
        manager = self.make_manager(io=io, foreground_wait=0.2)
        job = self.start(manager=manager)
        manager.cancel("conv-1", job["id"], wait=0)
        gate.set()
        final = manager.wait(job["id"], 10)
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["result"]["status"], "cancelled")
        self.assertIn("Research cancelled.", final["result"]["gaps"])

    def test_cancel_paused_job_replays_without_effects(self):
        clock = Clock()
        manager = self.make_manager(clock=clock, foreground_wait=0)
        job_id = manager.job_id("conv-1", "k1")
        manager.store.insert_job(job_id, "conv-1", "k1", json.dumps(normalize_request({"question": "q"})), clock.now)
        clock.now += 120
        io = FakeIO()
        restarted = self.make_manager(io=io, clock=clock, foreground_wait=5)
        final = restarted.cancel("conv-1", job_id)
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(sum(io.calls.values()), 0)

    def test_active_job_limit(self):
        gate = threading.Event()
        io = FakeIO(fetch_gate=gate)
        manager = self.make_manager(io=io, foreground_wait=0.1, max_active=1)
        self.start(key="a", manager=manager)
        with self.assertRaises(ContractError):
            self.start(key="b", question="other", manager=manager)
        gate.set()


if __name__ == "__main__":
    unittest.main()
