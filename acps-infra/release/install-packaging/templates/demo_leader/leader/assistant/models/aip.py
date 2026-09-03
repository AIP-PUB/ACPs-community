"""
Leader Agent Platform - AIP 协议类型

直接复用 acps_sdk.aip SDK 的类型定义，避免重复定义。
此处仅提供：
1. SDK 类型的 re-export（统一导入入口）
2. Leader 侧特有的扩展类型（如 AipTaskSnapshot、AipMessageDraft）
3. 辅助函数
"""

from typing import Any

# =============================================================================
# 直接复用 SDK 类型
# =============================================================================
# 从 acps_sdk.aip SDK 导入核心类型，避免重复定义
from acps_sdk.aip.aip_base_model import (
    DataItem,
    DataItemBase,
    FileDataItem,
    GetCommandParams,
    Message,
    Product,
    StartCommandParams,
    StructuredDataItem,
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from pydantic import BaseModel, ConfigDict, Field

from .base import AgentAic, AipTaskId, IsoDateTimeString

# =============================================================================
# Leader 特有的扩展类型
# =============================================================================


class TaskStatusSnapshot(BaseModel):
 """
    AIP TaskStatus 的快照（Leader 侧缓存用）。

    与 SDK 的 TaskStatus 区别：
    - 字段使用 snake_case + alias（符合 Leader 代码风格）
    - 用于 Leader 内存态，不直接用于 AIP 通信
 """

    state: TaskState = Field(..., description="任务状态")
    state_changed_at: IsoDateTimeString | None = Field(
        default=None,
        alias="stateChangedAt",
        description="状态变化时间",
    )
    data_items: list[DataItem] | None = Field(
        default=None,
        alias="dataItems",
        description="状态附带的数据项",
    )

    model_config = ConfigDict(populate_by_name=True)


class AipTaskSnapshot(BaseModel):
 """
    AIP Task 的快照（Leader 侧缓存用）。

    用途：
    - 缓存 Partner 返回的 Task 状态
    - 调试和日志记录
 """

    id: AipTaskId = Field(..., description="AIP 任务 ID")
    session_id: str = Field(..., alias="sessionId", description="关联的 Session ID")
    status: TaskStatusSnapshot = Field(..., description="任务状态快照")
    last_state_changed_at: IsoDateTimeString | None = Field(
        default=None,
        alias="lastStateChangedAt",
        description="最近状态变化时间",
    )
    products: list[dict[str, Any]] | None = Field(
        default=None,
        description="Partner 的产出物列表",
    )

    model_config = ConfigDict(populate_by_name=True)


class AipMessageDraft(BaseModel):
 """
    AIP Message 的草稿（Leader 规划阶段产出）。

    用途：
    - 2.2 编排阶段产出的待发送消息列表
    - 与 SDK 的 Message 区别：无需填写 id/sentAt/senderId 等运行时字段

    发送时由 Leader 补充完整字段后转换为 SDK 的 Message。
 """

    task_id: AipTaskId = Field(
        ...,
        alias="taskId",
        description="AIP Task 关联 ID",
    )
    command_type: TaskCommandType = Field(
        ...,
        alias="commandType",
        description="AIP TaskCommandType",
    )
    data_items: list[DataItem] | None = Field(
        default=None,
        alias="dataItems",
        description="消息负载",
    )
    group_id: str | None = Field(
        default=None,
        alias="groupId",
        description="Group 模式的组 ID",
    )
    mentions: list[AgentAic] | None = Field(
        default=None,
        description="Group 模式的定向路由",
    )
    command_params: dict[str, Any] | None = Field(
        default=None,
        alias="commandParams",
        description="命令参数",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 辅助函数
# =============================================================================


def create_text_item(
    text: str,
    schema: str | None = None,
) -> TextDataItem:
 """创建文本类型的 DataItem。"""
    metadata = {"schema": schema} if schema else None
    return TextDataItem(text=text, metadata=metadata)


def create_structured_item(
    data: dict[str, Any],
    schema: str | None = None,
) -> StructuredDataItem:
 """创建结构化类型的 DataItem。"""
    metadata = {"schema": schema} if schema else None
    return StructuredDataItem(data=data, metadata=metadata)
