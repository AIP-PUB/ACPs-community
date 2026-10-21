from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SDK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SDK_ROOT))
from acps_sdk.aip import TaskResult, TaskState
from acps_sdk.aip.aip_group_leader import GroupLeader
from acps_sdk.aip.resumable import TaskJournal


def result(state, second):
    at = f"2026-10-17T09:57:{second:02d}+00:00"
    return TaskResult(groupId="group", id="result-"+str(second), senderId="partner", senderRole="partner",
        sentAt=at, sessionId="session", taskId="task", status={"state": state, "stateChangedAt": at})


class TaskOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_working_cannot_hide_an_actual_product_ready_state(self):
        leader = object.__new__(GroupLeader)
        leader._task_listeners, leader._last_task_results = [], {}
        session = SimpleNamespace(session_id="session", record_task_state=Mock())
        seen = []
        leader.add_task_listener(lambda _, task: seen.append(task.status.state))
        for task in (result("accepted", 1), result("awaiting-completion", 3), result("working", 2)):
            await leader._handle_task_update(session, task)
        self.assertEqual(session.record_task_state.call_args.kwargs["state"], TaskState.AwaitingCompletion)
        self.assertEqual(len(seen), 3)

    async def test_a_genuine_later_continue_can_return_to_working(self):
        leader = object.__new__(GroupLeader)
        leader._task_listeners, leader._last_task_results = [], {}
        session = SimpleNamespace(session_id="session", record_task_state=Mock())
        await leader._handle_task_update(session, result("awaiting-completion", 3))
        await leader._handle_task_update(session, result("working", 4))
        self.assertEqual(session.record_task_state.call_args.kwargs["state"], TaskState.Working)

    async def test_durable_state_retains_late_events_without_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal(Path(directory) / "journal.sqlite")
            attempt = journal.record_attempt("logical", session_id="session", task_id="task", partner_aic="partner")
            session = SimpleNamespace(session_id="session")
            for task in (result("accepted", 1), result("awaiting-completion", 3), result("working", 2)):
                journal.on_task_update(session, task)
            self.assertEqual(journal.list_tasks()[0]["state"], "awaiting-completion")
            self.assertEqual([event["state"] for event in journal.events(attempt)],
                             ["dispatching", "accepted", "awaiting-completion", "working"])
            journal.on_task_update(session, result("completed", 4))
            journal.on_task_update(session, result("working", 5))
            self.assertEqual(journal.list_tasks()[0]["state"], "completed")
            self.assertEqual(journal.list_tasks(in_flight_only=True), [])


if __name__ == "__main__":
    unittest.main()
