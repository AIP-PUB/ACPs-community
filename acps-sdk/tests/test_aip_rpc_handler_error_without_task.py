"""A handler that raises before any task exists must yield a JSON-RPC error, not HTTP 500."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskState, TextDataItem
from acps_sdk.aip.aip_rpc_server import CommandHandlers, TaskManager, handle_rpc_request

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.state = SimpleNamespace(peer_aic=LEADER_AIC, peer_identity=None, peer_identity_error=None)

    async def json(self) -> dict:
        return self._body


def _body(command: TaskCommandType, task_id: str = "task-err-1") -> dict:
    cmd = TaskCommand(
        id=f"cmd-{command.value}",
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId=LEADER_AIC,
        sessionId="session-1",
        command=command,
        taskId=task_id,
        dataItems=[TextDataItem(text="hello")],
    )
    return {"jsonrpc": "2.0", "method": "rpc", "id": "rpc-1",
            "params": {"command": json.loads(cmd.model_dump_json(exclude_none=True))}}


@pytest.fixture(autouse=True)
def _clear_tasks():
    TaskManager._tasks.clear()
    yield
    TaskManager._tasks.clear()


@pytest.mark.asyncio
async def test_on_start_raising_before_task_creation_returns_internal_error() -> None:
    async def on_start(command, task):
        raise RuntimeError("backend unavailable")

    resp = await handle_rpc_request(
        _Request(_body(TaskCommandType.Start)),
        CommandHandlers(on_start=on_start),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )

    assert resp.result is None
    assert resp.error is not None
    assert resp.error.code == -32603
    assert "backend unavailable" in str(resp.error.data)
    assert TaskManager.get_task("task-err-1") is None


@pytest.mark.asyncio
async def test_on_start_raising_after_task_creation_still_marks_task_failed() -> None:
    async def on_start(command, task):
        TaskManager.create_task(command)
        raise RuntimeError("late failure")

    resp = await handle_rpc_request(
        _Request(_body(TaskCommandType.Start)),
        CommandHandlers(on_start=on_start),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )

    assert resp.error is None
    assert resp.result.status.state == TaskState.Failed
