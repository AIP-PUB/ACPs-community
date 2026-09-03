"""
Leader · StreamEventBus + SSE 端点单元测试

覆盖：
1. StreamEventBus 发布/订阅/多订阅者/哨兵
2. AipExecutor on_event 回调推送 bus（monkey-patch 验证）
3. SSE 端点 404 行为（session 不存在）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
_leader_dir = _project_root / "leader"
for _p in (_leader_dir, _project_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ===========================================================================
# StreamEventBus 单元测试
# ===========================================================================


def test_subscribe_returns_queue():
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("s1")
    assert q is not None
    assert bus.has_subscribers("s1")


def test_unsubscribe_removes_queue():
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("s1")
    bus.unsubscribe("s1", q)
    assert not bus.has_subscribers("s1")


def test_unsubscribe_unknown_queue_is_noop():
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("s1")
    bus.unsubscribe("s1", q)  # first remove
    bus.unsubscribe("s1", q)  # second remove should not raise


@pytest.mark.asyncio
async def test_push_delivers_event():
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("s1")
    resp = StreamResponse(id="ev1")
    bus.push("s1", resp)
    assert q.get_nowait() is resp


@pytest.mark.asyncio
async def test_push_sentinel_delivers_none():
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("s1")
    bus.push("s1", None)
    assert q.get_nowait() is None


def test_push_no_subscribers_is_noop():
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    bus.push("nonexistent", None)  # 不抛异常


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive():
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q1 = bus.subscribe("s1")
    q2 = bus.subscribe("s1")
    resp = StreamResponse(id="multi")
    bus.push("s1", resp)
    bus.push("s1", None)

    assert q1.get_nowait() is resp
    assert q1.get_nowait() is None
    assert q2.get_nowait() is resp
    assert q2.get_nowait() is None


@pytest.mark.asyncio
async def test_different_sessions_isolated():
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    qa = bus.subscribe("a")
    qb = bus.subscribe("b")

    bus.push("a", StreamResponse(id="for-a"))
    bus.push("b", StreamResponse(id="for-b"))

    a_event = qa.get_nowait()
    b_event = qb.get_nowait()
    assert a_event.id == "for-a"
    assert b_event.id == "for-b"
    # 队列各自不溢出
    with pytest.raises(asyncio.QueueEmpty):
        qa.get_nowait()


def test_get_stream_event_bus_singleton():
    """get_stream_event_bus 返回同一个实例。"""
    import assistant.core.stream_event_bus as _m

    original = _m._bus
    _m._bus = None  # 重置单例
    try:
        b1 = _m.get_stream_event_bus()
        b2 = _m.get_stream_event_bus()
        assert b1 is b2
    finally:
        _m._bus = original


# ===========================================================================
# AipExecutor on_event 接入 bus 的集成验证（不启动真实 HTTP）
# ===========================================================================


@pytest.mark.asyncio
async def test_aip_executor_pushes_events_to_bus():
    """验证 AipExecutor._execute_one_stream_partner 确实向 StreamEventBus 推送事件。"""
    import assistant.core.aip_executor as aip_mod
    from acps_sdk.aip.aip_base_model import TaskState
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.core.stream_event_bus import StreamEventBus

    fresh_bus = StreamEventBus()
    aip_mod.get_stream_event_bus = lambda: fresh_bus

    session_id = "s-bus-push-test"
    q = fresh_bus.subscribe(session_id)

    # 构造 AipExecutor 实例
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutorConfig

    executor = AipExecutor(
        leader_aic="test-leader",
        config=ExecutorConfig(),
    )

    # 构造 StreamExecutor.run 的 mock：触发一次 on_event 然后返回 mock TaskResult
    mock_final = MagicMock()
    mock_final.status.state = TaskState.Completed
    mock_final.status.dataItems = None

    captured_on_event = None

    async def _fake_run(session_id, user_input, task_id, on_event, **kwargs):
        nonlocal captured_on_event
        captured_on_event = on_event
        await on_event(StreamResponse(id="ev-pushed"))
        return mock_final

    with patch("assistant.core.aip_executor.StreamExecutor") as mock_stream_cls:
        mock_stream_instance = MagicMock()
        mock_stream_instance.run = _fake_run
        mock_stream_instance.close = AsyncMock()
        mock_stream_cls.return_value = mock_stream_instance

        task_info = {
            "endpoint": "http://partner/rpc",
            "aip_task_id": "task-abc",
            "selection": MagicMock(instruction_text="hello", instruction_data=None),
            "dimension_id": "dim1",
        }
        aic, per = await executor._execute_one_stream_partner(session_id, "partner-aic", task_info)

    # 验证 bus 收到了事件和结束哨兵
    events = []
    while not q.empty():
        events.append(q.get_nowait())

    assert any(isinstance(e, StreamResponse) and e.id == "ev-pushed" for e in events), f"events={events}"
    assert events[-1] is None, "最后一个应为结束哨兵 None"

    # 恢复原始引用
    import importlib

    importlib.reload(aip_mod)


# ===========================================================================
# SSE 端点 404 测试（session 不存在）
# ===========================================================================


def _build_minimal_app(session_exists: bool):
    """构建只含 leader routes 的最小 FastAPI 应用。"""
    from assistant.api.routes import init_routes, router
    from fastapi import FastAPI

    sm = MagicMock()
    sm.get_session.return_value = MagicMock() if session_exists else None

    app = FastAPI()
    app.include_router(router)
    orchestrator_mock = MagicMock()
    init_routes(orchestrator_mock, sm)
    return app


@pytest.mark.asyncio
async def test_stream_endpoint_404_for_unknown_session():
    """session 不存在时 SSE 端点应返回 404。"""
    from httpx import ASGITransport, AsyncClient

    with (
        patch("assistant.auth.oidc_enabled", return_value=False),
        patch(
            "assistant.api.routes.oidc_enabled",
            return_value=False,
        ),
    ):
        app = _build_minimal_app(session_exists=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/stream/nonexistent-session-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_endpoint_sse_yields_done():
    """
    SSE 端点应在收到 None 哨兵后输出 done 信号，对普通事件输出 data: JSON。

    直接调用路由函数并消费 StreamingResponse.body_iterator，
    不经过 HTTPX，避免 SSE 连接永久挂起。
    """
    import assistant.core.stream_event_bus as bus_mod
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.api import routes as routes_mod
    from assistant.core.stream_event_bus import StreamEventBus

    fresh_bus = StreamEventBus()
    original_fn = bus_mod.get_stream_event_bus
    bus_mod.get_stream_event_bus = lambda: fresh_bus

    try:
        session_id = "s-sse-done-test"

        # 提前把事件推入 bus（端点 subscribe 后能立即收到）
        # 注意：端点 subscribe 在被调用时创建新 Queue，所以需要在调用之前预填
        # 解决方案：在 bus.subscribe 的时候，把数据填进去
        # 更简单：mock subscribe 直接返回预填队列
        pre_q: asyncio.Queue = asyncio.Queue()
        await pre_q.put(StreamResponse(id="sse-ev-1"))
        await pre_q.put(None)

        original_subscribe = fresh_bus.subscribe
        fresh_bus.subscribe = lambda sid: pre_q  # type: ignore[assignment]

        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        sm = MagicMock()
        sm.get_session.return_value = MagicMock()
        routes_mod._session_manager = sm

        with (
            patch("assistant.auth.oidc_enabled", return_value=False),
            patch.object(
                routes_mod,
                "oidc_enabled",
                return_value=False,
            ),
        ):
            response = await routes_mod.stream_session_events(  # type: ignore[arg-type]
                session_id=session_id,
                request=mock_request,
                stream_token=None,
            )
            assert response is not None
            assert response.media_type == "text/event-stream"

            lines = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode()
                lines.append(chunk)
                if '"done":true' in chunk:
                    break

            combined = "".join(lines)
            assert '"done":true' in combined, f"done not in output: {combined!r}"
            assert '"id"' in combined, f"event id not in output: {combined!r}"

    finally:
        bus_mod.get_stream_event_bus = original_fn
        routes_mod._session_manager = None


# ===========================================================================
# 延迟订阅者保障（late-subscriber guarantee）测试
# ===========================================================================


@pytest.mark.asyncio
async def test_late_subscriber_gets_sentinel_immediately():
    """流结束后再订阅，应立即收到哨兵 None（而不是挂起）。"""
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    # 先推送哨兵（此时无订阅者）
    bus.push("late-sess", None)

    # 再订阅
    q = bus.subscribe("late-sess")

    # 应该立即能拿到 None
    item = q.get_nowait()
    assert item is None


@pytest.mark.asyncio
async def test_late_subscriber_not_added_to_active_list():
    """延迟订阅者不应加入活跃订阅列表（流已结束）。"""
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    bus.push("done-sess", None)

    bus.subscribe("done-sess")
    # 流已结束，不应有活跃订阅者
    assert not bus.has_subscribers("done-sess")


@pytest.mark.asyncio
async def test_normal_subscriber_before_stream_ends():
    """先订阅再推送（正常路径），事件仍能正常投递。"""
    from acps_sdk.aip.aip_stream_model import StreamResponse
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    q = bus.subscribe("normal-sess")

    event = StreamResponse(id="ev-normal")
    bus.push("normal-sess", event)
    bus.push("normal-sess", None)

    assert q.get_nowait() is event
    assert q.get_nowait() is None


def test_cleanup_expired_removes_old_done_sessions():
    """超过 TTL 的已结束 session 应被清理。"""
    import time

    from assistant.core.stream_event_bus import _DONE_TTL_S, StreamEventBus

    bus = StreamEventBus()
    bus.push("old-sess", None)

    # 伪造时间：手动设置为过期
    bus._done_at["old-sess"] = time.monotonic() - _DONE_TTL_S - 1.0

    # 触发清理（通过任意 push None）
    bus.push("trigger", None)

    assert "old-sess" not in bus._done_at


def test_cleanup_keeps_recent_done_sessions():
    """未超过 TTL 的已结束 session 不应被清理。"""
    from assistant.core.stream_event_bus import StreamEventBus

    bus = StreamEventBus()
    bus.push("recent-sess", None)

    # 立刻触发清理
    bus.push("trigger2", None)

    assert "recent-sess" in bus._done_at
