"""
S3 测试：AipStreamClient
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus, TextDataItem
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_model import StreamEventData, StreamResponse

NOW = datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# SSE 测试辅助：生成 bytes 流
# ---------------------------------------------------------------------------


def _make_sse_bytes(*events: StreamResponse) -> bytes:
    """将多个 StreamResponse 拼接为 SSE bytes。"""
    parts = []
    for ev in events:
        json_str = ev.model_dump_json(exclude_none=True)
        parts.append(f"data: {json_str}\n\n")
    return "".join(parts).encode()


def _task_result_resp(seq: int, task_id: str, state: TaskState) -> StreamResponse:
    from acps_sdk.aip.aip_stream_model import StreamEventData

    tr = TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )
    return StreamResponse(id="rpc-1", result=StreamEventData(eventSeq=seq, eventData=tr))


class _SSETransport(httpx.AsyncBaseTransport):
    """返回 SSE 字节流的 mock 传输层。"""

    def __init__(self, sse_bytes: bytes) -> None:
        self._bytes = sse_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=self._bytes,
            headers={"Content-Type": "text/event-stream"},
        )


class _RpcTransport(httpx.AsyncBaseTransport):
    """回显请求 ID 的 mock RPC 传输层（确保 ID 匹配校验通过）。"""

    def __init__(self, task_result_dict: dict) -> None:
        self._task_result = task_result_dict

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content)
        req_id = req_body.get("id")
        resp = {"jsonrpc": "2.0", "id": req_id, "result": self._task_result}
        return httpx.Response(
            200, content=json.dumps(resp).encode(), headers={"Content-Type": "application/json"}
        )


# ---------------------------------------------------------------------------
# _make_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_command_start():
    """_make_command 构建 start TaskCommand，task_id 不填时自动生成 UUID。"""
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="leader-1",
        identity_binding_enabled=False,
    )
    cmd = client._make_command(command_type="start", session_id="sess-1")
    assert cmd.taskId is not None
    assert cmd.command.value == "start"
    await client.close()


@pytest.mark.asyncio
async def test_make_command_with_task_id():
    """_make_command 传入 task_id 时使用给定值。"""
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="leader-1",
        identity_binding_enabled=False,
    )
    cmd = client._make_command(command_type="cancel", session_id="sess-1", task_id="fixed-task")
    assert cmd.taskId == "fixed-task"
    await client.close()


# ---------------------------------------------------------------------------
# _parse_sse_line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_sse_line_valid():
    """有效 'data: ...' 行解析为 StreamResponse。"""
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        identity_binding_enabled=False,
    )
    resp = _task_result_resp(1, "t-1", TaskState.Working)
    line = "data: " + resp.model_dump_json(exclude_none=True)
    parsed = client._parse_sse_line(line)
    assert parsed is not None
    assert parsed.result is not None
    assert parsed.result.eventSeq == 1
    await client.close()


@pytest.mark.asyncio
async def test_parse_sse_line_non_data():
    """非 'data: ' 行返回 None（注释行等）。"""
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        identity_binding_enabled=False,
    )
    assert client._parse_sse_line(": heartbeat") is None
    assert client._parse_sse_line("event: ping") is None
    assert client._parse_sse_line("") is None
    await client.close()


# ---------------------------------------------------------------------------
# start_stream 正常流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stream_yields_events():
    """start_stream 正确 yield TaskResult（从 SSE stream 解析）。"""
    task_id = "t-stream"
    sse_bytes = _make_sse_bytes(
        _task_result_resp(1, task_id, TaskState.Working),
        _task_result_resp(2, task_id, TaskState.Completed),
    )
    transport = _SSETransport(sse_bytes)
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )

    events: list[StreamResponse] = []
    async for event in client.start_stream(session_id="sess-1", task_id=task_id):
        events.append(event)

    assert len(events) == 2
    assert events[0].result.eventSeq == 1  # type: ignore
    assert events[1].result.eventSeq == 2  # type: ignore
    await client.close()


# ---------------------------------------------------------------------------
# complete / cancel（使用 RPC 客户端）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_calls_rpc():
    """complete 发送 complete 命令到 RPC 端点并返回 TaskResult。"""
    task_result = TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId="t-complete",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=NOW),
    )
    task_result_dict = json.loads(task_result.model_dump_json(exclude_none=True))
    transport = _RpcTransport(task_result_dict)

    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    result = await client.complete(task_id="t-complete", session_id="sess-1")
    assert result.status.state == TaskState.Completed
    await client.close()


@pytest.mark.asyncio
async def test_cancel_calls_rpc():
    """cancel 发送 cancel 命令并返回 TaskResult。"""
    task_result = TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId="t-cancel",
        status=TaskStatus(state=TaskState.Canceled, stateChangedAt=NOW),
    )
    task_result_dict = json.loads(task_result.model_dump_json(exclude_none=True))
    transport = _RpcTransport(task_result_dict)

    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    result = await client.cancel(task_id="t-cancel", session_id="sess-1")
    assert result.status.state == TaskState.Canceled
    await client.close()


# ---------------------------------------------------------------------------
# S3 新增：last_event_seq + stream_with_reconnect + StreamProtocolError
# ---------------------------------------------------------------------------

from acps_sdk.aip.aip_stream_client import StreamProtocolError
from acps_sdk.aip.aip_rpc_model import JSONRPCError


def _error_resp(code: int, msg: str) -> StreamResponse:
    """构造带 error 字段的 StreamResponse（服务端明确错误帧）。"""
    return StreamResponse(id="rpc-err", error=JSONRPCError(code=code, message=msg))


class _FailOnceSSETransport(httpx.AsyncBaseTransport):
    """第一次调用抛 RemoteProtocolError，之后返回 SSE 字节流。"""

    def __init__(self, sse_bytes: bytes) -> None:
        self._bytes = sse_bytes
        self._call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._call_count += 1
        if self._call_count == 1:
            raise httpx.RemoteProtocolError("Connection dropped", request=request)
        return httpx.Response(
            200, content=self._bytes, headers={"Content-Type": "text/event-stream"}
        )


@pytest.mark.asyncio
async def test_multiline_sse_data_concatenated():
    """多行 data: 字段应被拼接后解析为一个 StreamResponse。

    SSE 规范：一个事件可由多个 data: 行组成，各行值以 LF 拼接，事件以空行终止。
    测试方法：将 JSON 先序列化为 pretty-print（自然含 LF），再按行拆成多个 data: 行。
    拼接后得到合法 pretty-print JSON，可被 Pydantic 正确解析。
    """
    import json
    from datetime import datetime, timezone

    NOW = datetime.now(timezone.utc).isoformat()
    task_result = TaskResult(
        id="r1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId="t-ml",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=NOW),
    )
    event_data = StreamEventData(eventSeq=1, eventData=task_result)
    response = StreamResponse(id="1", result=event_data)

    # Pretty-print JSON 含换行，每行构成一个 data: 子行
    pretty_json = json.dumps(response.model_dump(exclude_none=True), indent=2)
    json_lines = pretty_json.split("\n")
    assert len(json_lines) > 1, "pretty-print JSON should have multiple lines"

    # 构建多行 SSE 事件体：每个 JSON 行是一个 data: 行，末尾空行结束事件
    sse_lines = "\n".join(f"data: {l}" for l in json_lines) + "\n\n"
    multiline_body = sse_lines.encode()

    class _MultiLineTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=multiline_body,
            )

    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=_MultiLineTransport(),
        identity_binding_enabled=False,
    )
    events = []
    async for ev in client.start_stream(session_id="s", task_id="t-ml"):
        events.append(ev)
    await client.close()

    assert len(events) == 1, f"Expected 1 event, got {len(events)}"
    assert events[0].result is not None
    assert events[0].result.eventSeq == 1


@pytest.mark.asyncio
async def test_last_event_seq_updated_during_stream():
    """start_stream 成功后，last_event_seq 应等于最后收到的 eventSeq。"""
    task_id = "t-seq"
    sse_bytes = _make_sse_bytes(
        _task_result_resp(10, task_id, TaskState.Working),
        _task_result_resp(20, task_id, TaskState.Completed),
    )
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=_SSETransport(sse_bytes),
        identity_binding_enabled=False,
    )
    assert client.last_event_seq is None
    async for _ in client.start_stream(session_id="s", task_id=task_id):
        pass
    assert client.last_event_seq == 20
    await client.close()


@pytest.mark.asyncio
async def test_stream_with_reconnect_happy_path():
    """stream_with_reconnect 无故障时正常 yield 所有事件。"""
    task_id = "t-reconnect-ok"
    sse_bytes = _make_sse_bytes(
        _task_result_resp(1, task_id, TaskState.Working),
        _task_result_resp(2, task_id, TaskState.Completed),
    )
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=_SSETransport(sse_bytes),
        identity_binding_enabled=False,
    )
    events = []
    async for ev in client.stream_with_reconnect(
        session_id="s", user_input="hello", task_id=task_id, backoff_s=0
    ):
        events.append(ev)
    assert len(events) == 2
    assert events[-1].result.eventSeq == 2  # type: ignore
    await client.close()


@pytest.mark.asyncio
async def test_stream_with_reconnect_raises_stream_protocol_error():
    """Partner 推送 error 帧时，stream_with_reconnect 抛出 StreamProtocolError（不重连）。"""
    sse_bytes = _make_sse_bytes(_error_resp(-32000, "buffer expired"))
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=_SSETransport(sse_bytes),
        identity_binding_enabled=False,
    )
    with pytest.raises(StreamProtocolError) as exc_info:
        async for _ in client.stream_with_reconnect(
            session_id="s", user_input="hi", backoff_s=0
        ):
            pass
    assert exc_info.value.code == -32000
    assert "buffer expired" in exc_info.value.message
    await client.close()


@pytest.mark.asyncio
async def test_stream_with_reconnect_max_reconnects_exceeded():
    """超过 max_reconnects 次后仍失败，向外抛出连接异常。"""

    class _AlwaysFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("always fail", request=request)

    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=_AlwaysFailTransport(),
        identity_binding_enabled=False,
    )
    with pytest.raises(httpx.RemoteProtocolError):
        async for _ in client.stream_with_reconnect(
            session_id="s",
            user_input="hi",
            max_reconnects=2,
            backoff_s=0,
        ):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_stream_with_reconnect_reconnects_on_network_error():
    """网络中断（RemoteProtocolError）后自动通过 re_stream 续传。"""
    task_id = "t-recon"
    # 重连后 re_stream 从 seq=0 续传，返回这两个事件
    sse_bytes = _make_sse_bytes(
        _task_result_resp(1, task_id, TaskState.Working),
        _task_result_resp(2, task_id, TaskState.Completed),
    )
    transport = _FailOnceSSETransport(sse_bytes)
    client = AipStreamClient(
        partner_stream_url="http://p/stream",
        partner_rpc_url="http://p/rpc",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    events = []
    async for ev in client.stream_with_reconnect(
        session_id="s",
        user_input="hello",
        task_id=task_id,
        max_reconnects=1,
        backoff_s=0,
    ):
        events.append(ev)

    assert transport._call_count == 2, "应该先失败一次再重连一次"
    assert len(events) == 2
    assert events[-1].result.eventSeq == 2  # type: ignore
    await client.close()
