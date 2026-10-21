"""SQLite task history and a composition wrapper for the official GroupLeader."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from .._task_order import accepts_task_update, TERMINAL_STATES


class TaskJournal:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("a durable SQLite file is required")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS coordinator_terms (name TEXT PRIMARY KEY, term INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS task_attempts (
                    attempt_id TEXT PRIMARY KEY, logical_task_id TEXT NOT NULL,
                    group_id TEXT, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    partner_aic TEXT NOT NULL, command TEXT NOT NULL, state TEXT NOT NULL,
                    coordinator_term INTEGER NOT NULL, content TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS task_attempt_lookup ON task_attempts(session_id, task_id, partner_aic);
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
                    state TEXT NOT NULL, artifact_digest TEXT, artifact TEXT, recorded_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS takeover_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    failed_partner TEXT NOT NULL, replacement TEXT, reason TEXT NOT NULL,
                    fence TEXT, state TEXT NOT NULL, recorded_at REAL NOT NULL);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record_attempt(self, logical_task_id: str, *, session_id: str, task_id: str,
                       partner_aic: str = "*", group_id: str | None = None,
                       command: str = "start", coordinator_term: int = 0,
                       content: str | None = None) -> str:
        attempt_id = str(uuid.uuid4())
        now = time.time()
        with self._connect() as conn:
            conn.execute("INSERT INTO task_attempts VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?, ?, ?)",
                         (attempt_id, logical_task_id, group_id, session_id, task_id, partner_aic, command, coordinator_term, content, now, now))
            conn.execute("INSERT INTO task_events(attempt_id,state,recorded_at) VALUES (?, 'dispatching', ?)", (attempt_id, now))
        return attempt_id

    def transition(self, attempt_id: str, state: str, artifact: Any = None) -> None:
        value = state.value if hasattr(state, "value") else str(state)
        encoded = json.dumps(artifact, ensure_ascii=False, sort_keys=True) if artifact is not None else None
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest() if encoded else None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT state FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if current is None:
                raise KeyError(attempt_id)
            apply = current["state"] not in TERMINAL_STATES or current["state"] == value
            if apply and isinstance(artifact, dict) and "status" in artifact:
                previous = None
                for event in conn.execute("SELECT artifact FROM task_events WHERE attempt_id=? AND artifact IS NOT NULL ORDER BY event_id", (attempt_id,)):
                    record = json.loads(event["artifact"])
                    if isinstance(record, dict) and "status" in record and accepts_task_update(record, previous):
                        previous = record
                apply = accepts_task_update(artifact, previous)
            if apply:
                conn.execute("UPDATE task_attempts SET state=?, updated_at=? WHERE attempt_id=?", (value, time.time(), attempt_id))
            # Preserve every received result even when its state is obsolete.
            conn.execute("INSERT INTO task_events(attempt_id,state,artifact_digest,artifact,recorded_at) VALUES (?, ?, ?, ?, ?)",
                         (attempt_id, value, digest, encoded, time.time()))

    def on_task_update(self, session: Any, task: Any) -> None:
        session_id = session.session_id
        with self._connect() as conn:
            rows = conn.execute("SELECT attempt_id FROM task_attempts WHERE session_id=? AND task_id=? AND partner_aic IN (?, '*') AND created_at=(SELECT MAX(b.created_at) FROM task_attempts b WHERE b.session_id=task_attempts.session_id AND b.task_id=task_attempts.task_id AND b.partner_aic=task_attempts.partner_aic)",
                                (session_id, task.taskId, task.senderId)).fetchall()
        artifact = task.model_dump(mode="json", exclude_none=True) if hasattr(task, "model_dump") else None
        if not rows:
            attempt_id = self.record_attempt(task.taskId, session_id=session_id, task_id=task.taskId,
                                             partner_aic=task.senderId, group_id=getattr(session, "group_id", None))
            rows = [{"attempt_id": attempt_id}]
        for row in rows:
            self.transition(row["attempt_id"], task.status.state, artifact)

    def list_tasks(self, *, in_flight_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT * FROM task_attempts"
            if in_flight_only:
                query += " WHERE state NOT IN ('completed','cancelled','canceled','failed','rejected') AND NOT EXISTS (SELECT 1 FROM task_attempts newer WHERE newer.logical_task_id=task_attempts.logical_task_id AND newer.partner_aic=task_attempts.partner_aic AND newer.created_at>task_attempts.created_at)"
            return [dict(row) for row in conn.execute(query + " ORDER BY created_at")]

    def events(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE attempt_id=? ORDER BY event_id", (attempt_id,))]

    def record_takeover(self, session_id: str, failed_partner: str, reason: str, *, replacement: str | None = None,
                        fence: Any = None, state: str = "started") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO takeover_events(session_id,failed_partner,replacement,reason,fence,state,recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (session_id, failed_partner, replacement, reason, json.dumps(fence) if fence else None, state, time.time()))


class CoordinatorTerm:
    """Allocate one increasing term per coordinator process start, not per task."""
    def __init__(self, journal: TaskJournal | str | Path, name: str = "default"):
        self.journal = journal if isinstance(journal, TaskJournal) else TaskJournal(journal)
        self.name = name
        self.value = self.next_term()

    def next_term(self) -> int:
        with self.journal._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT term FROM coordinator_terms WHERE name=?", (self.name,)).fetchone()
            value = (row[0] if row else 0) + 1
            conn.execute("INSERT OR REPLACE INTO coordinator_terms VALUES (?, ?)", (self.name, value))
        self.value = value
        return value

    def __int__(self) -> int:
        return self.value


class ResumableGroupLeader:
    """Composition adapter; original leader method arguments remain supported.

    Dispatch intent is persisted *before* transport. A transport exception is
    delivery_unknown, because the receiver might have accepted the message.
    """
    def __init__(self, leader: Any, journal: TaskJournal, term: CoordinatorTerm | None = None):
        self.leader = leader
        self.journal = journal
        self.term = term or CoordinatorTerm(journal)
        leader.add_task_listener(journal.on_task_update)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.leader, name)

    async def _dispatch(self, command: str, session_id: str, task_id: str, logical_task_id: str,
                        partners: list[str], content: str | None, callback: Any) -> Any:
        session = getattr(self.leader, "group_sessions", {}).get(session_id)
        if not partners and session is not None:
            client = getattr(session, "leader_mq_client", None)
            partners = list(getattr(client, "partners", {}) or {})
        attempts = [self.journal.record_attempt(logical_task_id, session_id=session_id, task_id=task_id,
                                               partner_aic=partner, group_id=getattr(session, "group_id", None),
                                               command=command, coordinator_term=int(self.term), content=content)
                    for partner in (partners or ["*"])]
        try:
            result = await callback()
        except BaseException:
            for attempt in attempts:
                self.journal.transition(attempt, "delivery_unknown")
            raise
        for attempt in attempts:
            # A fast receiver can publish a state before the send coroutine returns.
            with self.journal._connect() as conn:
                state = conn.execute("SELECT state FROM task_attempts WHERE attempt_id=?", (attempt,)).fetchone()[0]
            if state == "dispatching":
                self.journal.transition(attempt, "sent")
        return result

    async def start_task(self, session_id: str, *, task_content: str, task_id: str | None = None,
                         target_partners: list[str] | None = None, logical_task_id: str | None = None) -> str:
        task_id = task_id or f"task-{uuid.uuid4()}"
        return await self._dispatch("start", session_id, task_id, logical_task_id or task_id, target_partners or [], task_content,
            lambda: self.leader.start_task(session_id, task_content=task_content, task_id=task_id, target_partners=target_partners))

    async def continue_task(self, session_id: str, task_id: str, content: str, target_partner: str | None = None,
                            *, logical_task_id: str | None = None) -> None:
        return await self._dispatch("continue", session_id, task_id, logical_task_id or task_id,
            [target_partner] if target_partner else [], content,
            lambda: self.leader.continue_task(session_id, task_id, content, target_partner))

    async def complete_task(self, session_id: str, task_id: str, target_partner: str | None = None,
                            *, logical_task_id: str | None = None) -> None:
        return await self._dispatch("complete", session_id, task_id, logical_task_id or task_id,
            [target_partner] if target_partner else [], None,
            lambda: self.leader.complete_task(session_id, task_id, target_partner))

