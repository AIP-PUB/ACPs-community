from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_server import (
    CommandHandlers,
    DefaultHandlers,
    TaskManager,
    handle_rpc_request,
)

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.state = SimpleNamespace(peer_aic=LEADER_AIC, peer_identity=None, peer_identity_error=None)

    async def json(self) -> dict:
        return self._body


def _command(command: TaskCommandType) -> TaskCommand:
    return TaskCommand(
        id=f"cmd-{command.value}",
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId=LEADER_AIC,
        sessionId="session-1",
        command=command,
        taskId="task-1",
        dataItems=[TextDataItem(text="hello")],
    )


def _body(command: TaskCommandType) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "rpc",
        "id": "rpc-1",
        "params": {"command": json.loads(_command(command).model_dump_json(exclude_none=True))},
    }


@pytest.fixture(autouse=True)
def _clear_tasks():
    TaskManager._tasks.clear()
    yield
    TaskManager._tasks.clear()


async def _start_via_task_manager(command, task):
    task = await DefaultHandlers.start(command, task)
    return TaskManager.update_task_status(task.taskId, TaskState.Working)


async def _get_via_default(command, task):
    return await DefaultHandlers.get(command, task)


async def _continue_raises(command, task):
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_custom_start_using_task_manager_is_signed_with_local_aic() -> None:
    response = await handle_rpc_request(
        _Request(_body(TaskCommandType.Start)),
        CommandHandlers(on_start=_start_via_task_manager),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is None
    assert response.result.senderId == PARTNER_AIC


@pytest.mark.asyncio
async def test_custom_get_on_task_manager_task_is_signed_with_local_aic() -> None:
    TaskManager.create_task(_command(TaskCommandType.Start))
    response = await handle_rpc_request(
        _Request(_body(TaskCommandType.Get)),
        CommandHandlers(on_get=_get_via_default),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is None
    assert response.result.senderId == PARTNER_AIC


@pytest.mark.asyncio
async def test_failed_result_after_handler_exception_is_signed_with_local_aic() -> None:
    TaskManager.create_task(_command(TaskCommandType.Start))
    TaskManager.update_task_status("task-1", TaskState.AwaitingInput)
    response = await handle_rpc_request(
        _Request(_body(TaskCommandType.Continue)),
        CommandHandlers(on_continue=_continue_raises),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.result.status.state == TaskState.Failed
    assert response.result.senderId == PARTNER_AIC


@pytest.mark.asyncio
async def test_custom_handler_own_sender_is_kept() -> None:
    async def on_start(command, task):
        return TaskResult(
            id="result-own",
            sentAt="2026-01-01T00:00:00Z",
            senderRole="partner",
            senderId=PARTNER_AIC,
            taskId=command.taskId,
            sessionId=command.sessionId,
            status=TaskStatus(state=TaskState.Accepted, stateChangedAt="2026-01-01T00:00:00Z"),
        )

    response = await handle_rpc_request(
        _Request(_body(TaskCommandType.Start)),
        CommandHandlers(on_start=on_start),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is None
    assert response.result.id == "result-own"
    assert response.result.senderId == PARTNER_AIC
