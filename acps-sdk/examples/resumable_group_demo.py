"""Offline official-client demo: python examples/resumable_group_demo.py --output DIR.

Uses in-process delivery in place of RabbitMQ. Native task-command dispatch,
partner processing, AccessEmitter, and the shared SQLite ledger run unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acps_sdk.aip.aip_group_leader import GroupLeaderMqClient
from acps_sdk.aip.aip_group_partner import GroupPartnerMqClient
from acps_sdk.aip.resumable import Fence, OperationLedger, ResumableGroupPartner
from acps_sdk.amp.access_emitter import AccessEmitter
from acps_sdk.amp.message_emitter import MessageEmitter


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    session_id = "demo-" + uuid.uuid4().hex
    ledger = OperationLedger(output / "operations.sqlite", "demo-resource")
    resource = output / "resource.json"
    if not resource.exists():
        resource.write_text("0", encoding="utf-8")
    before = int(resource.read_text(encoding="utf-8"))
    current = ledger.current_fence() or Fence(0)
    fence = Fence(current.coordinator_term + 1)
    outcomes = []
    leader = GroupLeaderMqClient("demo-leader",
        access_emitter=AccessEmitter(output / "leader-access.jsonl", "demo-leader"),
        message_emitter=MessageEmitter(output / "leader-message.jsonl", "demo-leader"))
    leader._group_id = "demo-group"
    partner = GroupPartnerMqClient("demo-partner",
        access_emitter=AccessEmitter(output / "partner-access.jsonl", "demo-partner"),
        message_emitter=MessageEmitter(output / "partner-message.jsonl", "demo-partner"))

    async def modify(command, is_mentioned):
        actual = int(resource.read_text(encoding="utf-8")) + 1
        resource.write_text(str(actual), encoding="utf-8")
        return {"counter": actual}

    async def report(command, outcome):
        outcomes.append(outcome.to_dict())

    ResumableGroupPartner(partner, ledger).set_command_handler(modify, result_handler=report)

    async def deliver(command):
        await partner._handle_task_command(command)

    leader.publish_message = deliver
    operation_id = session_id + ":increment"
    payload = {"risk": "write", "operation_id": operation_id, "parameters": {"increment": 1},
               "fence": fence.to_dict(), "tool": "demo.counter.increment"}
    # Duplicate transport delivery carries the same operation identity.
    for attempt in (1, 2):
        await leader.start_task(session_id, json.dumps(payload), f"{session_id}-attempt-{attempt}", ["demo-partner"])
    newer = await asyncio.to_thread(ledger.next_fence, fence.coordinator_term)
    await leader.start_task(session_id, json.dumps(payload), session_id + "-stale", ["demo-partner"])
    effects = int(resource.read_text(encoding="utf-8")) - before
    result = {"session_id": session_id, "transport": "in_process", "statuses": [o["status"] for o in outcomes],
              "resource_effects": effects, "duplicate_effects": max(0, effects - 1),
              "leader_task_commands": 3, "partner_addressed_tasks": 3, "active_fence": newer.to_dict()}
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result["statuses"] == ["applied", "skipped_already_applied", "rejected_stale_fence"]
    assert effects == 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output" / "resumable-demo")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output)), ensure_ascii=False, indent=2))

