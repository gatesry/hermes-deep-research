"""Durable job state in SQLite (JSON columns only; nothing is unpickled)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    conversation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request TEXT NOT NULL,
    state TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '',
    usage TEXT,
    result TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deadline_at REAL,
    heartbeat_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_conversation ON jobs(conversation);
CREATE TABLE IF NOT EXISTS steps (
    job_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (job_id, step_id)
);
"""

LIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("completed", "partial", "cancelled", "failed", "indeterminate")


class Store:
    def __init__(self, connect: Callable[[], sqlite3.Connection], clock: Callable[[], float] = time.time):
        self._conn = connect()
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.clock = clock
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- jobs ---------------------------------------------------------
    def insert_job(self, job_id: str, conversation: str, key: str, request_json: str, now: float) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO jobs (id, conversation, idempotency_key, request, state, progress, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, 'queued', 'Queued', ?, ?)",
            (job_id, conversation, key, request_json, now, now))
        return cur.rowcount == 1

    def insert_job_if_room(self, job_id: str, conversation: str, key: str, request_json: str, now: float,
                           cap: int) -> str:
        """Insert under one write transaction: "inserted", "exists" or "full". BEGIN IMMEDIATE takes
        the write lock first, so managers sharing the database cannot both pass the quota check."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                    outcome = "exists"
                elif self._conn.execute("SELECT COUNT(*) FROM jobs WHERE conversation = ?",
                                        (conversation,)).fetchone()[0] >= cap:
                    outcome = "full"
                else:
                    self._conn.execute(
                        "INSERT INTO jobs (id, conversation, idempotency_key, request, state, progress, created_at,"
                        " updated_at) VALUES (?, ?, ?, ?, 'queued', 'Queued', ?, ?)",
                        (job_id, conversation, key, request_json, now, now))
                    outcome = "inserted"
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return outcome

    def get_job(self, job_id: str) -> dict | None:
        row = self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    def count_jobs(self, conversation: str) -> int:
        return self._one("SELECT COUNT(*) FROM jobs WHERE conversation = ?", (conversation,))[0]

    def count_live(self, fresh_after: float) -> int:
        return self._one("SELECT COUNT(*) FROM jobs WHERE state = 'running' AND heartbeat_at >= ?", (fresh_after,))[0]

    def prune(self, cutoff: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM steps WHERE job_id IN (SELECT id FROM jobs WHERE updated_at < ?)", (cutoff,))
            self._conn.execute("DELETE FROM jobs WHERE updated_at < ?", (cutoff,))

    def claim(self, job_id: str, now: float, deadline_at: float) -> bool:
        """Atomically take a queued/paused job; the first run fixes the deadline."""
        cur = self._exec(
            "UPDATE jobs SET state = 'running', progress = 'Researching', heartbeat_at = ?, updated_at = ?,"
            " deadline_at = COALESCE(deadline_at, ?) WHERE id = ? AND state IN ('queued', 'paused')",
            (now, now, deadline_at, job_id))
        return cur.rowcount == 1

    def set_deadline(self, job_id: str, deadline_at: float) -> None:
        self._exec("UPDATE jobs SET deadline_at = ? WHERE id = ?", (deadline_at, job_id))

    def heartbeat(self, job_id: str, now: float) -> None:
        self._exec("UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND state = 'running'", (now, job_id))

    def update_progress(self, job_id: str, progress: str, usage: dict, now: float) -> None:
        self._exec("UPDATE jobs SET progress = ?, usage = ?, heartbeat_at = ?, updated_at = ? WHERE id = ? AND state = 'running'",
                   (progress, json.dumps(usage, sort_keys=True), now, now, job_id))

    def mark_stale(self, job_id: str, stale_before: float, progress: str, now: float) -> bool:
        cur = self._exec(
            "UPDATE jobs SET state = 'paused', progress = ?, updated_at = ? WHERE id = ? AND ("
            "(state = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)) OR (state = 'queued' AND updated_at < ?))",
            (progress, now, job_id, stale_before, stale_before))
        return cur.rowcount == 1

    def request_cancel(self, job_id: str, now: float) -> None:
        self._exec("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now, job_id))

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self._one("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
        return bool(row and row[0])

    def finish(self, job_id: str, state: str, progress: str, result_json: str, usage: dict, now: float) -> None:
        # The stored result is immutable: later writes never replace it.
        self._exec(
            "UPDATE jobs SET state = ?, progress = ?, result = ?, usage = ?, updated_at = ? WHERE id = ? AND result IS NULL",
            (state, progress, result_json, json.dumps(usage, sort_keys=True), now, job_id))

    def fail(self, job_id: str, progress: str, now: float) -> None:
        self._exec("UPDATE jobs SET state = 'failed', progress = ?, updated_at = ? WHERE id = ? AND result IS NULL",
                   (progress, now, job_id))

    # ---- steps --------------------------------------------------------
    def get_step(self, job_id: str, step_id: str) -> dict | None:
        row = self._one("SELECT state, value FROM steps WHERE job_id = ? AND step_id = ?", (job_id, step_id))
        if not row:
            return None
        return {"state": row["state"], "value": json.loads(row["value"]) if row["value"] is not None else None}

    def put_step(self, job_id: str, step_id: str, state: str, value: Any = None) -> None:
        self._exec("INSERT OR REPLACE INTO steps (job_id, step_id, state, value) VALUES (?, ?, ?, ?)",
                   (job_id, step_id, state, None if value is None else json.dumps(value, ensure_ascii=False)))

    def count_steps(self, job_id: str) -> int:
        return self._one("SELECT COUNT(*) FROM steps WHERE job_id = ?", (job_id,))[0]


class JobSteps:
    """Adapter giving the engine a per-job view of the step receipts."""

    def __init__(self, store: Store, job_id: str):
        self.store, self.job_id = store, job_id

    def get(self, key: str) -> dict | None:
        return self.store.get_step(self.job_id, key)

    def put(self, key: str, state: str, value: Any = None) -> None:
        self.store.put_step(self.job_id, key, state, value)
