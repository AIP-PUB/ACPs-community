"""tests/aip/test_group_leader_message.py — GroupLeaderMqClient opt-in message 埋点测试。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_group_leader import GroupLeaderMqClient
from acps_sdk.aip.aip_group_model import GroupMgmtCommand, GroupMgmtCommandType
from acps_sdk.amp.message_emitter import MessageEmitter
from acps_sdk.amp.trace_context import TRACEPARENT_HEADER, TraceContext


def _task_command(*, cmd_id: str = "cmd-001", session_id: str = "sess-1") -> TaskCommand:
    return TaskCommand(
        id=cmd_id,
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId="leader-aic",
        sessionId=session_id,
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


def _setup_client(tmp_path: Path, *, with_emitter: bool = True) -> tuple[GroupLeaderMqClient, MessageEmitter | None, Path]:
    log_file = tmp_path / "message.jsonl"
    emitter = MessageEmitter(log_file, aic="leader-aic") if with_emitter else None
    client = GroupLeaderMqClient(
        leader_aic="leader-aic",
        message_emitter=emitter,
        message_system="rabbitmq",
        trace_context_provider=lambda: TraceContext(trace_id="a" * 32, span_id="b" * 16),
        identity_binding_enabled=False,
    )
    client._exchange = AsyncMock()
    client._exchange_name = "group.test"
    client._group_id = "group-1"
    client.rabbitmq_vhost = "acps"
    client._leader_queue = MagicMock()
    client._leader_queue.name = "leader.queue"
    return client, emitter, log_file


@pytest.mark.asyncio
async def test_publish_task_command_emits_send_and_traceparent(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    captured: dict[str, Any] = {}

    async def _capture_publish(msg: Any, routing_key: str = "") -> None:
        captured["headers"] = dict(msg.headers or {})
        captured["routing_key"] = routing_key

    client._exchange.publish = AsyncMock(side_effect=_capture_publish)

    await client.publish_message(_task_command())

    assert TRACEPARENT_HEADER in captured["headers"]
    assert captured["routing_key"] == ""
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["log_type"] == "message"
    assert record["body"]["eventType"] == "send"
    assert record["body"]["messageId"] == "cmd-001"
    assert record["body"]["destination"]["name"] == "group.test"
    assert record["trace_id"] == "a" * 32


@pytest.mark.asyncio
async def test_on_message_task_result_emits_receive_and_ack(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    send_span = "c" * 16
    handler = AsyncMock()
    client.set_message_handler(handler)

    on_message = await _capture_on_message(client)

    message = _make_incoming_message(
        _task_result(),
        headers={TRACEPARENT_HEADER: f"00-{'d' * 32}-{send_span}-01"},
    )
    await on_message(message)

    handler.assert_awaited_once()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    receive = json.loads(lines[0])
    ack = json.loads(lines[1])
    assert receive["body"]["eventType"] == "receive"
    assert receive["parent_span_id"] == send_span
    assert ack["body"]["eventType"] == "ack"


@pytest.mark.asyncio
async def test_on_message_handler_error_emits_nack_and_reraises(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)

    async def _fail(_msg: Any) -> None:
        raise RuntimeError("handler failed")

    client.set_message_handler(_fail)
    on_message = await _capture_on_message(client)
    message = _make_incoming_message(_task_result())

    with pytest.raises(RuntimeError, match="handler failed"):
        await on_message(message)

    record = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["body"]["eventType"] == "nack"
    assert record["body"]["settlement"]["reason"] == "handler failed"


@pytest.mark.asyncio
async def test_self_sent_message_not_emitted(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    client.set_message_handler(AsyncMock())
    on_message = await _capture_on_message(client)
    await on_message(_make_incoming_message(_task_result(sender_id="leader-aic")))
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_mgmt_message_not_emitted_and_no_traceparent(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path)
    captured: dict[str, Any] = {}

    async def _capture_publish(msg: Any, routing_key: str = "") -> None:
        captured["headers"] = dict(msg.headers or {})

    client._exchange.publish = AsyncMock(side_effect=_capture_publish)

    mgmt = GroupMgmtCommand(
        id="mgmt-1",
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId="leader-aic",
        sessionId="sess-1",
        groupId="group-1",
        command=GroupMgmtCommandType.MUTE,
    )
    await client.publish_message(mgmt)

    assert TRACEPARENT_HEADER not in captured.get("headers", {})
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_without_emitter_behavior_unchanged(tmp_path: Path) -> None:
    client, _, log_file = _setup_client(tmp_path, with_emitter=False)
    captured: dict[str, Any] = {}

    async def _capture_publish(msg: Any, routing_key: str = "") -> None:
        captured["headers"] = dict(msg.headers or {})

    client._exchange.publish = AsyncMock(side_effect=_capture_publish)
    await client.publish_message(_task_command())
    assert TRACEPARENT_HEADER not in captured.get("headers", {})
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_publish(tmp_path: Path) -> None:
    client, emitter, _ = _setup_client(tmp_path)
    client._exchange.publish = AsyncMock()
    emitter.emit = AsyncMock(side_effect=RuntimeError("emit failed"))  # type: ignore[method-assign]

    await client.publish_message(_task_command())
    client._exchange.publish.assert_awaited_once()


async def _capture_on_message(client: GroupLeaderMqClient):
    captured: dict[str, Any] = {}

    async def _consume(handler: Any) -> None:
        captured["handler"] = handler

    client._leader_queue.consume = _consume

    def _fake_create_task(coro: Any) -> asyncio.Task:
        async def _run() -> None:
            await coro

        return asyncio.get_running_loop().create_task(_run())

    with patch("acps_sdk.aip.aip_group_leader.asyncio.create_task", side_effect=_fake_create_task):
        await client.start_consuming()
        await asyncio.sleep(0)
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
