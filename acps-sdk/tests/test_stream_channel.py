"""
S1 测试：TaskStreamChannel / StreamHub 纯逻辑
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from acps_sdk.aip.aip_base_model import (
    Product,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_stream_model import TaskStatusUpdateEvent, ProductChunkEvent
from acps_sdk.aip.aip_stream_server import BufferedStreamEvent, StreamHub, TaskStreamChannel

NOW = datetime.now(timezone.utc).isoformat()


def _task_result(state: TaskState = TaskState.Working, task_id: str = "t-1") -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


def _status_event(task_id: str = "t-1") -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        id="ev-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=TaskState.Working, stateChangedAt=NOW),
    )


def _chunk_event(task_id: str = "t-1") -> ProductChunkEvent:
    return ProductChunkEvent(
        id="ev-2",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        product=Product(id="p-1", dataItems=[TextDataItem(text="chunk")]),
        append=True,
        lastChunk=False,
    )


# ---------------------------------------------------------------------------
# TaskStreamChannel — 序列号单调递增
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_seq_monotonic():
    ch = TaskStreamChannel(max_buffer=10)
    for i in range(3):
        await ch.publish(_task_result())
    assert ch.latest_seq == 3


# ---------------------------------------------------------------------------
# 环形缓冲：oldest_buffered_seq 更新
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oldest_buffered_seq():
    ch = TaskStreamChannel(max_buffer=3)
    for _ in range(4):
        await ch.publish(_task_result())
    # 缓冲区只保留 seq 2,3,4；oldest = 2
    assert ch.oldest_buffered_seq == 2


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_and_replay():
    ch = TaskStreamChannel(max_buffer=10)
    for _ in range(5):
        await ch.publish(_task_result())
    replayed = ch.replay(last_event_seq=2)
    seqs = [e.event_seq for e in replayed]
    assert seqs == [3, 4, 5]


@pytest.mark.asyncio
async def test_replay_none_returns_all():
    ch = TaskStreamChannel(max_buffer=10)
    for _ in range(3):
        await ch.publish(_task_result())
    replayed = ch.replay(last_event_seq=None)
    assert len(replayed) == 3


# ---------------------------------------------------------------------------
# can_resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_resume_valid():
    ch = TaskStreamChannel(max_buffer=5)
    for _ in range(3):
        await ch.publish(_task_result())
    # oldest=1, last_event_seq=1 (== oldest-1+1) → 可续传
    assert ch.can_resume(last_event_seq=1) is True


@pytest.mark.asyncio
async def test_can_resume_expired():
    ch = TaskStreamChannel(max_buffer=3)
    for _ in range(5):
        await ch.publish(_task_result())
    # oldest=3, last_event_seq=1 → 过期
    assert ch.can_resume(last_event_seq=1) is False


@pytest.mark.asyncio
async def test_can_resume_none():
    ch = TaskStreamChannel(max_buffer=5)
    for _ in range(3):
        await ch.publish(_task_result())
    assert ch.can_resume(last_event_seq=None) is True


# ---------------------------------------------------------------------------
# subscribe：重放 + 实时
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_replays_then_live():
    ch = TaskStreamChannel(max_buffer=10)
    # 先发 3 条
    for _ in range(3):
        await ch.publish(_task_result())

    received: list[BufferedStreamEvent] = []

    async def collect():
        async for ev in ch.subscribe(last_event_seq=1):
            received.append(ev)
            if len(received) == 3:
                break

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)          # 让重放先执行（seq 2,3）
    await ch.publish(_task_result())   # 实时第 4 条
    await asyncio.wait_for(collector, timeout=2.0)

    seqs = [e.event_seq for e in received]
    assert seqs == [2, 3, 4]


# ---------------------------------------------------------------------------
# close 停止订阅
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_stops_subscribe():
    ch = TaskStreamChannel(max_buffer=10)

    async def consume():
        events = []
        async for ev in ch.subscribe():
            events.append(ev)
        return events

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await ch.publish(_task_result())
    await ch.close()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert len(result) >= 1
    assert ch.is_closed


# ---------------------------------------------------------------------------
# 终态事件自动关闭通道
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_event_closes_channel():
    ch = TaskStreamChannel(max_buffer=10)
    await ch.publish(_task_result(), is_terminal=True)
    assert ch.is_closed


# ---------------------------------------------------------------------------
# StreamHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_publish_task_result_terminal_auto():
    """终态 TaskResult 发布后 is_terminal 自动判定为 True。"""
    hub = StreamHub()
    task_id = "t-hub"
    hub.get_or_create_channel(task_id)

    completed_result = _task_result(state=TaskState.Completed, task_id=task_id)
    await hub.publish_task_result(task_id, completed_result)

    ch = hub.get_channel(task_id)
    assert ch is not None
    # 终态发布后通道已关闭
    assert ch.is_closed


@pytest.mark.asyncio
async def test_hub_close_stream_removes_channel():
    hub = StreamHub()
    task_id = "t-remove"
    hub.get_or_create_channel(task_id)
    await hub.close_stream(task_id)
    assert hub.get_channel(task_id) is None
