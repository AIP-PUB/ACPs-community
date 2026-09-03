"""tests/unit/test_generic_runner_access.py — GenericRunner Access 发射单元测试（EA4）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from acps_sdk.aip.access_status import error_info_from_rpc_response, status_from_rpc_response
from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_rpc_model import JSONRPCError, RpcRequest, RpcResponse
from acps_sdk.amp.trace_context import TRACEPARENT_HEADER, TraceContext, format_traceparent, new_span_id, new_trace_id

from partners.generic_runner import GenericRunner


def test_different_agent_names_have_different_access_files(mock_generic_runner: GenericRunner) -> None:
    base_dir = mock_generic_runner.base_dir
    with patch("partners.generic_runner.AsyncOpenAI"):
        runner_a = GenericRunner("agent_alpha", base_dir)
        runner_b = GenericRunner("agent_beta", base_dir)

    assert runner_a._access_emitter._log_file != runner_b._access_emitter._log_file
    assert "agent_alpha" in runner_a._access_emitter._log_file.name
    assert "agent_beta" in runner_b._access_emitter._log_file.name


def test_access_service_name_includes_agent_name(mock_generic_runner: GenericRunner) -> None:
    assert mock_generic_runner._service_name == f"demo-partner-{mock_generic_runner.agent_name}"


def test_status_from_rpc_response_mappings() -> None:
    err_resp = RpcResponse(id="1", error=JSONRPCError(code=-32001, message="not found"))
    assert status_from_rpc_response(err_resp) == 404

    failed_task = TaskResult(
        id="r1",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="partner",
        senderId="p1",
        taskId="t1",
        sessionId="s1",
        status=TaskStatus(state=TaskState.Failed, stateChangedAt=datetime.now(UTC).isoformat()),
    )
    failed_resp = RpcResponse(id="2", result=failed_task)
    assert status_from_rpc_response(failed_resp) == 500

    rejected_task = TaskResult(
        id="r2",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="partner",
        senderId="p1",
        taskId="t2",
        sessionId="s1",
        status=TaskStatus(state=TaskState.Rejected, stateChangedAt=datetime.now(UTC).isoformat()),
    )
    rejected_resp = RpcResponse(id="3", result=rejected_task)
    assert status_from_rpc_response(rejected_resp) == 200
    err = error_info_from_rpc_response(rejected_resp)
    assert err is not None
    assert err.code == "REJECTED"


@pytest.mark.asyncio
async def test_emit_server_access_span_with_traceparent(
    mock_generic_runner: GenericRunner,
    rpc_request_factory: Callable[..., RpcRequest],
    tmp_path: Path,
) -> None:
    from acps_sdk.aip.access_status import error_info_from_rpc_response, status_from_rpc_response
    from acps_sdk.amp.models import AccessBody, AccessParticipant, AccessRequest, AccessResponse
    from acps_sdk.amp.trace_context import parse_traceparent

    trace_id = new_trace_id()
    client_span = new_span_id()
    traceparent = format_traceparent(TraceContext(trace_id=trace_id, span_id=client_span))

    log_file = tmp_path / "amp_access_test.jsonl"
    mock_generic_runner._access_emitter._log_file = log_file

    request = rpc_request_factory("hello")
    command = request.params.command
    assert command.taskId is not None
    parent = parse_traceparent(traceparent)
    assert parent is not None
    server_span = new_span_id()

    completed = TaskResult(
        id="r-ok",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="partner",
        senderId=mock_generic_runner._aic,
        taskId=command.taskId,
        sessionId=command.sessionId,
        status=TaskStatus(state=TaskState.Accepted, stateChangedAt=datetime.now(UTC).isoformat()),
    )
    resp = RpcResponse(id="rpc-1", result=completed)
    body = AccessBody(
        request=AccessRequest(
            method=str(command.command.value),
            url="/rpc",
            route="/rpc",
            headers={TRACEPARENT_HEADER: traceparent},
            bodySizeBytes=10,
        ),
        response=AccessResponse(statusCode=status_from_rpc_response(resp), bodySizeBytes=20),
        caller=AccessParticipant(aic=command.senderId, serviceName="demo-leader"),
        callee=AccessParticipant(aic=mock_generic_runner._aic, serviceName=mock_generic_runner._service_name),
        error=error_info_from_rpc_response(resp),
        durationMs=5,
    )
    await mock_generic_runner._access_emitter.emit(
        body,
        trace_id=parent.trace_id,
        span_id=server_span,
        parent_span_id=parent.span_id,
        correlation_id=command.sessionId,
    )

    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["trace_id"] == trace_id
    assert record["parent_span_id"] == client_span
    assert record["body"]["caller"]["aic"] == "test-leader-001"
    assert record["body"]["callee"]["aic"] == mock_generic_runner._aic
