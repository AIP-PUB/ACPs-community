"""
D3 · StreamExecutor 单元测试
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from acps_sdk.aip.aip_base_model import Product, TaskCommand, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_rpc_model import RpcResponse
from acps_sdk.aip.aip_stream_model import (
    SSE_MEDIA_TYPE,
    ProductChunkEvent,
    StreamEventData,
    StreamResponse,
    TaskStatusUpdateEvent,
)

NOW = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _make_task_result(task_id: str, state: TaskState) -> TaskResult:
    return TaskResult(
        id="r1",
        sentAt=NOW,
        senderRole="partner",
        senderId="p1",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


def _sse_line(stream_response: StreamResponse) -> bytes:
    """将 StreamResponse 序列化为 SSE 行。"""
    payload = stream_response.model_dump_json(exclude_none=True)
    return f"data: {payload}\n\n".encode()


def _make_sse_body(events: list[StreamResponse]) -> bytes:
    return b"".join(_sse_line(e) for e in events)


def _make_stream_response(
    seq: int,
    event_data: TaskResult | TaskCommand | TaskStatusUpdateEvent | ProductChunkEvent,
) -> StreamResponse:
    """构建一个包含 StreamEventData 的 StreamResponse（id 自动生成）。"""
    return StreamResponse(
        id=str(seq),
        result=StreamEventData(
            eventSeq=seq,
            eventData=event_data,
        ),
    )


# ---------------------------------------------------------------------------
# Mock Transports
# ---------------------------------------------------------------------------


class _SseTransport(httpx.AsyncBaseTransport):
    """模拟 Partner /stream 端点的 SSE 响应。"""

    def __init__(self, events: list[StreamResponse]):
        self._body = _make_sse_body(events)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": SSE_MEDIA_TYPE},
            content=self._body,
        )


class _RpcTransport(httpx.AsyncBaseTransport):
    """模拟 Partner /rpc 端点的 JSON-RPC 响应。"""

    def __init__(self, task_id: str):
        self._task_id = task_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content)
        rpc_id = req_body.get("id", "1")
        result_obj = _make_task_result(self._task_id, TaskState.Completed)
        rpc_resp = RpcResponse(id=rpc_id, result=result_obj)
        return httpx.Response(200, json=rpc_resp.model_dump(mode="json", exclude_none=True))


# ---------------------------------------------------------------------------
# 测试：run 聚合终态结果
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_aggregates_final_result():
    """
    working → ProductChunk → completed(终态)
    - run() 返回的 TaskResult.status.state == Completed
    - on_event 收到全部 3 个事件
    """
    from assistant.core.stream_executor import StreamExecutor

    task_id = "t-se-1"
    working_result = _make_task_result(task_id, TaskState.Working)
    completed_result = _make_task_result(task_id, TaskState.Completed)

    chunk_event = ProductChunkEvent(
        id="pc1",
        sentAt=NOW,
        senderRole="partner",
        senderId="p1",
        taskId=task_id,
        product=Product(id="pr1", dataItems=[]),
        append=True,
        lastChunk=False,
    )

    sse_events = [
        _make_stream_response(1, working_result),
        _make_stream_response(2, chunk_event),
        _make_stream_response(3, completed_result),
    ]

    sse_transport = _SseTransport(sse_events)
    rpc_transport = _RpcTransport(task_id)

    received: list[StreamResponse] = []

    executor = StreamExecutor(
        partner_base_url="http://fake-partner",
        leader_id="leader-1",
        expected_partner_aic="partner-aic",
        identity_binding_enabled=False,
        sse_transport=sse_transport,
        rpc_transport=rpc_transport,
    )

    async def on_event(event: StreamResponse) -> None:
        received.append(event)

    final = await executor.run(
        session_id="sess-1",
        user_input="test input",
        on_event=on_event,
        task_id=task_id,
    )
    await executor.close()

    assert len(received) == 3, f"Expected 3 events, got {len(received)}"
    assert final is not None
    assert final.status.state == TaskState.Completed


@pytest.mark.asyncio
async def test_run_stops_on_awaiting_input_stable_state():
    """遇到 AwaitingInput 稳定态时应立即返回，不等待流进入终态。"""
    from assistant.core.stream_executor import StreamExecutor

    task_id = "t-se-awaiting-input"
    working_result = _make_task_result(task_id, TaskState.Working)
    awaiting_input_result = _make_task_result(task_id, TaskState.AwaitingInput)

    executor = StreamExecutor(
        partner_base_url="http://fake-partner",
        leader_id="leader-1",
        expected_partner_aic="partner-aic",
        identity_binding_enabled=False,
        rpc_transport=_RpcTransport(task_id),
    )

    async def _hanging_stream() -> AsyncIterator[StreamResponse]:
        yield _make_stream_response(1, working_result)
        yield _make_stream_response(2, awaiting_input_result)
        await asyncio.Event().wait()

    executor.stream_client.stream_with_reconnect = MagicMock(return_value=_hanging_stream())

    received: list[StreamResponse] = []

    async def on_event(event: StreamResponse) -> None:
        received.append(event)

    final = await asyncio.wait_for(
        executor.run(
            session_id="sess-awaiting-input",
            user_input="test input",
            on_event=on_event,
            task_id=task_id,
        ),
        timeout=0.2,
    )
    await executor.close()

    assert len(received) == 2
    assert final is not None
    assert final.status.state == TaskState.AwaitingInput


# ---------------------------------------------------------------------------
# 测试：complete / cancel 走 RPC 不走 SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_calls_rpc_not_sse():
    """complete() 只调用 RPC 客户端，不发 SSE 请求。"""
    from assistant.core.stream_executor import StreamExecutor

    task_id = "t-se-2"
    rpc_transport = _RpcTransport(task_id)

    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request):
            raise AssertionError("SSE transport should not be called by complete()")

    executor = StreamExecutor(
        partner_base_url="http://fake-partner",
        leader_id="leader-1",
        expected_partner_aic="partner-aic",
        identity_binding_enabled=False,
        sse_transport=_FailTransport(),
        rpc_transport=rpc_transport,
    )

    # complete 不应抛出异常
    await executor.complete(task_id=task_id, session_id="sess-1")
    await executor.close()


@pytest.mark.asyncio
async def test_cancel_calls_rpc_not_sse():
    """cancel() 只调用 RPC 客户端，不发 SSE 请求。"""
    from assistant.core.stream_executor import StreamExecutor

    task_id = "t-se-3"
    rpc_transport = _RpcTransport(task_id)

    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request):
            raise AssertionError("SSE transport should not be called by cancel()")

    executor = StreamExecutor(
        partner_base_url="http://fake-partner",
        leader_id="leader-1",
        expected_partner_aic="partner-aic",
        identity_binding_enabled=False,
        sse_transport=_FailTransport(),
        rpc_transport=rpc_transport,
    )

    await executor.cancel(task_id=task_id, session_id="sess-1")
    await executor.close()


# ---------------------------------------------------------------------------
# 测试：reconnect=True 时使用 stream_with_reconnect
# ---------------------------------------------------------------------------


class _FailOnceTransport(httpx.AsyncBaseTransport):
    """第一次请求抛网络错误，之后正常返回 SSE。"""

    def __init__(self, events: list[StreamResponse]):
        self._calls = 0
        self._body = _make_sse_body(events)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._calls += 1
        if self._calls == 1:
            raise httpx.RemoteProtocolError("connection dropped", request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": SSE_MEDIA_TYPE},
            content=self._body,
        )


@pytest.mark.asyncio
async def test_run_with_reconnect_recovers_on_network_error():
    """reconnect=True 时，网络断开后通过 stream_with_reconnect 续传，最终收到所有事件。"""
    from assistant.core.stream_executor import StreamExecutor

    task_id = "t-reconnect"
    working_result = _make_task_result(task_id, TaskState.Working)
    completed_result = _make_task_result(task_id, TaskState.Completed)

    sse_events = [
        _make_stream_response(1, working_result),
        _make_stream_response(2, completed_result),
    ]

    sse_transport = _FailOnceTransport(sse_events)
    rpc_transport = _RpcTransport(task_id)
    received: list = []

    executor = StreamExecutor(
        partner_base_url="http://fake-partner",
        leader_id="leader-1",
        expected_partner_aic="partner-aic",
        identity_binding_enabled=False,
        sse_transport=sse_transport,
        rpc_transport=rpc_transport,
    )

    final = await executor.run(
        session_id="sess-rc",
        user_input="test reconnect",
        on_event=lambda e: received.append(e) or _noop(),
        task_id=task_id,
        reconnect=True,
    )
    await executor.close()

    assert final is not None
    assert final.status.state == TaskState.Completed


async def _noop():
    pass


def test_stream_executor_passes_rpc_transport_to_aip_rpc_client():
    """构造 StreamExecutor 时应把 rpc_transport 直接传给 AipRpcClient。"""
    from assistant.core.stream_executor import StreamExecutor

    rpc_transport = _RpcTransport("t-rpc-transport")

    with (
        patch("assistant.core.stream_executor.AipStreamClient") as mock_stream_cls,
        patch("assistant.core.stream_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_stream = AsyncMock()
        mock_stream.close = AsyncMock()
        mock_stream_cls.return_value = mock_stream

        mock_rpc = AsyncMock()
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = StreamExecutor(
            partner_base_url="http://fake-partner",
            leader_id="leader-1",
            expected_partner_aic="partner-aic",
            identity_binding_enabled=False,
            rpc_transport=rpc_transport,
        )

    assert mock_rpc_cls.call_args.kwargs["transport"] is rpc_transport
    assert executor._rpc_client is mock_rpc
    assert executor.stream_client is mock_stream


@pytest.mark.asyncio
async def test_stream_executor_close_closes_mocked_stream_and_rpc_clients():
    """close 应同时关闭 stream client 与 rpc client。"""
    from assistant.core.stream_executor import StreamExecutor

    with (
        patch("assistant.core.stream_executor.AipStreamClient") as mock_stream_cls,
        patch("assistant.core.stream_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_stream = AsyncMock()
        mock_stream.close = AsyncMock()
        mock_stream_cls.return_value = mock_stream

        mock_rpc = AsyncMock()
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = StreamExecutor(
            partner_base_url="http://fake-partner",
            leader_id="leader-1",
            expected_partner_aic="partner-aic",
            identity_binding_enabled=False,
        )
        await executor.close()

    mock_stream.close.assert_awaited_once()
    mock_rpc.close.assert_awaited_once()
