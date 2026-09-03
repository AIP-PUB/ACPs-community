"""
S2 测试：SSE 编解码 + handle_stream_request
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_model import JSONRPCError
from acps_sdk.aip.aip_stream_model import (
    StreamEventData,
    StreamRequest,
    StreamRequestParams,
    StreamResponse,
)
from acps_sdk.aip.aip_stream_server import (
    BufferedStreamEvent,
    StreamHub,
    TaskStreamChannel,
    add_aip_stream_router,
    build_stream_error_response,
    build_stream_response,
    format_sse,
)

NOW = datetime.now(timezone.utc).isoformat()


def _task_result(task_id: str = "t-1", state: TaskState = TaskState.Working) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


def _task_command(task_id: str = "t-1") -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=NOW,
        senderRole="leader",
        senderId="leader-1",
        sessionId="sess-1",
        command=TaskCommandType.Start,
        taskId=task_id,
    )


def _make_rpc_id(n: int = 1) -> str:
    return str(n)


# ---------------------------------------------------------------------------
# format_sse
# ---------------------------------------------------------------------------


def test_format_sse_basic():
    """format_sse 正确序列化 StreamResponse → bytes。"""
    event_data = StreamEventData(eventSeq=1, eventData=_task_result())
    resp = StreamResponse(id="rpc-1", result=event_data)
    data = format_sse(resp)
    assert isinstance(data, bytes)
    # 应以 "data: " 开头，以 "\n\n" 结尾
    text = data.decode()
    assert text.startswith("data: ")
    assert text.endswith("\n\n")


def test_format_sse_content_valid_json():
    """SSE 数据行内容可解析为合法 JSON。"""
    event_data = StreamEventData(eventSeq=2, eventData=_task_result())
    resp = StreamResponse(id="1", result=event_data)
    text = format_sse(resp).decode()
    json_part = text[len("data: ") : -2]
    parsed = json.loads(json_part)
    assert parsed["result"]["eventSeq"] == 2


def test_format_sse_excludes_none():
    """序列化时 exclude_none=True，空字段不出现在输出中。"""
    event_data = StreamEventData(eventSeq=1, eventData=_task_result())
    resp = StreamResponse(id="1", result=event_data)
    text = format_sse(resp).decode()
    assert '"null"' not in text
    # 如果 error 字段为 None，不应出现在 JSON 中
    assert '"error": null' not in text
    assert '"error":null' not in text


def test_build_stream_response_seq():
    """build_stream_response 使用给定的 event_seq 构建帧。"""
    event = BufferedStreamEvent(event_seq=3, payload=_task_result(), is_terminal=False)
    resp = build_stream_response(rpc_id="1", event=event)
    assert resp.result is not None
    assert resp.result.eventSeq == 3
    assert resp.error is None


def test_build_stream_error_response():
    """build_stream_error_response 生成纯 error 帧，result 为 None。"""
    resp = build_stream_error_response(rpc_id="1", code=-32001, message="not found")
    assert resp.result is None
    assert resp.error is not None
    assert resp.error.code == -32001


# ---------------------------------------------------------------------------
# SSE 生成器（集成：通道 → SSE bytes）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_generator_emits_events():
    """订阅通道，生成器正确产出 SSE bytes。"""
    from acps_sdk.aip.aip_stream_server import _sse_event_generator

    hub = StreamHub()
    task_id = "t-gen"
    hub.get_or_create_channel(task_id)

    # 先发布 2 条，再发布终态触发关闭
    await hub.publish_task_result(task_id, _task_result(task_id=task_id, state=TaskState.Working))
    await hub.publish_task_result(
        task_id, _task_result(task_id=task_id, state=TaskState.Completed)
    )

    chunks: list[bytes] = []
    async for chunk in _sse_event_generator(hub=hub, task_id=task_id, rpc_id="1"):
        chunks.append(chunk)

    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.startswith(b"data: ")


# ---------------------------------------------------------------------------
# FastAPI 路由：正常流
# ---------------------------------------------------------------------------


def _build_app_with_hub(hub: StreamHub) -> FastAPI:
    """构建带 stream 路由的 FastAPI 测试应用。"""
    app = FastAPI()

    async def on_stream_start(command: TaskCommand) -> None:
        pass  # 真实应用在此启动任务处理

    add_aip_stream_router(
        app,
        "/stream",
        hub=hub,
        handlers=on_stream_start,
        identity_binding_enabled=False,
    )
    return app


async def _post_async(app: FastAPI, path: str, **kwargs: object) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, **kwargs)


def _post(app: FastAPI, path: str, **kwargs: object) -> Response:
    return asyncio.run(_post_async(app, path, **kwargs))


def _stream_request_body(task_id: str = "t-http", rpc_id: str = "1") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": rpc_id,
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "sessionId": "sess-1",
                "command": "start",
                "taskId": task_id,
            }
        },
    }


@pytest.mark.asyncio
async def test_stream_endpoint_success():
    """正常流：POST /stream 返回 SSE（200，text/event-stream）。"""
    hub = StreamHub()
    task_id = "t-http-ok"

    # 先写入终态事件，让通道关闭（TestClient 同步读取时不会永久阻塞）
    hub.get_or_create_channel(task_id)
    await hub.publish_task_result(
        task_id, _task_result(task_id=task_id, state=TaskState.Completed)
    )

    app = _build_app_with_hub(hub)
    response = await _post_async(
        app,
        "/stream",
        json=_stream_request_body(task_id=task_id),
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]


def test_stream_endpoint_restream_not_found_404():
    """re-stream 对不存在的任务通道 → 404。"""
    hub = StreamHub()
    app = _build_app_with_hub(hub)

    body = {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": "1",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-x",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "re-stream",
                "taskId": "t-nonexistent",
            }
        },
    }
    response = _post(app, "/stream", json=body)
    assert response.status_code == 404


def test_stream_endpoint_unsupported_command_400():
    """get/cancel 等命令不走 stream 端点 → 400。"""
    hub = StreamHub()
    app = _build_app_with_hub(hub)

    body = {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": "1",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-x",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "get",  # get 不是流式命令
                "taskId": "t-get",
            }
        },
    }
    response = _post(app, "/stream", json=body)
    assert response.status_code == 400


def test_stream_endpoint_invalid_body_400():
    """请求体格式错误 → 400。"""
    hub = StreamHub()
    app = _build_app_with_hub(hub)
    response = _post(app, "/stream", json={"invalid": "body"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# re-stream 续传：成功路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restream_endpoint_replays_buffered_events():
    """re-stream 命令携带 lastEventSeq → Partner 从缓冲区续传后续事件。"""
    hub = StreamHub()
    task_id = "t-restream-ok"
    hub.get_or_create_channel(task_id)

    # 发布 3 条事件，第 3 条为终态（关闭通道，让测试不永久阻塞）
    await hub.publish_task_result(task_id, _task_result(task_id=task_id, state=TaskState.Working))
    await hub.publish_task_result(task_id, _task_result(task_id=task_id, state=TaskState.Working))
    await hub.publish_task_result(task_id, _task_result(task_id=task_id, state=TaskState.Completed))

    app = _build_app_with_hub(hub)
    body = {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": "re-1",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-re",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "sessionId": "sess-re",
                "command": "re-stream",
                "taskId": task_id,
                "commandParams": {"lastEventSeq": 1},  # 续传 seq >= 2
            }
        },
    }
    response = await _post_async(
        app,
        "/stream",
        json=body,
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    # 解析 SSE 帧，应包含 seq=2 和 seq=3 的事件
    lines = [l for l in response.text.splitlines() if l.startswith("data: ")]
    assert len(lines) >= 2
    seqs = []
    for line in lines:
        import json as _json
        obj = _json.loads(line[len("data: "):])
        if obj.get("result"):
            seqs.append(obj["result"]["eventSeq"])
    assert 2 in seqs
    assert 3 in seqs


@pytest.mark.asyncio
async def test_restream_endpoint_buffer_expired_returns_error_frame():
    """re-stream 续传点已超出缓冲区 → 返回 SSE 错误帧（200 + error JSON）。"""
    from acps_sdk.aip.aip_stream_server import TaskStreamChannel

    hub = StreamHub()
    task_id = "t-restream-expired"

    # 使用小缓冲区（2），手动装入通道
    ch = TaskStreamChannel(max_buffer=2)
    hub._channels[task_id] = ch  # type: ignore[attr-defined]

    # 发布 4 条，oldest_buffered_seq 变为 3（缓冲区满后循环覆盖）
    for i in range(4):
        state = TaskState.Working if i < 3 else TaskState.Completed
        await ch.publish(_task_result(task_id=task_id, state=state))

    app = _build_app_with_hub(hub)
    body = {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": "re-2",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-re2",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "sessionId": "sess-re",
                "command": "re-stream",
                "taskId": task_id,
                "commandParams": {"lastEventSeq": 1},  # seq 1 已过期（oldest=3）
            }
        },
    }
    response = await _post_async(
        app,
        "/stream",
        json=body,
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    # 响应体应含 error 帧
    assert "error" in response.text
