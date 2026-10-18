"""Explicit handover packages and ADP-driven group partner replacement."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable

from acps_sdk.adp import DiscoveryRequest

from .journal import TaskJournal
from .ledger import Fence, OperationLedger


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class ResumePackage:
    logical_task_id: str
    goal: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    remaining_tasks: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    fence: Fence = field(default_factory=lambda: Fence(0))
    created_at: float = field(default_factory=time.time)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResumePackage":
        value = dict(data)
        value["fence"] = Fence(**value.get("fence", {"coordinator_term": 0}))
        if value.get("schema_version", "1.0") != "1.0":
            raise ValueError("unsupported resume package schema_version")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> "ResumePackage":
        return cls.from_dict(json.loads(value))

    def reusable_evidence(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Only evidence with an explicit future expiry can be reused."""
        now = time.time() if now is None else now
        return [item for item in self.evidence_refs
                if isinstance(item.get("expires_at"), (float, int)) and item["expires_at"] > now]


class TakeoverManager:
    """Compose native leader APIs with injected ADP transport/capability probes.

    The SDK's ADP module defines models, not an authenticated discovery HTTP
    client, so ``discover`` accepts its real DiscoveryRequest. ``status_probe``
    must await a *reply*, not treat successful management-command publishing as
    liveness. ``wait_joined`` must observe group membership before dispatching.
    """
    def __init__(self, leader: Any, journal: TaskJournal, ledger: OperationLedger | None,
                 discover: Callable[[DiscoveryRequest], Any], confirm_capability: Callable[[dict[str, Any], str], Any],
                 *, status_probe: Callable[[str, str], Any] | None = None,
                 heartbeat_probe: Callable[[str], Any] | None = None,
                 wait_joined: Callable[[str, str], Any] | None = None,
                 resume_payload_factory: Callable[[ResumePackage], Any] | None = None,
                 advance_fence: Callable[[dict[str, Any], Fence], Any] | None = None,
                 timeout: float = 5.0):
        if ledger is None and advance_fence is None:
            raise ValueError("a local ledger or remote advance_fence callback is required")
        self.leader, self.journal, self.ledger = leader, journal, ledger
        self.discover, self.confirm_capability = discover, confirm_capability
        self.status_probe, self.heartbeat_probe = status_probe, heartbeat_probe
        self.wait_joined, self.timeout = wait_joined, timeout
        self.resume_payload_factory = resume_payload_factory
        self.advance_fence = advance_fence
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def detect_failure(self, session_id: str, partner_aic: str, *, deadline: float | None = None,
                             last_update: float | None = None, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if deadline is not None and now >= deadline and (last_update is None or last_update < deadline):
            return "task_deadline_expired"
        if self.status_probe is not None:
            await self.leader.check_partner_status(partner_aic, session_id=session_id)
            try:
                reply = await asyncio.wait_for(_await(self.status_probe(session_id, partner_aic)), self.timeout)
            except (asyncio.TimeoutError, ConnectionError):
                return "management_status_timeout"
            if reply is False or reply is None:
                return "management_status_no_reply"
        if self.heartbeat_probe is not None:
            heartbeat = await _await(self.heartbeat_probe(partner_aic))
            if heartbeat is False or (isinstance(heartbeat, dict) and heartbeat.get("expires_at", now + 1) <= now):
                return "amp_heartbeat_expired"
        return None

    @staticmethod
    def _candidates(response: Any) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return response
        result = getattr(response, "result", None)
        if result is None:
            return []
        return [{"aic": aic, "acs_data": acs, "skill_id": skill.skill_id}
                for aic, acs, skill, _ in result.iter_agent_skills()]

    async def _wait_membership(self, session_id: str, aic: str) -> bool:
        if self.wait_joined is not None:
            return bool(await asyncio.wait_for(_await(self.wait_joined(session_id, aic)), self.timeout))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            runtime = self.leader.get_group_runtime(session_id)
            if any(member.get("partner_aic") == aic and member.get("connected") for member in runtime.get("members", [])):
                return True
            await asyncio.sleep(min(0.05, self.timeout))
        return False

    async def takeover(self, session_id: str, failed_partner: str, skill: str, package: ResumePackage,
                       *, reason: str, partner_acs_factory: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        if not reason:
            raise ValueError("a recorded failure reason is required")
        lock = self._locks.setdefault((session_id, failed_partner), asyncio.Lock())
        async with lock:
            self.journal.record_takeover(session_id, failed_partner, reason)
            response = await _await(self.discover(DiscoveryRequest(type="explicit", query=skill, limit=10)))
            candidates = self._candidates(response)
            removed = False
            for candidate in candidates:
                aic = candidate.get("aic")
                if not aic or aic == failed_partner:
                    continue
                if not await _await(self.confirm_capability(candidate, skill)):
                    continue
                current = (self.ledger.current_fence() if self.ledger else None) or package.fence
                term = max(current.coordinator_term, package.fence.coordinator_term)
                # Advance before any replacement can modify the shared resource.
                minimum_version = max(current.takeover_version if current.coordinator_term == term else 0,
                                      package.fence.takeover_version if package.fence.coordinator_term == term else 0)
                if self.advance_fence:
                    value = await _await(self.advance_fence(candidate, Fence(term, minimum_version + 1)))
                    fence = value if isinstance(value, Fence) else Fence(**value)
                    if fence < Fence(term, minimum_version + 1):
                        raise ValueError("remote fence did not advance")
                else:
                    fence = await asyncio.to_thread(self.ledger.next_fence, term,
                        minimum_takeover_version=minimum_version)
                handover = replace(package, fence=fence)
                self.journal.record_takeover(session_id, failed_partner, reason, replacement=aic, fence=fence.to_dict(), state="fenced")
                try:
                    if not removed:
                        await self.leader.force_remove_partner(failed_partner, session_id=session_id)
                        removed = True
                    acs_data = candidate.get("acs_data", candidate)
                    if partner_acs_factory:
                        acs = partner_acs_factory(acs_data)
                    else:
                        from acps_sdk.aip.aip_group_model import ACSObject
                        acs = ACSObject.model_validate(acs_data)
                    invited = await self.leader.invite_partner(session_id, acs, partner_acs_data=acs_data)
                    if not invited or not await self._wait_membership(session_id, aic):
                        raise TimeoutError("replacement did not join the group")
                    payload = (await _await(self.resume_payload_factory(handover)) if self.resume_payload_factory
                               else {"type": "resume", "resume_package": handover.to_dict()})
                    if not isinstance(payload, dict):
                        raise TypeError("resume_payload_factory must return a JSON object")
                    task_id = await self.leader.start_task(session_id,
                        task_content=json.dumps(payload, ensure_ascii=False),
                        target_partners=[aic])
                except Exception as exc:
                    self.journal.record_takeover(session_id, failed_partner, reason, replacement=aic, fence=fence.to_dict(), state="failed:" + type(exc).__name__)
                    continue
                self.journal.record_takeover(session_id, failed_partner, reason, replacement=aic, fence=fence.to_dict(), state="dispatched")
                return {"partner_aic": aic, "task_id": task_id, "fence": fence.to_dict(), "resume_package": handover.to_dict(), "reason": reason}
            raise RuntimeError("no capable replacement completed the takeover")

