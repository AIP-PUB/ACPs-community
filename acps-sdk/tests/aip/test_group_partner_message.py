"""tests/aip/test_group_partner_message.py — GroupPartnerMqClient opt-in message 埋点测试。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_group_partner import GroupPartnerMqClient, _active_cmd_trace
from acps_sdk.amp.message_emitter import MessageEmitter
from acps_sdk.amp.trace_context import TRACEPARENT_HEADER, TraceContext


def _task_command(*, cmd_id: str = "cmd-001", sender_id: str = "leader-aic") -> TaskCommand:
    return TaskCommand(
        id=cmd_id,
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId=sender_id,
        sessionId="sess-1",
        command=TaskCommandType.Get,
        taskId="task-1",
        groupId="group-1",
    )


def _task_result(*, result_id: str = "res-001", sender_id: str = "partner-aic") -> TaskResult:
    return TaskResult(
        id=result_id,
        sentAt="2026-01-01T00:00:00Z",
        senderRole="partner",
        senderId=sender_id,
        taskId="task-1",
        sessionId="sess-1",
        groupId="group-1",
        status=TaskStatus(state=TaskState.Working, stateChangedAt="2026-01-01T00:00:00Z"),
    )


def _setup_client(tmp_path: Path, *, with_emitter: bool = True) -> tuple[GroupPartnerMqClient, MessageEmitter | None, Path]:
    log_file = tmp_path / "message.jsonl"
    emitter = MessageEmitter(log_file, aic="partner-aic") if with_emitter else None
    client = GroupPartnerMqClient(
        partner_aic="partner-aic",
        message_emitter=emitter,
        message_system="rabbitmq",
        identity_binding_enabled=False,
    )
    client._exchange = AsyncMock()
    client._exchange_name = "group.test"
    client._group_id = "group-1"
    client.rabbitmq_vhost = "acps"
    client._queue = MagicMock()
    client._queue_name = "partner.queue"
    return client, emitter, log_file


@pytest.mark.asyncio
async def test_consume_task_command_emits_receive_and_ack(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    send_span = "c" * 16
    client._command_handler = AsyncMock()
    on_message = await _capture_on_message(client)
    await on_message(
        _make_incoming_message(
            _task_command(),
            headers={TRACEPARENT_HEADER: f"00-{'d' * 32}-{send_span}-01"},
        )
    )
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    receive = json.loads(lines[0])
    ack = json.loads(lines[1])
    assert receive["body"]["eventType"] == "receive"
    assert receive["parent_span_id"] == send_span
    assert ack["body"]["eventType"] == "ack"


@pytest.mark.asyncio
async def test_handler_error_emits_nack_clears_contextvar(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)

    async def _fail(_cmd: TaskCommand, _mentioned: bool) -> None:
        raise RuntimeError("handler failed")

    client._command_handler = _fail
    on_message = await _capture_on_message(client)
    with pytest.raises(RuntimeError, match="handler failed"):
        await on_message(_make_incoming_message(_task_command()))
    assert _active_cmd_trace.get() is None
    record = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["body"]["eventType"] == "nack"


@pytest.mark.asyncio
async def test_self_task_result_not_emitted(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    on_message = await _capture_on_message(client)
    await on_message(_make_incoming_message(_task_result(sender_id="partner-aic")))
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_other_task_result_not_emitted(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    on_message = await _capture_on_message(client)
    await on_message(_make_incoming_message(_task_result(sender_id="other-partner")))
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_publish_task_result_emits_send(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    client._exchange.publish = AsyncMock()
    await client._publish_message(_task_result())
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["body"]["eventType"] == "send"


@pytest.mark.asyncio
async def test_o_m6_in_chain_trace_inheritance(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    captured_send: dict[str, Any] = {}

    async def _handler(_cmd: TaskCommand, _mentioned: bool) -> None:
        await client._publish_message(_task_result(result_id="res-chain"))

    client._command_handler = _handler
    on_message = await _capture_on_message(client)
    send_span = "e" * 16
    await on_message(
        _make_incoming_message(
            _task_command(),
            headers={TRACEPARENT_HEADER: f"00-{'f' * 32}-{send_span}-01"},
        )
    )
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    receive = json.loads(lines[0])
    send = json.loads(lines[2])
    assert receive["trace_id"] == send["trace_id"]
    assert send["parent_span_id"] == receive["span_id"]


@pytest.mark.asyncio
async def test_o_m6_out_of_chain_new_trace(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    client._exchange.publish = AsyncMock()
    _active_cmd_trace.set(None)
    await client._publish_message(_task_result(result_id="res-standalone"))
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["parent_span_id"] == ""


@pytest.mark.asyncio
async def test_without_emitter_unchanged(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path, with_emitter=False)
    client._exchange.publish = AsyncMock()
    await client._publish_message(_task_result())
    assert not log_file.exists()


async def _capture_on_message(client: GroupPartnerMqClient):
    captured: dict[str, Any] = {}

    async def _consume(handler: Any) -> str:
        captured["handler"] = handler
        return "tag-1"

    client._queue.consume = _consume
    await client._start_consuming()
    return captured["handler"]


def _make_incoming_message(
    payload: TaskCommand | TaskResult,
    *,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    body = payload.model_dump(exclude_none=True)
    message = MagicMock()
    message.body = json.dumps(body, ensure_ascii=False).encode()
    message.headers = headers
    message.routing_key = ""
    message.redelivered = False

    @asynccontextmanager
    async def _process():
        yield

    message.process = MagicMock(return_value=_process())
    return message
