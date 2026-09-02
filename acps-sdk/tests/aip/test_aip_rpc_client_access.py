"""tests/aip/test_aip_rpc_client_access.py — AipRpcClient opt-in access 埋点测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_rpc_model import RpcResponse
from acps_sdk.amp.access_emitter import AccessEmitter
from acps_sdk.amp.trace_context import TRACEPARENT_HEADER, TraceContext


def _make_command() -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId="leader-aic",
        sessionId="sess-1",
        command=TaskCommandType.Get,
        taskId="task-1",
    )


def _success_response(request_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": TaskResult(
            id="msg-1",
            sentAt="2026-01-01T00:00:00Z",
            senderRole="partner",
            senderId="partner-aic",
            taskId="task-1",
            sessionId="sess-1",
            status=TaskStatus(state=TaskState.Working, stateChangedAt="2026-01-01T00:00:00Z"),
        ).model_dump(),
    }


@pytest.mark.asyncio
async def test_injected_emitter_emits_client_span_and_traceparent(tmp_path: Path) -> None:
    log_file = tmp_path / "access.jsonl"
    emitter = AccessEmitter(log_file, aic="leader-aic")
    client = AipRpcClient(
        partner_url="http://partner.local/rpc",
        leader_id="leader-aic",
        access_emitter=emitter,
        callee_aic="partner-aic",
        caller_service="demo-leader",
        callee_service="demo-partner-x",
        trace_context_provider=lambda: TraceContext(trace_id="a" * 32, span_id="b" * 16),
        identity_binding_enabled=False,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = json.dumps(_success_response("req-1")).encode()
    mock_response.json.return_value = _success_response("req-1")
    mock_response.raise_for_status = MagicMock()

    captured_headers: dict[str, str] = {}

    async def _post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.update(kwargs.get("headers", {}))
        # patch request id inside client by validating returned payload id later
        return mock_response

    client.http_client.post = AsyncMock(side_effect=_post)  # type: ignore[method-assign]

    with patch("uuid.uuid4", return_value="req-1"):
        await client._send_request(_make_command())

    assert TRACEPARENT_HEADER in captured_headers
    assert log_file.exists()
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["log_type"] == "access"
    assert record["trace_id"] == "a" * 32
    assert record["parent_span_id"] == ""
    assert record["body"]["caller"]["aic"] == "leader-aic"
    assert record["body"]["callee"]["aic"] == "partner-aic"
    assert record["body"]["response"]["statusCode"] == 200


@pytest.mark.asyncio
async def test_http_error_still_emits_access_log(tmp_path: Path) -> None:
    log_file = tmp_path / "access.jsonl"
    emitter = AccessEmitter(log_file, aic="leader-aic")
    client = AipRpcClient(
        partner_url="http://partner.local/rpc",
        leader_id="leader-aic",
        access_emitter=emitter,
        callee_aic="partner-aic",
        identity_binding_enabled=False,
    )

    request = httpx.Request("POST", "http://partner.local/rpc")
    response = httpx.Response(500, request=request, text="boom")
    client.http_client.post = AsyncMock(side_effect=httpx.HTTPStatusError("err", request=request, response=response))

    with pytest.raises(Exception):
        await client._send_request(_make_command())

    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["body"]["response"]["statusCode"] == 500
    assert record["body"]["error"] is not None


@pytest.mark.asyncio
async def test_without_emitter_no_header_and_no_file(tmp_path: Path) -> None:
    log_file = tmp_path / "access.jsonl"
    client = AipRpcClient(
        partner_url="http://partner.local/rpc",
        leader_id="leader-aic",
        identity_binding_enabled=False,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = json.dumps(_success_response("req-2")).encode()
    mock_response.json.return_value = _success_response("req-2")
    mock_response.raise_for_status = MagicMock()
    captured_headers: dict[str, str] = {}

    async def _post(*args: Any, **kwargs: Any) -> MagicMock:
        captured_headers.update(kwargs.get("headers", {}))
        return mock_response

    client.http_client.post = AsyncMock(side_effect=_post)  # type: ignore[method-assign]

    with patch("uuid.uuid4", return_value="req-2"):
        resp = await client._send_request(_make_command())
    assert isinstance(resp, RpcResponse)
    assert TRACEPARENT_HEADER not in captured_headers
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_rpc(tmp_path: Path) -> None:
    emitter = AccessEmitter(tmp_path / "access.jsonl", aic="leader-aic")
    client = AipRpcClient(
        partner_url="http://partner.local/rpc",
        leader_id="leader-aic",
        access_emitter=emitter,
        identity_binding_enabled=False,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = json.dumps(_success_response("req-3")).encode()
    mock_response.json.return_value = _success_response("req-3")
    mock_response.raise_for_status = MagicMock()
    client.http_client.post = AsyncMock(return_value=mock_response)

    with (
        patch("uuid.uuid4", return_value="req-3"),
        patch.object(emitter, "emit", AsyncMock(side_effect=RuntimeError("emit failed"))),
    ):
        resp = await client._send_request(_make_command())
    assert isinstance(resp, RpcResponse)
