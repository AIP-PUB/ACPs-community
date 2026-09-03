"""
S0 测试：aip_stream_model.py 调整项
- StreamResponse.result 为可选字段（支持纯 error 响应）
- StreamEventData.eventData 使用判别联合
- 新增 StreamEventPayload / SSE_MEDIA_TYPE / SSE_HEADERS 常量
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
)
from acps_sdk.aip.aip_rpc_model import JSONRPCError
from acps_sdk.aip.aip_stream_model import (
    ProductChunkEvent,
    ReStreamCommandParams,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    StreamEventData,
    StreamEventPayload,
    StreamResponse,
    TaskStatusUpdateEvent,
)
from acps_sdk.aip.aip_base_model import Product, TextDataItem

NOW = datetime.now(timezone.utc).isoformat()


def _make_task_result(state: TaskState = TaskState.Working) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId="task-1",
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


def _make_task_command() -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=NOW,
        senderRole="leader",
        senderId="leader-1",
        sessionId="sess-1",
        command=TaskCommandType.Start,
        taskId="task-1",
    )


# ---------------------------------------------------------------------------
# StreamResponse.result 可选性
# ---------------------------------------------------------------------------


def test_stream_response_result_optional():
    """result 可不传，纯 error 响应也合法。"""
    err = JSONRPCError(code=-32001, message="task not found")
    resp = StreamResponse(id="1", error=err)
    assert resp.result is None
    assert resp.error is not None


def test_stream_response_with_result():
    """含 result 的正常响应，result.eventSeq 正确。"""
    event_data = StreamEventData(
        eventSeq=1,
        eventData=_make_task_result(),
    )
    resp = StreamResponse(id="1", result=event_data)
    assert resp.result is not None
    assert resp.result.eventSeq == 1


# ---------------------------------------------------------------------------
# StreamEventData 判别联合（四个分支）
# ---------------------------------------------------------------------------


def test_stream_event_data_discriminated_union_task_result():
    """eventData.type == 'task-result' → 还原为 TaskResult。"""
    raw = {
        "eventSeq": 1,
        "eventData": {
            "type": "task-result",
            "id": "tr-1",
            "sentAt": NOW,
            "senderRole": "partner",
            "senderId": "agent",
            "taskId": "task-1",
            "status": {"state": "working", "stateChangedAt": NOW},
        },
    }
    ed = StreamEventData.model_validate(raw)
    assert isinstance(ed.eventData, TaskResult)
    assert ed.eventData.taskId == "task-1"


def test_stream_event_data_discriminated_union_task_command():
    """eventData.type == 'task-command' → 还原为 TaskCommand（command/commandParams 字段存在）。"""
    raw = {
        "eventSeq": 2,
        "eventData": {
            "type": "task-command",
            "id": "cmd-1",
            "sentAt": NOW,
            "senderRole": "leader",
            "senderId": "leader-1",
            "command": "start",
            "taskId": "task-1",
        },
    }
    ed = StreamEventData.model_validate(raw)
    assert isinstance(ed.eventData, TaskCommand)
    assert ed.eventData.command == TaskCommandType.Start


def test_stream_event_data_discriminated_union_status_update():
    """eventData.type == 'task-status-update' → 还原为 TaskStatusUpdateEvent。"""
    raw = {
        "eventSeq": 3,
        "eventData": {
            "type": "task-status-update",
            "id": "ev-1",
            "sentAt": NOW,
            "senderRole": "partner",
            "senderId": "agent",
            "taskId": "task-1",
            "status": {"state": "working", "stateChangedAt": NOW},
        },
    }
    ed = StreamEventData.model_validate(raw)
    assert isinstance(ed.eventData, TaskStatusUpdateEvent)
    assert ed.eventData.taskId == "task-1"


def test_stream_event_data_discriminated_union_product_chunk():
    """eventData.type == 'product-chunk' → 还原为 ProductChunkEvent。"""
    raw = {
        "eventSeq": 4,
        "eventData": {
            "type": "product-chunk",
            "id": "ev-2",
            "sentAt": NOW,
            "senderRole": "partner",
            "senderId": "agent",
            "taskId": "task-1",
            "product": {
                "id": "prod-1",
                "dataItems": [{"type": "text", "text": "hello"}],
            },
            "append": True,
            "lastChunk": False,
        },
    }
    ed = StreamEventData.model_validate(raw)
    assert isinstance(ed.eventData, ProductChunkEvent)
    assert ed.eventData.taskId == "task-1"
    assert ed.eventData.append is True


# ---------------------------------------------------------------------------
# ReStreamCommandParams
# ---------------------------------------------------------------------------


def test_restream_params_parse():
    """lastEventSeq 传值或 None 均可构造。"""
    p1 = ReStreamCommandParams(lastEventSeq=5)
    assert p1.lastEventSeq == 5

    p2 = ReStreamCommandParams(lastEventSeq=None)
    assert p2.lastEventSeq is None

    p3 = ReStreamCommandParams()
    assert p3.lastEventSeq is None


# ---------------------------------------------------------------------------
# SSE 常量
# ---------------------------------------------------------------------------


def test_sse_constants_defined():
    """SSE_MEDIA_TYPE 与 SSE_HEADERS 均正确定义。"""
    assert SSE_MEDIA_TYPE == "text/event-stream"
    assert "Cache-Control" in SSE_HEADERS
    assert "Connection" in SSE_HEADERS


def test_stream_event_payload_type_alias():
    """StreamEventPayload 为联合类型别名，可在类型注解中使用。"""
    import typing
    # 能从 stream_model 直接导入，且是 Union 类型
    assert StreamEventPayload is not None
