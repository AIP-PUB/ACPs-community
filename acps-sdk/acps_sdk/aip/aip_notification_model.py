"""
AIP v2 通知方式数据模型定义

本模块定义了 AIP v2 协议中异步通知方式（Notification Style）的全套数据对象：
- 通知配置：NotificationConfig
- 四类 JSON-RPC 请求/响应：set / delete / get / start
- 常量：NOTIFICATION_TOKEN_HEADER
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel

from .aip_base_model import TaskCommand, TaskState
from .aip_rpc_model import JSONRPCRequest, JSONRPCResponse

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

NOTIFICATION_TOKEN_HEADER: str = "X-ACPs-AIP-Notification-Token"
"""HTTP 请求头名称：通知回调时用于 token 校验。"""


# ---------------------------------------------------------------------------
# 通知配置
# ---------------------------------------------------------------------------


class NotificationConfig(BaseModel):
    """通知配置

    由 Leader 通过 notification/set 注册，Partner 存储后用于推送回调。
    """

    id: Optional[str] = None          # Partner 侧生成；Leader 发送时可不填
    url: str                           # 回调 URL（Leader 端接收通知的端点）
    token: str                         # 用于签名/验证的共享 token
    taskId: str                        # 关联的任务 ID


# ---------------------------------------------------------------------------
# notification/set
# ---------------------------------------------------------------------------


class NotificationRequest(JSONRPCRequest):
    """AIP notification/set 请求"""

    method: Literal["notification/set"] = "notification/set"
    params: NotificationConfig


class NotificationResponse(JSONRPCResponse):
    """AIP notification/set 响应（result 包含完整配置，含服务端生成的 id）"""

    result: Optional[NotificationConfig] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# notification/delete & notification/get 公共参数
# ---------------------------------------------------------------------------


class NotificationIdParams(BaseModel):
    """notification/delete 与 notification/get 共用的请求参数"""

    taskId: str
    notificationConfigId: Optional[str] = None  # 不填则操作该 task 全部配置


# ---------------------------------------------------------------------------
# notification/delete
# ---------------------------------------------------------------------------


class NotificationDeleteRequest(JSONRPCRequest):
    """AIP notification/delete 请求"""

    method: Literal["notification/delete"] = "notification/delete"
    params: NotificationIdParams


class NotificationDeleteResult(BaseModel):
    """notification/delete 成功结果"""

    success: Literal[True] = True


class NotificationDeleteResponse(JSONRPCResponse):
    """AIP notification/delete 响应"""

    result: Optional[NotificationDeleteResult] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# notification/get
# ---------------------------------------------------------------------------


class NotificationGetRequest(JSONRPCRequest):
    """AIP notification/get 请求"""

    method: Literal["notification/get"] = "notification/get"
    params: NotificationIdParams


class NotificationGetResponse(JSONRPCResponse):
    """AIP notification/get 响应（result 为配置列表）"""

    result: Optional[List[NotificationConfig]] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# notification/start
# ---------------------------------------------------------------------------


class NotificationStartParams(BaseModel):
    """notification/start 命令参数（嵌入在 TaskCommand.commandParams 中）"""

    notificationConfigId: str
    notifyOnStates: Optional[List[TaskState]] = None


class NotificationStartRequestParams(BaseModel):
    """notification/start 的 JSON-RPC params 字段"""

    message: TaskCommand   # 使用 message 字段（与 stream 保持一致）


class NotificationStartRequest(JSONRPCRequest):
    """AIP notification/start 请求"""

    method: Literal["notification/start"] = "notification/start"
    params: NotificationStartRequestParams
