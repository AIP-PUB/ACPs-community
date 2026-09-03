"""
AIP v2 流式传输数据模型定义

本模块定义了 AIP v2 协议中流式传输方式的数据对象，包括：
- SSE 事件：TaskStatusUpdateEvent, ProductChunkEvent (均继承自 Message)
- 流式请求/响应：StreamRequest, StreamResponse
- 类型别名与 SSE 常量：StreamEventPayload, SSE_MEDIA_TYPE, SSE_HEADERS
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from .aip_base_model import Message, Product, TaskCommand, TaskResult, TaskStatus
from .aip_rpc_model import JSONRPCRequest, JSONRPCResponse

# ---------------------------------------------------------------------------
# SSE 常量
# ---------------------------------------------------------------------------

SSE_MEDIA_TYPE: str = "text/event-stream"
SSE_HEADERS: dict[str, str] = {"Cache-Control": "no-cache", "Connection": "keep-alive"}


# ---------------------------------------------------------------------------
# SSE 事件类型
# ---------------------------------------------------------------------------


class TaskStatusUpdateEvent(Message):
    """
    AIP v2 任务状态更新事件

    继承自 Message，用于 SSE 流式传输中的任务状态更新通知。
    """

    type: Literal["task-status-update"] = "task-status-update"
    taskId: str
    status: TaskStatus


class ProductChunkEvent(Message):
    """
    AIP v2 产出物分块事件

    继承自 Message，用于 SSE 流式传输中的产出物分块传输。
    """

    type: Literal["product-chunk"] = "product-chunk"
    taskId: str
    product: Product
    append: bool
    lastChunk: bool


# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

# SSE 事件负载：eventData 字段可能包含的所有类型
StreamEventPayload = Union[TaskResult, TaskCommand, TaskStatusUpdateEvent, ProductChunkEvent]


# ---------------------------------------------------------------------------
# 流式请求 / 响应
# ---------------------------------------------------------------------------


class StreamRequestParams(BaseModel):
    """流式请求参数"""

    message: TaskCommand  # AIP v2: 使用 TaskCommand 类型


class StreamRequest(JSONRPCRequest):
    """AIP 流式请求"""

    method: Literal["stream"] = "stream"
    params: StreamRequestParams


class StreamEventData(BaseModel):
    """流式事件数据（单条 SSE 事件的载体）"""

    eventSeq: int
    # 判别联合：pydantic v2 通过 type 字段高效选择具体类型
    eventData: Annotated[
        Union[TaskResult, TaskCommand, TaskStatusUpdateEvent, ProductChunkEvent],
        Field(discriminator="type"),
    ]


class StreamResponse(JSONRPCResponse):
    """AIP 流式响应

    result 与 error 互斥，二者至多其一存在。
    - 正常事件帧：result 有值，error 为 None
    - 错误帧（如 re-stream 不可续传）：error 有值，result 为 None
    """

    result: Optional[StreamEventData] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 重连参数
# ---------------------------------------------------------------------------


class ReStreamCommandParams(BaseModel):
    """重连流式传输命令参数"""

    lastEventSeq: Optional[int] = None
