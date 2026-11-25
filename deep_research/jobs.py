"""Background research jobs: idempotent start, polling, cancel, and receipt-based recovery."""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .contract import Budget, ContractError, canonical_request
from .engine import Engine
from .store import TERMINAL_STATES, JobSteps, Store

MAX_JOBS_PER_CONVERSATION = 32
RETENTION_SECONDS = 7 * 86400
HEARTBEAT_SECONDS = 10.0
STALE_SECONDS = 60.0
PRUNE_EVERY_SECONDS = 60.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobManager:
    def __init__(self, store: Store, io_factory: Callable[[], Any], *, clock: Callable[[], float] = time.time,
                 foreground_wait: float = 300.0, max_active: int = 4):
        self.store = store
        self.io_factory = io_factory
        self.clock = clock
        self.foreground_wait = foreground_wait
        self.max_active = max_active
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._admit = threading.Lock()
        self._stopping = threading.Event()  # unload/reload: pause runners, don't cancel them
        self.cancel_wait = 5.0  # cancel answers quickly; the job settles in the background  # serialises capacity checks with the launch they guard
        self._last_prune = float("-inf")

    # ---- identity and views -------------------------------------------
    @staticmethod
    def job_id(conversation: str, key: str) -> str:
        digest = hashlib.sha256(f"{conversation}\0{key}".encode()).hexdigest()
        return "dr-" + digest[:40]

    def _alive(self, job_id: str) -> bool:
        thread = self._threads.get(job_id)
        return bool(thread and thread.is_alive())

    def _active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._threads.values() if t.is_alive())

    def _refresh(self, job: dict) -> dict:
        """A runner that stopped heartbeating (process exit, crash) is shown as paused."""
        if job["state"] in ("running", "queued") and not self._alive(job["id"]):
            now = self.clock()
            if self.store.mark_stale(job["id"], now - STALE_SECONDS,
                                     "Paused: the runner stopped (restart or crash). Use resume to continue.", now):
                job = self.store.get_job(job["id"]) or job
        return job

    def _view(self, job: dict) -> dict:
        view = {
            "id": job["id"],
            "state": job["state"],
            "progress": job["progress"],
            "usage": json.loads(job["usage"]) if job["usage"] else None,
            "createdAt": _iso(job["created_at"]),
            "updatedAt": _iso(job["updated_at"]),
        }
        if job["result"]:
            view["result"] = json.loads(job["result"])
        return view

    def _prune(self) -> None:
        now = self.clock()
        if now - self._last_prune >= PRUNE_EVERY_SECONDS:
            self._last_prune = now
            self.store.prune(now - RETENTION_SECONDS)

    def _same_request(self, job: dict | None, canonical: str) -> dict:
        if job is None:
            raise ContractError("unknown jobId for this conversation")
        if job["request"] != canonical:
            raise ContractError("idempotencyKey was already used with a different request")
        return self._view(self._refresh(job))

    def _load(self, conversation: str, job_id: str) -> dict:
        self._refuse_if_stopping()
        self._prune()
        job = self.store.get_job(job_id)
        if not job or job["conversation"] != conversation:
            raise ContractError("unknown jobId for this conversation")
        return self._refresh(job)

    # ---- actions ------------------------------------------------------
    def get(self, conversation: str, job_id: str) -> dict:
        return self._view(self._load(conversation, job_id))

    def raw_result(self, conversation: str, job_id: str) -> tuple[dict, str | None]:
        job = self._load(conversation, job_id)
        return job, job["result"]

    def start(self, conversation: str, key: str, request: dict) -> dict:
        self._refuse_if_stopping()
        self._prune()
        job_id = self.job_id(conversation, key)
        canonical = canonical_request(request)
        existing = self.store.get_job(job_id)
        if existing:
            return self._same_request(existing, canonical)
        with self._admit:
            self._refuse_if_stopping()
            existing = self.store.get_job(job_id)  # recheck: an identical start may have just won
            if existing:
                return self._same_request(existing, canonical)
            if self._active_count() >= self.max_active:
                raise ContractError(f"{self.max_active} research jobs are already running; try again when one finishes.")
            outcome = self.store.insert_job_if_room(job_id, conversation, key, canonical, self.clock(),
                                                    MAX_JOBS_PER_CONVERSATION)
            if outcome == "exists":  # another caller won the race; it must have sent the same request
                return self._same_request(self.store.get_job(job_id), canonical)
            if outcome == "full":
                raise ContractError(f"This conversation already keeps {MAX_JOBS_PER_CONVERSATION} research jobs; "
                                    "older ones expire 7 days after their last update.")
            self._launch(job_id, replay_only=None)
        return self.wait(job_id, self.foreground_wait)

    def resume(self, conversation: str, job_id: str) -> dict:
        job = self._load(conversation, job_id)
        if job["state"] in TERMINAL_STATES or self._alive(job_id) or job["state"] == "running":
            return self._view(job)
        replay = None
        if self.store.is_cancel_requested(job_id):
            replay = "cancelled"
        elif job["deadline_at"] is not None and self.clock() >= job["deadline_at"]:
            replay = "deadline"
        with self._admit:
            self._refuse_if_stopping()
            if self._active_count() >= self.max_active:
                raise ContractError(f"{self.max_active} research jobs are already running; try again when one finishes.")
            self._launch(job_id, replay_only=replay)
        return self.wait(job_id, self.foreground_wait)

    def cancel(self, conversation: str, job_id: str, wait: float | None = None) -> dict:
        job = self._load(conversation, job_id)
        if job["state"] in TERMINAL_STATES:
            return self._view(job)
        self.store.request_cancel(job_id, self.clock())
        event = self._cancel_events.get(job_id)
        if event:
            event.set()
        if not self._alive(job_id) and job["state"] != "running":
            # Paused or never started: replay recorded receipts only, then settle as cancelled.
            with self._admit:
                if not self._stopping.is_set():  # while unloading, the flag alone is recorded
                    self._launch(job_id, replay_only="cancelled")
        return self.wait(job_id, min(self.foreground_wait, self.cancel_wait) if wait is None else wait)

    def wait(self, job_id: str, timeout: float) -> dict:
        thread = self._threads.get(job_id)
        if thread and timeout > 0:
            thread.join(timeout)
        job = self.store.get_job(job_id)
        if job is None:
            raise ContractError("unknown jobId for this conversation")
        return self._view(job)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Plugin unload: stop every local runner at its next call, record its job as paused (resumable,
        receipts kept), and close the database once no runner still needs it."""
        with self._admit:  # no launch can be half-done while we take the snapshot
            self._stopping.set()
            with self._lock:
                events = list(self._cancel_events.values())
                threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if not any(t.is_alive() for t in threads):
            self.store.close()

    def _refuse_if_stopping(self) -> None:
        if self._stopping.is_set():
            raise ContractError("The deep-research plugin is unloading; try again in a moment.")

    # ---- runner -------------------------------------------------------
    def _launch(self, job_id: str, replay_only: str | None) -> None:
        job = self.store.get_job(job_id)
        request = json.loads(job["request"])
        now = self.clock()
        if not self.store.claim(job_id, now, now + request["budget"]["durationMs"] / 1000):
            return
        event = threading.Event()
        # Carry the caller's context (Hermes binds the active profile there) into the runner.
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(self._run, job_id, request, replay_only, event),
                                  name=f"deep-research-{job_id[:11]}", daemon=True)
        with self._lock:
            self._cancel_events[job_id] = event
            self._threads[job_id] = thread
        thread.start()

    def _run(self, job_id: str, request: dict, replay_only: str | None, cancel_event: threading.Event) -> None:
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    self.store.heartbeat(job_id, self.clock())
                except Exception:  # noqa: BLE001 - a busy database must not end the heartbeat
                    continue

        threading.Thread(target=beat, name=f"deep-research-beat-{job_id[:11]}", daemon=True).start()
        try:
            job = self.store.get_job(job_id)
            budget = Budget(request["budget"], deadline_at=job["deadline_at"], clock=self.clock)

            def cancelled() -> bool:
                return cancel_event.is_set() or self._stopping.is_set() or self.store.is_cancel_requested(job_id)

            engine = Engine(
                request, self.io_factory(), JobSteps(self.store, job_id), budget,
                documents_at=_iso(job["created_at"]), replay_only=replay_only, is_cancelled=cancelled,
                progress=lambda msg, usage: self.store.update_progress(job_id, msg, usage, self.clock()),
            )
            result = engine.run()
            if self._stopping.is_set() and not self.store.is_cancel_requested(job_id):
                self.store.pause(job_id, "Paused: the plugin was unloaded. Use resume to continue.", self.clock())
                return
            state = "indeterminate" if engine.uncertain else result["status"]
            progress = {
                "completed": "Completed",
                "partial": "Finished with gaps",
                "cancelled": "Cancelled",
                "indeterminate": "Finished; an interrupted operation's outcome is unknown and was not repeated",
            }[state]
            self.store.finish(job_id, state, progress, json.dumps(result, ensure_ascii=False, sort_keys=True),
                              result["usage"], self.clock())
        except Exception as exc:  # noqa: BLE001 - recorded as job failure, never raised into the host
            self.store.fail(job_id, f"Failed: {type(exc).__name__}", self.clock())
        finally:
            stop.set()
            with self._lock:
                self._cancel_events.pop(job_id, None)
                if self._threads.get(job_id) is threading.current_thread():
                    del self._threads[job_id]
