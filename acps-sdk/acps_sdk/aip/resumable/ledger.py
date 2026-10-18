"""Durable operation replay with resource-scoped fencing.

Only a verified postcondition resolves an uncertain operation. An exception from
an external tool does not establish that the tool had no effect.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect as inspect_module
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def _call(callback: Callable[[], Any]) -> Any:
    value = callback()
    return await value if inspect_module.isawaitable(value) else value


@dataclass(frozen=True, order=True)
class Fence:
    coordinator_term: int
    takeover_version: int = 0

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 0 for v in (self.coordinator_term, self.takeover_version)):
            raise ValueError("fence components must be non-negative integers")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class OperationOutcome:
    operation_id: str
    status: str
    result: Any = None
    state: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperationLedger:
    """SQLite ledger shared by all partners allowed to change a resource domain.

    ``execute`` serializes effects and fence activation with SQLite's writer
    lock. A durable executing record is committed before invoking the action;
    a process death therefore leaves an inspectable uncertain operation.
    ``activate_fence`` is asynchronous so waiting for an in-flight effect does
    not block the event loop. All writers must use this ledger or resource-native
    fencing; it cannot stop an independent tool process that survives its owner.
    """

    def __init__(self, path: str | Path, resource_domain: str = "default", *, timeout: float = 30.0):
        self.path = str(path)
        self.resource_domain = resource_domain
        self.timeout = timeout
        if self.path == ":memory:":
            raise ValueError("a durable SQLite file is required")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operation_fences (
                    resource_domain TEXT PRIMARY KEY, coordinator_term INTEGER NOT NULL,
                    takeover_version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    resource_domain TEXT NOT NULL, operation_id TEXT NOT NULL,
                    parameters_digest TEXT NOT NULL, parameters TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, before_state TEXT, after_state TEXT,
                    coordinator_term INTEGER NOT NULL, takeover_version INTEGER NOT NULL,
                    owner TEXT, error TEXT, updated_at REAL NOT NULL,
                    PRIMARY KEY(resource_domain, operation_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _locked(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        return conn

    def _fence(self, conn: sqlite3.Connection) -> Fence | None:
        row = conn.execute("SELECT * FROM operation_fences WHERE resource_domain=?", (self.resource_domain,)).fetchone()
        return Fence(row["coordinator_term"], row["takeover_version"]) if row else None

    def _activate(self, conn: sqlite3.Connection, fence: Fence) -> bool:
        current = self._fence(conn)
        if current is not None and fence < current:
            return False
        conn.execute("INSERT OR REPLACE INTO operation_fences VALUES (?, ?, ?)",
                     (self.resource_domain, fence.coordinator_term, fence.takeover_version))
        return True

    def advance_fence(self, fence: Fence) -> Fence:
        """Activate a monotonic fence; use activate_fence inside async code."""
        conn = self._locked()
        try:
            if not self._activate(conn, fence):
                raise ValueError("rejected_stale_fence")
            conn.commit()
            return fence
        finally:
            conn.close()

    async def activate_fence(self, fence: Fence) -> Fence:
        return await asyncio.to_thread(self.advance_fence, fence)

    def next_fence(self, coordinator_term: int, *, minimum_takeover_version: int = 0) -> Fence:
        """Atomically allocate a distinct takeover version across coordinators."""
        Fence(coordinator_term, minimum_takeover_version)
        conn = self._locked()
        try:
            current = self._fence(conn)
            if current and coordinator_term < current.coordinator_term:
                raise ValueError("rejected_stale_fence")
            version = max(current.takeover_version if current and current.coordinator_term == coordinator_term else 0,
                          minimum_takeover_version) + 1
            fence = Fence(coordinator_term, version)
            self._activate(conn, fence)
            conn.commit()
            return fence
        finally:
            conn.close()

    def current_fence(self) -> Fence | None:
        with closing(self._connect()) as conn:
            return self._fence(conn)

    def _row(self, conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM operations WHERE resource_domain=? AND operation_id=?",
                            (self.resource_domain, operation_id)).fetchone()

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = self._row(conn, operation_id)
        if row is None:
            return None
        record = dict(row)
        for key in ("parameters", "result", "before_state", "after_state"):
            record[key] = json.loads(record[key]) if record[key] is not None else None
        return record

    def list_operations(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            ids = [r[0] for r in conn.execute("SELECT operation_id FROM operations WHERE resource_domain=? ORDER BY updated_at", (self.resource_domain,))]
        return [self.get(operation_id) for operation_id in ids]

    def record_received(self, operation_id: str, parameters: Any, fence: Fence) -> dict[str, Any]:
        """Persist receipt independently of execution, before acknowledging work."""
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id is required")
        encoded = _json(parameters)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        conn = self._locked()
        try:
            if not self._activate(conn, fence):
                raise ValueError("rejected_stale_fence")
            row = self._row(conn, operation_id)
            if row and row["parameters_digest"] != digest:
                raise ValueError("rejected_parameter_mismatch")
            if row is None:
                conn.execute("INSERT INTO operations VALUES (?, ?, ?, ?, 'received', NULL, NULL, NULL, ?, ?, NULL, NULL, ?)",
                             (self.resource_domain, operation_id, digest, encoded, fence.coordinator_term, fence.takeover_version, time.time()))
            conn.commit()
        finally:
            conn.close()
        return self.get(operation_id)

    def _outcome(self, row: sqlite3.Row, status: str) -> OperationOutcome:
        return OperationOutcome(row["operation_id"], status,
                                json.loads(row["result"]) if row["result"] is not None else None,
                                row["state"], row["error"])

    async def execute(self, operation_id: str, parameters: Any, fence: Fence,
                      action: Callable[[], Any], *, inspect: Callable[[], Any] | None = None,
                      expected_before: Any = None, expected_after: Any = None,
                      retry_if_unchanged: bool = False) -> OperationOutcome:
        """Execute once, or reconcile an interrupted attempt using actual state.

        For executing/unknown/failed records, retry requires all of: inspection,
        an explicit expected_before match, and retry_if_unchanged=True. A caller
        must only opt in when the observation proves the old effect cannot still
        arrive (e.g. the old worker is stopped and the resource serializes writes).
        A matching expected_after resolves applied without issuing another effect.
        """
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id is required")
        encoded = _json(parameters)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        owner = str(uuid.uuid4())
        conn = await asyncio.to_thread(self._locked)
        try:
            if not self._activate(conn, fence):
                conn.rollback()
                return OperationOutcome(operation_id, "rejected_stale_fence")
            row = self._row(conn, operation_id)
            if row and row["parameters_digest"] != digest:
                conn.rollback()
                return OperationOutcome(operation_id, "rejected_parameter_mismatch", state=row["state"])
            if row and row["state"] == "applied":
                conn.commit()
                return self._outcome(row, "skipped_already_applied")
            before = None
            if row and row["state"] in ("executing", "unknown", "failed"):
                if inspect is None:
                    conn.commit()
                    return self._outcome(row, "reconciliation_required")
                before = await _call(inspect)
                if expected_after is not None and before == expected_after:
                    conn.execute("UPDATE operations SET state='applied', after_state=?, result=?, error=NULL, updated_at=? WHERE resource_domain=? AND operation_id=?",
                                 (_json(before), _json(before), time.time(), self.resource_domain, operation_id))
                    conn.commit()
                    return OperationOutcome(operation_id, "reconciled_applied", before, "applied")
                if not (retry_if_unchanged and expected_before is not None and before == expected_before):
                    conn.commit()
                    return self._outcome(row, "reconciliation_required")
            elif inspect is not None:
                before = await _call(inspect)
                if expected_before is not None and before != expected_before:
                    conn.commit()
                    return OperationOutcome(operation_id, "rejected_precondition", state=row["state"] if row else None)
            elif expected_before is not None:
                conn.commit()
                return OperationOutcome(operation_id, "rejected_precondition_unverified", state=row["state"] if row else None)
            if row is None:
                conn.execute("INSERT INTO operations VALUES (?, ?, ?, ?, 'received', NULL, ?, NULL, ?, ?, ?, NULL, ?)",
                             (self.resource_domain, operation_id, digest, encoded, _json(before),
                              fence.coordinator_term, fence.takeover_version, owner, time.time()))
            conn.execute("UPDATE operations SET state='executing', owner=?, before_state=?, coordinator_term=?, takeover_version=?, error=NULL, updated_at=? WHERE resource_domain=? AND operation_id=?",
                         (owner, _json(before), fence.coordinator_term, fence.takeover_version, time.time(), self.resource_domain, operation_id))
            conn.commit()  # This survives a hard process termination during the effect.
        finally:
            conn.close()

        conn = await asyncio.to_thread(self._locked)
        try:
            current = self._fence(conn)
            row = self._row(conn, operation_id)
            if current != fence:
                conn.rollback()
                return OperationOutcome(operation_id, "rejected_stale_fence", state=row["state"])
            if row["state"] == "applied":
                conn.commit()
                return self._outcome(row, "skipped_already_applied")
            if row["owner"] != owner:
                conn.commit()
                return self._outcome(row, "reconciliation_required")
            try:
                result = await _call(action)
                after = await _call(inspect) if inspect is not None else expected_after
                result_json = _json(result)
                if expected_after is not None and after != expected_after:
                    raise RuntimeError("postcondition did not match expected_after")
            except BaseException as exc:
                conn.execute("UPDATE operations SET state='unknown', error=?, updated_at=? WHERE resource_domain=? AND operation_id=?",
                             (type(exc).__name__, time.time(), self.resource_domain, operation_id))
                conn.commit()
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return OperationOutcome(operation_id, "unknown", state="unknown", error=type(exc).__name__)
            conn.execute("UPDATE operations SET state='applied', result=?, after_state=?, error=NULL, updated_at=? WHERE resource_domain=? AND operation_id=?",
                         (result_json, _json(after), time.time(), self.resource_domain, operation_id))
            conn.commit()
            return OperationOutcome(operation_id, "applied", result, "applied")
        finally:
            conn.close()

