"""app/message/schema.py — Message 专属请求/响应/视图模型（spec §6.5.1）。

复用 app.core.amp_api_schema 通用件，定义 message 的 6 个端点模型。
全部 populate_by_name=True，camelCase alias 对外、snake_case 字段名对内。

数值精度约定（偏异 D-2，同 access）：
  - 原始/计数字段 → int
  - 聚合浮点字段（avgAckLatencyMs 等）→ float | None
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.amp_api_schema import AMPFilter, AMPPaginationRequest, AMPSortSpec, AMPTimeRange

# ── 请求模型 ──────────────────────────────────────────────────────────────────


class MessageQueryRequest(BaseModel):
    """events/query 请求基类（spec §6.5.3）。"""

    model_config = ConfigDict(populate_by_name=True)

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    filter: AMPFilter | None = None
    sort: list[AMPSortSpec] | None = None
    page: AMPPaginationRequest = Field(default_factory=AMPPaginationRequest)
    include_raw_log: bool = Field(default=False, alias="includeRawLog")


class MessageLifecycleQueryRequest(MessageQueryRequest):
    """lifecycles/query 扩展请求（spec §6.5.1）。"""

    min_receive_count: int | None = Field(default=None, alias="minReceiveCount")
    only_acked: bool = Field(default=False, alias="onlyAcked")
    only_unacked: bool = Field(default=False, alias="onlyUnacked")
    min_age: str | None = Field(default=None, alias="minAge")
    include_timeout: bool = Field(default=False, alias="includeTimeout")
    terminal_states: list[str] | None = Field(default=None, alias="terminalStates")


class MessageDeadletterQueryRequest(MessageQueryRequest):
    """deadletters/query 扩展请求。"""

    min_receive_count: int | None = Field(default=None, alias="minReceiveCount")


class MessageDestinationStateQueryRequest(MessageQueryRequest):
    """destinations/query 扩展请求（+groupBy）。"""

    group_by: list[str] | None = Field(default=None, alias="groupBy")


class MessageThroughputRequest(BaseModel):
    """destinations/throughput 请求（独立，不继承 MessageQueryRequest，spec §6.5.3）。"""

    model_config = ConfigDict(populate_by_name=True)

    time_range: AMPTimeRange | None = Field(default=None, alias="timeRange")
    system: str | None = None
    destination_name: str | None = Field(default=None, alias="destinationName")
    destination_kind: str | None = Field(default=None, alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    step: str | None = None


# ── 视图子模型 ────────────────────────────────────────────────────────────────


class MessageDestinationView(BaseModel):
    """目的地三元组视图（对应 MessageBody.destination 结构）。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    kind: str | None = None
    virtual_host: str | None = Field(default=None, alias="virtualHost")


class MessageRoutingView(BaseModel):
    """消息路由信息视图。"""

    model_config = ConfigDict(populate_by_name=True)

    key: str | None = None
    partition: str | None = None
    offset: int | None = None


class MessageSettlementView(BaseModel):
    """消息结算信息视图。"""

    model_config = ConfigDict(populate_by_name=True)

    latency_ms: int | None = Field(default=None, alias="latencyMs")
    reason: str | None = None


class MessageErrorView(BaseModel):
    """错误信息视图。"""

    model_config = ConfigDict(populate_by_name=True)

    code: str | None = None
    message: str | None = None


# ── 端点视图模型 ──────────────────────────────────────────────────────────────


class MessageEventView(BaseModel):
    """events/query 单事件视图（spec §6.5.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    log_id: str = Field(alias="logId")
    timestamp: str
    aic: str | None = None
    trace_id: str | None = Field(default=None, alias="traceId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    direction: str
    event_type: str = Field(alias="eventType")
    system: str
    destination_name: str = Field(alias="destinationName")
    destination_kind: str = Field(alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    subscription_name: str | None = Field(default=None, alias="subscriptionName")
    consumer_group_name: str | None = Field(default=None, alias="consumerGroupName")
    routing_key: str | None = Field(default=None, alias="routingKey")
    routing: MessageRoutingView | None = None
    message_id: str | None = Field(default=None, alias="messageId")
    lifecycle_key: str | None = Field(default=None, alias="lifecycleKey")
    payload_size_bytes: int | None = Field(default=None, alias="payloadSizeBytes")
    delivery_attempt: int | None = Field(default=None, alias="deliveryAttempt")
    settlement: MessageSettlementView | None = None
    dead_lettered: bool | None = Field(default=None, alias="deadLettered")
    dead_letter_reason: str | None = Field(default=None, alias="deadLetterReason")
    error: MessageErrorView | None = None
    attributes: dict[str, str] | None = None
    raw_log: str | None = Field(default=None, alias="rawLog")


class MessageLifecycleView(BaseModel):
    """lifecycles/query 生命周期视图（spec §6.5.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    lifecycle_key: str = Field(alias="lifecycleKey")
    message_id: str | None = Field(default=None, alias="messageId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    trace_id: str | None = Field(default=None, alias="traceId")
    system: str
    destination_name: str = Field(alias="destinationName")
    destination_kind: str = Field(alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    subscription_name: str | None = Field(default=None, alias="subscriptionName")
    consumer_group_name: str | None = Field(default=None, alias="consumerGroupName")
    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")
    dead_lettered_at: str | None = Field(default=None, alias="deadLetteredAt")
    producer_aics: list[str] = Field(default_factory=list, alias="producerAics")
    consumer_aics: list[str] = Field(default_factory=list, alias="consumerAics")
    send_count: int = Field(alias="sendCount")
    receive_count: int = Field(alias="receiveCount")
    max_delivery_attempt: int | None = Field(default=None, alias="maxDeliveryAttempt")
    terminal_state: str | None = Field(default=None, alias="terminalState")
    dead_lettered: bool = Field(alias="deadLettered")
    dead_letter_reason: str | None = Field(default=None, alias="deadLetterReason")
    duplicate_consumed: bool = Field(alias="duplicateConsumed")
    unacked: bool
    avg_ack_latency_ms: float | None = Field(default=None, alias="avgAckLatencyMs")


class MessageLifecycleDetailView(BaseModel):
    """lifecycles/{messageId} 裸资源视图（无 meta 字段，设计 §6.3）。"""

    model_config = ConfigDict(populate_by_name=True)

    lifecycle_key: str = Field(alias="lifecycleKey")
    message_id: str | None = Field(default=None, alias="messageId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    trace_id: str | None = Field(default=None, alias="traceId")
    system: str
    destination_name: str = Field(alias="destinationName")
    destination_kind: str = Field(alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    subscription_name: str | None = Field(default=None, alias="subscriptionName")
    consumer_group_name: str | None = Field(default=None, alias="consumerGroupName")
    first_seen_at: str = Field(alias="firstSeenAt")
    last_seen_at: str = Field(alias="lastSeenAt")
    dead_lettered_at: str | None = Field(default=None, alias="deadLetteredAt")
    producer_aics: list[str] = Field(default_factory=list, alias="producerAics")
    consumer_aics: list[str] = Field(default_factory=list, alias="consumerAics")
    send_count: int = Field(alias="sendCount")
    receive_count: int = Field(alias="receiveCount")
    max_delivery_attempt: int | None = Field(default=None, alias="maxDeliveryAttempt")
    terminal_state: str | None = Field(default=None, alias="terminalState")
    dead_lettered: bool = Field(alias="deadLettered")
    dead_letter_reason: str | None = Field(default=None, alias="deadLetterReason")
    duplicate_consumed: bool = Field(alias="duplicateConsumed")
    unacked: bool
    avg_ack_latency_ms: float | None = Field(default=None, alias="avgAckLatencyMs")


class MessageDestinationStateView(BaseModel):
    """destinations/query 目的地状态视图（spec §6.5.1）。

    Nullable 状态列注解为 int | None（broker 不支持时为 None，C-MESSAGE-QUERY-5）。
    """

    model_config = ConfigDict(populate_by_name=True)

    captured_at: str = Field(alias="capturedAt")
    system: str | None = None
    destination_name: str | None = Field(default=None, alias="destinationName")
    destination_kind: str | None = Field(default=None, alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    visible_messages: int | None = Field(default=None, alias="visibleMessages")
    inflight_messages: int | None = Field(default=None, alias="inflightMessages")
    delayed_messages: int | None = Field(default=None, alias="delayedMessages")
    dead_letter_messages: int | None = Field(default=None, alias="deadLetterMessages")
    oldest_message_age_seconds: int | None = Field(default=None, alias="oldestMessageAgeSeconds")
    active_consumers: int | None = Field(default=None, alias="activeConsumers")
    size_bytes: int | None = Field(default=None, alias="sizeBytes")


class MessageDeadLetterView(BaseModel):
    """deadletters/query 死信视图（spec §6.5.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    lifecycle_key: str = Field(alias="lifecycleKey")
    message_id: str | None = Field(default=None, alias="messageId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    trace_id: str | None = Field(default=None, alias="traceId")
    system: str
    destination_name: str = Field(alias="destinationName")
    destination_kind: str = Field(alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    dead_lettered_at: str | None = Field(default=None, alias="deadLetteredAt")
    dead_letter_reason: str | None = Field(default=None, alias="deadLetterReason")
    receive_count: int = Field(alias="receiveCount")
    max_delivery_attempt: int | None = Field(default=None, alias="maxDeliveryAttempt")
    producer_aics: list[str] = Field(default_factory=list, alias="producerAics")
    consumer_aics: list[str] = Field(default_factory=list, alias="consumerAics")


class MessageThroughputPoint(BaseModel):
    """destinations/throughput 单桶点视图（spec §6.5.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    bucket: datetime
    produced_count: int = Field(alias="producedCount")
    consumed_count: int = Field(alias="consumedCount")
    ack_count: int | None = Field(default=None, alias="ackCount")
    nack_count: int | None = Field(default=None, alias="nackCount")
    reject_count: int | None = Field(default=None, alias="rejectCount")
    timeout_count: int | None = Field(default=None, alias="timeoutCount")
    dead_letter_count: int | None = Field(default=None, alias="deadLetterCount")
    retry_count: int | None = Field(default=None, alias="retryCount")
    avg_ack_latency_ms: float | None = Field(default=None, alias="avgAckLatencyMs")


class MessageThroughputSeries(BaseModel):
    """destinations/throughput 吞吐时序（裸资源，无 meta，设计 §6.6）。"""

    model_config = ConfigDict(populate_by_name=True)

    system: str
    destination_name: str = Field(alias="destinationName")
    destination_kind: str | None = Field(default=None, alias="destinationKind")
    virtual_host: str | None = Field(default=None, alias="virtualHost")
    points: list[MessageThroughputPoint] = Field(default_factory=list)
