"""
Leader Agent Platform - API Schemas

本模块定义 Leader HTTP API 的请求/响应模型。

端点概览：
- POST /submit: 提交用户输入
- GET /result: 获取当前 Session 状态
- GET /log: 获取事件日志
- POST /cancel: 取消任务
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    ActiveTask,
    ActiveTaskId,
    ActiveTaskStatus,
    AgentAic,
    ClientRequestId,
    DialogContext,
    EventLogEntry,
    ExecutionMode,
    IsoDateTimeString,
    PartnerRuntimeState,
    ScenarioBrief,
    SessionId,
    UserResult,
)

# =============================================================================
# 通用响应结构
# =============================================================================

T = TypeVar("T")


class CommonError(BaseModel):
    """
    通用错误结构。

    说明：
    - code 采用 6 位整数：HTTP 状态码(3位) + 业务错误码(3位)
    - data 推荐返回结构化细节，便于 UI/测试定位问题
    """

    code: int = Field(..., description="错误码（6位整数）")
    message: str = Field(..., description="错误消息（面向开发者）")
    data: Any | None = Field(default=None, description="结构化错误详情")


class CommonResponse(BaseModel, Generic[T]):
    """
    通用响应结构。

    约束：result 与 error 互斥。
    """

    result: T | None = Field(default=None, description="成功时的返回数据")
    error: CommonError | None = Field(default=None, description="失败时的错误信息")


# =============================================================================
# /submit 端点
# =============================================================================


class SubmitRequest(BaseModel):
    """
    /submit 请求体。

    说明：
    - clientRequestId：用于幂等/重放保护（对应错误码 409003）
    - activeTaskId：用于"用户侧并发提交"的乐观校验（对应错误码 409002）
    - mode：创建新 Session 时指定执行模式，默认 direct_rpc；已有 Session 时忽略此字段
    """

    session_id: SessionId | None = Field(
        default=None,
        alias="sessionId",
        description="会话 ID（不传则创建新会话）",
    )
    mode: ExecutionMode = Field(
        default=ExecutionMode.DIRECT_RPC,
        description="执行模式（创建新 Session 时指定，已有 Session 时忽略）",
    )
    client_request_id: ClientRequestId = Field(
        ...,
        alias="clientRequestId",
        description="客户端请求去重 ID",
    )
    query: str = Field(..., description="用户输入文本")
    active_task_id: ActiveTaskId | None = Field(
        default=None,
        alias="activeTaskId",
        description="乐观校验用的任务 ID",
    )
    user_id: str | None = Field(
        default=None,
        alias="userId",
        description="可选用户标识",
    )

    model_config = ConfigDict(populate_by_name=True)


class SubmitResult(BaseModel):
    """
    /submit 成功返回的最小结果。

    说明：submit 是异步受理，返回"本次提交落在了哪个 session/activeTask"。
    """

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="会话 ID",
    )
    mode: ExecutionMode = Field(..., description="执行模式")
    active_task_id: ActiveTaskId = Field(
        ...,
        alias="activeTaskId",
        description="活跃任务 ID",
    )
    accepted_at: IsoDateTimeString = Field(
        ...,
        alias="acceptedAt",
        description="受理时间",
    )
    external_status: ActiveTaskStatus = Field(
        ...,
        alias="externalStatus",
        description="activeTask 的对外状态",
    )

    model_config = ConfigDict(populate_by_name=True)


class SubmitResponse(CommonResponse[SubmitResult]):
    """
    /submit 响应体。
    """

    pass


# =============================================================================
# /result 端点
# =============================================================================


class ScenarioRuntimeView(BaseModel):
    """
    场景运行时视图（不包含 prompts 等内部配置）。
    """

    id: str = Field(..., description="场景 ID")
    kind: str = Field(..., description="场景类型：base / expert")
    version: str | None = Field(default=None, description="场景版本")
    loaded_at: IsoDateTimeString = Field(
        ...,
        alias="loadedAt",
        description="场景加载时间",
    )
    source_path: str | None = Field(
        default=None,
        alias="sourcePath",
        description="配置来源路径",
    )
    config_digest: str | None = Field(
        default=None,
        alias="configDigest",
        description="配置内容摘要",
    )

    model_config = ConfigDict(populate_by_name=True)


class GroupRoutingInfoView(BaseModel):
    """
    Group 模式路由信息视图（脱敏版）。
    """

    group_id: str = Field(..., alias="groupId", description="Group ID")
    provider: str = Field(..., description="MQ 协议/实现标识")
    exchange: str | None = Field(default=None, description="RabbitMQ exchange")
    routing_key: str | None = Field(
        default=None,
        alias="routingKey",
        description="RabbitMQ routing key",
    )
    queue: str | None = Field(default=None, description="RabbitMQ queue")
    topic: str | None = Field(default=None, description="Kafka topic")

    model_config = ConfigDict(populate_by_name=True)


class LeaderResult(BaseModel):
    """
    /result 返回的 Session 视图（不包含 eventLog）。

    说明：
    - 前端交互主要依赖 activeTask/userResult/partner 状态
    - 配置原文（prompts/domainMeta）不在此暴露
    """

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="会话 ID",
    )
    mode: ExecutionMode = Field(..., description="执行模式")
    user_id: str | None = Field(
        default=None,
        alias="userId",
        description="用户标识",
    )
    created_at: IsoDateTimeString = Field(
        ...,
        alias="createdAt",
        description="创建时间",
    )
    updated_at: IsoDateTimeString = Field(
        ...,
        alias="updatedAt",
        description="更新时间",
    )
    touched_at: IsoDateTimeString = Field(
        ...,
        alias="touchedAt",
        description="最后触碰时间",
    )
    ttl_seconds: int = Field(
        ...,
        alias="ttlSeconds",
        description="TTL 秒数",
    )
    expires_at: IsoDateTimeString = Field(
        ...,
        alias="expiresAt",
        description="到期时间",
    )
    closed: bool | None = Field(default=None, description="是否已关闭")
    closed_at: IsoDateTimeString | None = Field(
        default=None,
        alias="closedAt",
        description="关闭时间",
    )
    closed_reason: str | None = Field(
        default=None,
        alias="closedReason",
        description="关闭原因",
    )
    group_id: str | None = Field(
        default=None,
        alias="groupId",
        description="Group 模式的 groupId",
    )
    group_routing: GroupRoutingInfoView | None = Field(
        default=None,
        alias="groupRouting",
        description="Group 模式路由信息",
    )
    base_scenario: ScenarioRuntimeView = Field(
        ...,
        alias="baseScenario",
        description="基础场景",
    )
    expert_scenario: ScenarioRuntimeView | None = Field(
        default=None,
        alias="expertScenario",
        description="当前专业场景",
    )
    scenario_briefs: list[ScenarioBrief] = Field(
        default_factory=list,
        alias="scenarioBriefs",
        description="已注册的专业场景列表",
    )
    active_task: ActiveTask | None = Field(
        default=None,
        alias="activeTask",
        description="当前活跃任务",
    )
    partners: dict[AgentAic, PartnerRuntimeState] = Field(
        default_factory=dict,
        description="Partner 运行时状态",
    )
    user_context: dict[str, Any] = Field(
        default_factory=dict,
        alias="userContext",
        description="用户上下文",
    )
    dialog_context: DialogContext | None = Field(
        default=None,
        alias="dialogContext",
        description="对话上下文",
    )
    user_result: UserResult = Field(
        ...,
        alias="userResult",
        description="用户可见输出",
    )

    model_config = ConfigDict(populate_by_name=True)


class ResultResponse(CommonResponse[LeaderResult]):
    """
    /result 响应体。
    """

    pass


class GroupRuntimeMemberView(BaseModel):
    """群组运行态中的成员视图。"""

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="群组成员 AIC",
    )
    invitation_route: str | None = Field(
        default=None,
        alias="invitationRoute",
        description="Leader 最近一次为该成员选择的邀请路由（inbox / rpc）",
    )
    connected: bool = Field(..., description="是否仍连接到群组 MQ")
    muted: bool = Field(..., description="是否处于静音状态")
    connection_name: str | None = Field(
        default=None,
        alias="connectionName",
        description="RabbitMQ 连接名",
    )
    vhost: str | None = Field(default=None, description="RabbitMQ vhost")
    node_name: str | None = Field(
        default=None,
        alias="nodeName",
        description="RabbitMQ node 名称",
    )
    queue_name: str | None = Field(
        default=None,
        alias="queueName",
        description="成员群组队列",
    )
    joined_at: IsoDateTimeString | None = Field(
        default=None,
        alias="joinedAt",
        description="加入群组时间",
    )

    model_config = ConfigDict(populate_by_name=True)


class GroupRuntimeView(BaseModel):
    """Leader 暴露的群组运行态视图。"""

    session_id: SessionId = Field(..., alias="sessionId", description="会话 ID")
    group_id: str = Field(..., alias="groupId", description="群组 ID")
    leader_aic: AgentAic = Field(..., alias="leaderAic", description="Leader AIC")
    state: str = Field(..., description="群组客户端状态")
    total_members: int = Field(..., alias="totalMembers", description="当前成员总数")
    connected_members: int = Field(
        ...,
        alias="connectedMembers",
        description="当前处于 connected=true 的成员数",
    )
    pending_invitations: list[AgentAic] = Field(
        default_factory=list,
        alias="pendingInvitations",
        description="仍在等待确认的邀请对象列表",
    )
    members: list[GroupRuntimeMemberView] = Field(
        default_factory=list,
        description="群组成员运行态明细",
    )

    model_config = ConfigDict(populate_by_name=True)


class GroupRuntimeResponse(CommonResponse[GroupRuntimeView]):
    """群组运行态接口响应体。"""

    pass


class GroupMemberActionResult(BaseModel):
    """群组成员动作响应。"""

    session_id: SessionId = Field(..., alias="sessionId", description="会话 ID")
    group_id: str = Field(..., alias="groupId", description="群组 ID")
    partner_aic: AgentAic = Field(..., alias="partnerAic", description="目标 Partner AIC")
    action: str = Field(..., description="执行动作")
    accepted_at: IsoDateTimeString = Field(
        ...,
        alias="acceptedAt",
        description="Leader 接受该动作的时间",
    )
    queue_deleted: bool | None = Field(
        default=None,
        alias="queueDeleted",
        description="强制移除时是否删除了成员队列",
    )

    model_config = ConfigDict(populate_by_name=True)


class GroupMemberActionResponse(CommonResponse[GroupMemberActionResult]):
    """群组成员动作接口响应体。"""

    pass


# =============================================================================
# /log 端点
# =============================================================================


class LogRequest(BaseModel):
    """
    /log 请求参数（Query Parameters）。
    """

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="会话 ID",
    )
    cursor: str | None = Field(
        default=None,
        description="游标（不传表示从头开始）",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="返回条目数上限",
    )

    model_config = ConfigDict(populate_by_name=True)


class LogResult(BaseModel):
    """
    /log 成功返回的结果。
    """

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="会话 ID",
    )
    items: list[EventLogEntry] = Field(
        default_factory=list,
        description="事件日志条目列表",
    )
    next_cursor: str | None = Field(
        default=None,
        alias="nextCursor",
        description="下一页游标",
    )
    has_more: bool | None = Field(
        default=None,
        alias="hasMore",
        description="是否还有更多条目",
    )

    model_config = ConfigDict(populate_by_name=True)


class LogResponse(CommonResponse[LogResult]):
    """
    /log 响应体。
    """

    pass


# =============================================================================
# /cancel 端点
# =============================================================================


class CancelRequest(BaseModel):
    """
    /cancel 请求体。
    """

    session_id: SessionId | None = Field(
        default=None,
        alias="sessionId",
        description="要取消的会话 ID",
    )
    active_task_id: ActiveTaskId | None = Field(
        default=None,
        alias="activeTaskId",
        description="乐观校验用的 activeTaskId",
    )
    client_request_id: ClientRequestId | None = Field(
        default=None,
        alias="clientRequestId",
        description="客户端请求去重 ID（推荐）",
    )
    delete_session: bool = Field(
        default=False,
        alias="deleteSession",
        description="是否在取消后同步删除 Session 并触发群组解散清理",
    )

    model_config = ConfigDict(populate_by_name=True)


class CancelResult(BaseModel):
    """
    /cancel 成功返回的结果。
    """

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="被取消的会话 ID",
    )
    success: bool = Field(..., description="是否成功取消")
    cancelled_tasks: list[ActiveTaskId] = Field(
        default_factory=list,
        alias="cancelledTasks",
        description="被取消的任务 ID 列表",
    )
    session_deleted: bool = Field(
        ...,
        alias="sessionDeleted",
        description="是否同步删除了 Session",
    )
    message: str = Field(..., description="操作结果说明")
    canceled_at: IsoDateTimeString = Field(
        ...,
        alias="canceledAt",
        description="取消时间",
    )

    model_config = ConfigDict(populate_by_name=True)


class CancelResponse(CommonResponse[CancelResult]):
    """
    /cancel 响应体。
    """

    pass


# =============================================================================
# /stream-token 端点
# =============================================================================


class StreamTokenResult(BaseModel):
    """短期 stream token 响应。"""

    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="绑定的会话 ID",
    )
    stream_token: str = Field(
        ...,
        alias="streamToken",
        description="短期 stream token",
    )
    expires_at: IsoDateTimeString = Field(
        ...,
        alias="expiresAt",
        description="失效时间",
    )

    model_config = ConfigDict(populate_by_name=True)


class StreamTokenResponse(CommonResponse[StreamTokenResult]):
    """stream token 接口响应体。"""

    pass
