"""Run without test dependencies: python -m unittest discover -s tests/resumable."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

SDK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SDK_ROOT))

from acps_sdk.aip.aip_base_model import StructuredDataItem, TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus, TextDataItem
from acps_sdk.aip.aip_group_leader import GroupLeader, GroupLeaderMqClient
from acps_sdk.aip.aip_group_partner import GroupPartnerMqClient
from acps_sdk.aip.resumable import CoordinatorTerm, Fence, OperationLedger, ResumePackage, ResumableGroupLeader, ResumableGroupPartner, TakeoverManager, TaskJournal
from acps_sdk.amp.access_emitter import AccessEmitter
from acps_sdk.amp.message_emitter import MessageEmitter


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.ledger = OperationLedger(self.path, "database")
        self.fence = Fence(1, 0)

    def tearDown(self):
        self.temp.cleanup()

    async def test_missing_record_executes_once_and_applied_replays_skip(self):
        calls = []
        async def action():
            calls.append(1)
            return {"changed": True}
        first = await self.ledger.execute("op", {"a": 1}, self.fence, action)
        replay = await OperationLedger(self.path, "database").execute("op", {"a": 1}, self.fence, action)
        self.assertEqual((first.status, replay.status), ("applied", "skipped_already_applied"))
        self.assertEqual(replay.result, {"changed": True})
        self.assertEqual(len(calls), 1)

    async def test_received_checks_precondition_before_modifying(self):
        self.ledger.record_received("op", {}, self.fence)
        action = Mock(return_value="ok")
        mismatch = await self.ledger.execute("op", {}, self.fence, action, inspect=lambda: 9, expected_before=0)
        self.assertEqual(mismatch.status, "rejected_precondition")
        action.assert_not_called()
        actual = {"state": 0}
        def modify():
            actual["state"] = 1
            return "changed"
        result = await self.ledger.execute("op", {}, self.fence, modify, inspect=lambda: actual["state"], expected_before=0, expected_after=1)
        self.assertEqual(result.status, "applied")

    async def test_uncertain_exception_never_automatically_retries(self):
        effects = []
        def interrupted():
            effects.append(1)
            raise TimeoutError("resource might already have changed")
        first = await self.ledger.execute("op", {}, self.fence, interrupted)
        retry = await self.ledger.execute("op", {}, self.fence, interrupted)
        self.assertEqual((first.status, retry.status), ("unknown", "reconciliation_required"))
        self.assertEqual(effects, [1])
        reconciled = await self.ledger.execute("op", {}, self.fence, interrupted, inspect=lambda: 1, expected_after=1)
        self.assertEqual(reconciled.status, "reconciled_applied")
        self.assertEqual(effects, [1])

    async def test_uncertain_retry_requires_inspection_and_explicit_opt_in(self):
        def failed_before_effect():
            raise ConnectionError("not known to the ledger")
        await self.ledger.execute("op", {}, self.fence, failed_before_effect)
        action = Mock(return_value="done")
        unresolved = await self.ledger.execute("op", {}, self.fence, action, inspect=lambda: 0, expected_before=0, expected_after=1)
        self.assertEqual(unresolved.status, "reconciliation_required")
        action.assert_not_called()
        value = [0]
        def effect():
            value[0] += 1
            return "done"
        resolved = await self.ledger.execute("op", {}, self.fence, effect, inspect=lambda: value[0],
                                             expected_before=0, expected_after=1, retry_if_unchanged=True)
        self.assertEqual(resolved.status, "applied")
        self.assertEqual(value, [1])

    async def test_old_fence_rejected_even_for_an_applied_replay(self):
        action = Mock(return_value="changed")
        await self.ledger.execute("op", {}, self.fence, action)
        await self.ledger.activate_fence(Fence(1, 1))
        rejected = await self.ledger.execute("op", {}, self.fence, action)
        newer_term = await self.ledger.execute("next", {}, Fence(2, 0), action)
        self.assertEqual(rejected.status, "rejected_stale_fence")
        self.assertEqual(newer_term.status, "applied")
        self.assertEqual(action.call_count, 2)

    async def test_parameter_conflict_is_not_a_second_effect(self):
        action = Mock(return_value=True)
        await self.ledger.execute("op", {"sql": "one"}, self.fence, action)
        result = await self.ledger.execute("op", {"sql": "two"}, self.fence, action)
        self.assertEqual(result.status, "rejected_parameter_mismatch")
        self.assertEqual(action.call_count, 1)

    async def test_concurrent_connections_do_not_duplicate_effects(self):
        effects = []
        async def effect():
            await asyncio.sleep(0.03)
            effects.append(1)
            return 1
        peers = [OperationLedger(self.path, "database") for _ in range(5)]
        outcomes = await asyncio.gather(*(p.execute("op", {}, self.fence, effect) for p in peers))
        self.assertEqual(len(effects), 1)
        self.assertEqual(sum(o.status == "applied" for o in outcomes), 1)
        self.assertTrue(all(o.status in ("applied", "skipped_already_applied", "reconciliation_required") for o in outcomes))

    async def test_fence_advance_waits_for_current_effect_to_finish(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def action():
            started.set()
            await release.wait()
            return 1
        task = asyncio.create_task(self.ledger.execute("op", {}, self.fence, action))
        await started.wait()
        activation = asyncio.create_task(self.ledger.activate_fence(Fence(1, 1)))
        await asyncio.sleep(0.03)
        self.assertFalse(activation.done())
        release.set()
        self.assertEqual((await task).status, "applied")
        self.assertEqual(await activation, Fence(1, 1))

    async def test_real_process_death_after_effect_reconciles_without_replay(self):
        effect_file = Path(self.temp.name) / "actual.json"
        script = """
import asyncio, os, sys
from pathlib import Path
from acps_sdk.aip.resumable import Fence, OperationLedger
ledger=OperationLedger(sys.argv[1], 'database')
def effect():
    Path(sys.argv[2]).write_text('1', encoding='utf-8')
    os._exit(23)
asyncio.run(ledger.execute('crashed', {}, Fence(1), effect))
"""
        env = dict(os.environ, PYTHONPATH=str(SDK_ROOT))
        child = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path), str(effect_file)], env=env, capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 23, child.stderr.decode(errors="replace"))
        self.assertEqual(self.ledger.get("crashed")["state"], "executing")
        action = Mock(side_effect=AssertionError("must not modify twice"))
        resolved = await self.ledger.execute("crashed", {}, Fence(1, 1), action,
            inspect=lambda: int(effect_file.read_text()), expected_after=1)
        self.assertEqual(resolved.status, "reconciled_applied")
        action.assert_not_called()
        self.assertEqual(effect_file.read_text(), "1")

    def test_fence_rejects_invalid_token_types(self):
        for token in ((-1, 0), (True, 1), (1, "2")):
            with self.assertRaises(ValueError):
                Fence(*token)


class JournalAndTakeoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = TaskJournal(Path(self.temp.name) / "tasks.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    async def test_restart_preserves_attempts_events_and_increasing_terms(self):
        first_term = CoordinatorTerm(self.journal)
        leader = Mock()
        leader.group_sessions = {"sess": SimpleNamespace(group_id="g")}
        leader.start_task = AsyncMock(return_value="task")
        wrapper = ResumableGroupLeader(leader, self.journal, first_term)
        await wrapper.start_task("sess", task_content="diagnose", task_id="task", target_partners=["p"], logical_task_id="logical")
        restarted = TaskJournal(self.journal.path)
        rows = restarted.list_tasks(in_flight_only=True)
        self.assertEqual((rows[0]["logical_task_id"], rows[0]["state"]), ("logical", "sent"))
        task = TaskResult(id="r", senderId="p", senderRole="partner", sentAt="2026-10-17T00:00:00Z",
                          sessionId="sess", groupId="g", taskId="task",
                          status=TaskStatus(state=TaskState.Completed, stateChangedAt="2026-10-17T00:00:00Z"))
        restarted.on_task_update(SimpleNamespace(session_id="sess", group_id="g"), task)
        self.assertEqual(restarted.list_tasks(in_flight_only=True), [])
        self.assertEqual(len(restarted.events(rows[0]["attempt_id"])), 3)
        self.assertEqual(int(CoordinatorTerm(restarted)), int(first_term) + 1)

    async def test_transport_failure_remains_delivery_unknown(self):
        leader = Mock()
        leader.group_sessions = {}
        leader.start_task = AsyncMock(side_effect=TimeoutError)
        wrapper = ResumableGroupLeader(leader, self.journal)
        with self.assertRaises(TimeoutError):
            await wrapper.start_task("sess", task_content="work")
        self.assertEqual(self.journal.list_tasks()[0]["state"], "delivery_unknown")

    def test_observation_evidence_reused_only_until_expiry(self):
        package = ResumePackage("logical", "recover", evidence_refs=[{"ref": "fresh", "expires_at": 100}, {"ref": "old", "expires_at": 50}, {"ref": "unbounded"}], fence=Fence(2, 4))
        restored = ResumePackage.from_json(package.to_json())
        self.assertEqual(restored.fence, Fence(2, 4))
        self.assertEqual(restored.reusable_evidence(now=75), [{"ref": "fresh", "expires_at": 100}])

    async def test_takeover_confirms_invites_waits_and_fences_before_resume(self):
        ledger = OperationLedger(Path(self.temp.name) / "ledger.sqlite")
        await ledger.activate_fence(Fence(2, 0))
        leader = Mock()
        leader.force_remove_partner = AsyncMock(return_value={"removed": True})
        leader.invite_partner = AsyncMock(return_value=True)
        leader.start_task = AsyncMock(return_value="resume-task")
        discovery = AsyncMock(return_value=[{"aic": "old"}, {"aic": "wrong"}, {"aic": "backup", "acs_data": {"aic": "backup"}}])
        confirm = AsyncMock(side_effect=lambda candidate, skill: candidate["aic"] == "backup")
        joined = AsyncMock(return_value=True)
        manager = TakeoverManager(leader, self.journal, ledger, discovery, confirm, wait_joined=joined)
        result = await manager.takeover("sess", "old", "database.diagnose", ResumePackage("logical", "restore", fence=Fence(2)), reason="task_deadline_expired")
        self.assertEqual(result["partner_aic"], "backup")
        self.assertEqual(ledger.current_fence(), Fence(2, 1))
        self.assertEqual(discovery.call_args.args[0].query, "database.diagnose")
        payload = json.loads(leader.start_task.call_args.kwargs["task_content"])
        self.assertEqual(payload["type"], "resume")
        self.assertEqual(payload["resume_package"]["fence"], Fence(2, 1).to_dict())
        stale = await ledger.execute("old-replay", {}, Fence(2), lambda: 1)
        self.assertEqual(stale.status, "rejected_stale_fence")

    async def test_invitation_without_membership_does_not_dispatch(self):
        ledger = OperationLedger(Path(self.temp.name) / "ledger.sqlite")
        leader = Mock()
        leader.force_remove_partner = AsyncMock()
        leader.invite_partner = AsyncMock(return_value=True)
        leader.start_task = AsyncMock()
        manager = TakeoverManager(leader, self.journal, ledger, AsyncMock(return_value=[{"aic": "new"}]),
            AsyncMock(return_value=True), wait_joined=AsyncMock(return_value=False))
        with self.assertRaises(RuntimeError):
            await manager.takeover("sess", "old", "skill", ResumePackage("id", "goal"), reason="timeout")
        leader.start_task.assert_not_called()

    async def test_failure_sources_use_actual_reply_and_priority(self):
        ledger = OperationLedger(Path(self.temp.name) / "ledger.sqlite")
        leader = Mock(check_partner_status=AsyncMock())
        manager = TakeoverManager(leader, self.journal, ledger, Mock(), Mock(), status_probe=AsyncMock(return_value=None), heartbeat_probe=AsyncMock(return_value=False))
        self.assertEqual(await manager.detect_failure("s", "p", deadline=10, now=20), "task_deadline_expired")
        leader.check_partner_status.assert_not_called()
        self.assertEqual(await manager.detect_failure("s", "p", now=20), "management_status_no_reply")
        manager.status_probe = AsyncMock(return_value=True)
        self.assertEqual(await manager.detect_failure("s", "p", now=20), "amp_heartbeat_expired")

    async def test_remote_domain_fence_and_ifc_resume_factory_need_no_local_ledger(self):
        leader = Mock()
        leader.force_remove_partner = AsyncMock()
        leader.invite_partner = AsyncMock(return_value=True)
        leader.start_task = AsyncMock(return_value="resume-ifc")
        activated = []
        async def activate(candidate, proposed):
            self.assertEqual(candidate["aic"], "backup")
            activated.append(proposed)
            return proposed.to_dict()
        def ifc(package):
            return {"schema_version": "ifc/1.0", "incident_id": "incident-one", "logical_task_id": package.logical_task_id,
                    "kind": "resume", "tool": "db.restore_writes", "operation_id": "incident-one:repair",
                    "fence": package.fence.to_dict(), "args": {"resume_package": package.to_dict()}}
        manager = TakeoverManager(leader, self.journal, None,
            AsyncMock(return_value=[{"aic": "backup"}]), AsyncMock(return_value=True),
            wait_joined=AsyncMock(return_value=True), resume_payload_factory=ifc, advance_fence=activate)
        result = await manager.takeover("session", "old", "database.restore", ResumePackage("logical", "recover", fence=Fence(3, 2)), reason="deadline")
        self.assertEqual(activated, [Fence(3, 3)])
        payload = json.loads(leader.start_task.call_args.kwargs["task_content"])
        self.assertEqual((payload["schema_version"], payload["kind"], payload["operation_id"]), ("ifc/1.0", "resume", "incident-one:repair"))
        self.assertEqual(result["fence"], Fence(3, 3).to_dict())


class OfficialGroupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def command(self):
        return TaskCommand(id="cmd", sentAt="2026-10-17T00:00:00Z", senderRole="leader", senderId="leader",
            sessionId="session", groupId="group", taskId="task", command=TaskCommandType.Start,
            mentions=["partner"], dataItems=[TextDataItem(text=json.dumps({"tool": "database.inspect"}))])

    async def test_native_leader_task_listener_runs_after_state_is_recorded(self):
        leader = GroupLeader("leader", {})
        session = Mock(session_id="session")
        task = TaskResult(id="r", sentAt="2026-10-17T00:00:00Z", senderRole="partner", senderId="partner", taskId="task",
            status=TaskStatus(state=TaskState.Working, stateChangedAt="2026-10-17T00:00:00Z"))
        seen = []
        async def listener(s, t):
            self.assertTrue(s.record_task_state.called)
            seen.append(t)
        leader.add_task_listener(listener)
        leader.add_task_listener(listener)
        await leader._handle_task_update(session, task)
        self.assertEqual(seen, [task])
        leader.remove_task_listener(listener)

    async def test_partner_one_access_per_addressed_task_and_failure(self):
        path = Path(self.temp.name) / "partner-access.jsonl"
        client = GroupPartnerMqClient("partner", access_emitter=AccessEmitter(path, "partner"),
            message_emitter=MessageEmitter(Path(self.temp.name) / "message.jsonl", "partner"))
        client.set_command_handler(AsyncMock())
        await client._handle_task_command(self.command())
        ignored = self.command().model_copy(update={"mentions": ["other"]})
        await client._handle_task_command(ignored)
        client.set_command_handler(AsyncMock(side_effect=ValueError("sensitive details")))
        with self.assertRaises(ValueError):
            await client._handle_task_command(self.command().model_copy(update={"taskId": "task-failed"}))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["trace_id"], "session")
        self.assertEqual(rows[0]["correlation_id"], "task")
        self.assertEqual(rows[0]["body"]["request"], {"method": "start", "route": "database.inspect"})
        self.assertEqual(rows[0]["body"]["callee"]["aic"], "partner")
        self.assertEqual(rows[1]["body"]["response"]["statusCode"], 500)
        self.assertNotIn("sensitive details", path.read_text(encoding="utf-8"))

    async def test_leader_send_access_is_202_and_preserves_publisher(self):
        path = Path(self.temp.name) / "leader-access.jsonl"
        client = GroupLeaderMqClient("leader", access_emitter=AccessEmitter(path, "leader"))
        client._group_id = "group"
        client.publish_message = AsyncMock()
        await client.start_task("session", '{"skill":"diagnosis"}', "task", ["partner"])
        client.publish_message.assert_awaited_once()
        row = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(row["body"]["callee"]["aic"], "partner")
        self.assertEqual(row["body"]["response"]["statusCode"], 202)
        self.assertEqual(row["body"]["request"]["route"], "diagnosis")

    async def test_partner_business_tool_error_is_an_access_failure(self):
        path = Path(self.temp.name) / "business-error-access.jsonl"
        client = GroupPartnerMqClient("partner", access_emitter=AccessEmitter(path, "partner"))
        client.set_command_handler(AsyncMock(return_value={"status": "tool_error"}))
        await client._handle_task_command(self.command())
        row = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(row["body"]["response"]["statusCode"], 500)
        self.assertEqual(row["body"]["error"]["code"], "tool_error")

    async def test_partner_start_complete_redelivery_counts_one_structured_route(self):
        path = Path(self.temp.name) / "once-access.jsonl"
        client = GroupPartnerMqClient("partner", access_emitter=AccessEmitter(path, "partner"))
        handler = AsyncMock(return_value={"status": "rejected"})
        client.set_command_handler(handler)
        command = self.command().model_copy(update={"dataItems": [StructuredDataItem(data={"tool": "database.restore"})]})
        await client._handle_task_command(command)
        await client._handle_task_command(command.model_copy(update={"command": TaskCommandType.Complete}))
        await client._handle_task_command(command)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(handler.await_count, 3)
        self.assertEqual(rows[0]["trace_id"], "session")
        self.assertEqual(rows[0]["correlation_id"], "task")
        self.assertEqual(rows[0]["body"]["request"], {"method": "start", "route": "database.restore"})
        self.assertEqual(rows[0]["body"]["response"]["statusCode"], 500)
        self.assertEqual(rows[0]["body"]["error"]["code"], "rejected")
        await client._handle_task_command(command.model_copy(update={"taskId": "new-attempt"}))
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    async def test_leader_start_complete_redelivery_counts_one_access(self):
        path = Path(self.temp.name) / "once-leader-access.jsonl"
        client = GroupLeaderMqClient("leader", access_emitter=AccessEmitter(path, "leader"))
        client._group_id = "group"
        client.publish_message = AsyncMock()
        await client.start_task("session", '{"skill":"diagnosis"}', "task", ["partner"])
        await client.complete_task("task", "session", mentions=["partner"])
        await client.start_task("session", '{"skill":"diagnosis"}', "task", ["partner"])
        self.assertEqual(client.publish_message.await_count, 3)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trace_id"], "session")
        self.assertEqual(rows[0]["correlation_id"], "task")
        self.assertEqual(rows[0]["body"]["request"]["route"], "diagnosis")

    async def test_failed_start_redelivery_keeps_one_first_failure_access(self):
        path = Path(self.temp.name) / "failed-once-access.jsonl"
        client = GroupPartnerMqClient("partner", access_emitter=AccessEmitter(path, "partner"))
        client.set_command_handler(AsyncMock(side_effect=TimeoutError))
        with self.assertRaises(TimeoutError):
            await client._handle_task_command(self.command())
        client.set_command_handler(AsyncMock(return_value={"status": "ok"}))
        await client._handle_task_command(self.command())
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"]["error"]["code"], "TimeoutError")

    async def test_partner_wrapper_requires_operation_identity_and_skips_replay(self):
        client = GroupPartnerMqClient("partner")
        ledger = OperationLedger(Path(self.temp.name) / "ledger.sqlite")
        adapter = ResumableGroupPartner(client, ledger)
        handler = AsyncMock(return_value={"changed": True})
        payload = {"risk": "write"}
        adapter.set_command_handler(handler, payload_extractor=lambda c: payload)
        rejected = await client._command_handler(self.command(), True)
        self.assertEqual(rejected.status, "rejected_missing_operation_identity")
        handler.assert_not_called()
        payload.update(operation_id="op", fence=Fence(1).to_dict(), parameters={})
        first = await client._command_handler(self.command(), True)
        replay = await client._command_handler(self.command(), True)
        self.assertEqual((first.status, replay.status), ("applied", "skipped_already_applied"))
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
