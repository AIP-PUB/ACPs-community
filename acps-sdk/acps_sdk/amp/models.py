"""AMP (Agent Monitoring Protocol) 全量日志数据模型。

遵循 AMP Spec v02.00 §5 的完整定义，覆盖六种日志类型。

设计原则：
- 接收侧（monitor-server 等消费端）使用泛型 LogRecord（body: dict[str, Any]）
- 发射侧（Agent / SDK 客户端）使用各日志类型的强类型 XxxBody + XxxLogRecord
- 顶层字段无 alias → 保持 snake_case（schema_version / log_id / trace_id …）
- body 子字段带 camelCase alias → by_alias=True 序列化为 camelCase（与 JSON 线格式一致）

序列化约定（发射侧）：
    record.model_dump(mode="json", by_alias=True, exclude_none=True)

日志类型：
    heartbeat / metrics / access / message / audit / system
    系统日志（system）body 为自由格式，直接使用 LogRecord 即可。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── 共享：完整性签名 ──────────────────────────────────────────────────────────


class LogRecordIntegrity(BaseModel):
    """AMP 数字签名信息（Spec §5.1.2 integrity 字段）。

    接收侧与发射侧共用。
    算法推荐优先级：EdDSA > ES256 > RS256（见 Spec §5.1.2）。
    """

    alg: str = Field(description="签名算法：'EdDSA'（首选）/ 'ES256' / 'RS256'")
    kid: str = Field(
        description=(
            "签名证书的 X.509 序列号（大写十六进制字符串，如 '3F8A91B2C4D5E6F7'）。"
            "验签方通过 kid 直接向 CA 服务查询对应证书的公钥（GET /acps-atr-v2/ca/keys/{kid}）。"
            "密钥轮转时新序列号自动区分版本，无需额外版本管理（Spec §5.1.2）。"
        )
    )
    sig: str = Field(description="Base64url 编码的数字签名（不含填充 '='）")


# ─── 接收侧通用 LogRecord ─────────────────────────────────────────────────────


class LogRecord(BaseModel):
    """AMP 顶层日志记录（接收 / 消费侧通用模型）。

    从 Kafka / 日志文件反序列化后，按 log_type 分派给各处理器。
    body 保留为 dict，由处理器自行解析。
    timestamp 保留原始字符串，不做 datetime 转换（链哈希依赖字节级一致性）。
    """

    schema_version: str = Field(description="Schema 版本，如 '1.0'")
    timestamp: str = Field(description="ISO 8601 带时区时间戳（事件产生时间）")
    aic: str = Field(description="Agent Instance Context 标识符")
    log_type: str = Field(
        description="heartbeat / metrics / access / message / audit / system"
    )
    log_id: str | None = Field(
        default=None,
        description=(
            "单事件幂等键（Spec §5.1.3）。源端一次性生成，推荐 UUIDv7；"
            "下游 Forwarder / Writer 全程不得重新生成。"
            "源端未提供时，Writer 须按 spec §5.1.3 的 JCS 内容哈希兜底。"
        ),
    )
    observed_timestamp: str | None = Field(
        default=None, description="观测时间（监控系统接收时间），ISO 8601"
    )
    trace_id: str | None = Field(default=None, description="分布式追踪链路 ID")
    span_id: str | None = Field(default=None, description="当前 Span ID")
    parent_span_id: str | None = Field(default=None, description="父 Span ID")
    correlation_id: str | None = Field(default=None, description="业务相关 ID")
    severity_text: str | None = Field(
        default=None, description="日志级别文本，如 'INFO' / 'ERROR'"
    )
    severity_number: int | None = Field(
        default=None,
        description="规范化数值级别（1-24，见 Spec §5.1.2 SeverityNumber）",
    )
    body: dict[str, Any] | None = Field(
        default=None, description="日志 body，各日志类型格式不同"
    )
    resource: dict[str, Any] | None = Field(
        default=None, description="日志来源描述（服务、主机、容器等）"
    )
    attributes: dict[str, Any] | None = Field(
        default=None, description="附加键值对属性"
    )
    integrity: LogRecordIntegrity | None = Field(
        default=None, description="数字签名信息（审计日志必须携带）"
    )


# ─── 心跳日志（Spec §5.2）─────────────────────────────────────────────────────


class HeartbeatBody(BaseModel):
    """心跳日志 body（Spec §5.2 HeartbeatBody）。"""

    model_config = ConfigDict(populate_by_name=True)

    uptime_seconds: float | None = Field(
        default=None,
        alias="uptimeSeconds",
        description="系统自启动起的累计运行时间（秒）",
    )


class HeartbeatLogRecord(BaseModel):
    """完整的心跳 LogRecord（发射侧强类型）。"""

    schema_version: str = "1.0"
    log_id: str = Field(description="全局唯一日志 ID，推荐 UUID v7")
    log_type: str = "heartbeat"
    timestamp: str = Field(description="ISO 8601 带时区时间戳")
    aic: str
    body: HeartbeatBody
    integrity: LogRecordIntegrity | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    severity_text: str | None = None
    severity_number: int | None = None

    @staticmethod
    def new_log_id() -> str:
        """生成新的 log_id（Python 3.14+ uuid7，旧版本 fallback 到 uuid4）。"""
        try:
            return str(uuid.uuid7())  # type: ignore[attr-defined]
        except AttributeError:
            return str(uuid.uuid4())


# ─── 指标日志（Spec §5.3）─────────────────────────────────────────────────────


class LoadMetrics(BaseModel):
    """即时负载指标（Spec §5.3 LoadMetrics）。"""

    model_config = ConfigDict(populate_by_name=True)

    active_tasks: int = Field(alias="activeTasks", description="当前执行中的任务数")
    queued_tasks: int = Field(alias="queuedTasks", description="队列中等待的任务数")
    max_active_tasks: int | None = Field(
        default=None, alias="maxActiveTasks", description="系统最大并发任务数上限"
    )
    max_queued_tasks: int | None = Field(
        default=None, alias="maxQueuedTasks", description="系统最大排队长度上限"
    )
    cpu_usage: float | None = Field(
        default=None, alias="cpuUsage", description="CPU 使用率，百分比 0-100"
    )
    memory_usage: float | None = Field(
        default=None, alias="memoryUsage", description="内存使用率，百分比 0-100"
    )
    disk_usage: float | None = Field(
        default=None, alias="diskUsage", description="磁盘使用率，百分比 0-100"
    )
    network_in_usage: float | None = Field(
        default=None, alias="networkInUsage", description="入站网络带宽使用率 0-100"
    )
    network_out_usage: float | None = Field(
        default=None, alias="networkOutUsage", description="出站网络带宽使用率 0-100"
    )


class WindowMetrics(BaseModel):
    """滑动窗口汇总指标（Spec §5.3 WindowMetrics）。"""

    model_config = ConfigDict(populate_by_name=True)

    window: str = Field(description="ISO 8601 Duration，如 'PT5M'（5分钟窗口）")
    success_rate: float = Field(alias="successRate", description="成功率，百分比 0-100")
    request_total: int | None = Field(
        default=None, alias="requestTotal", description="窗口内总请求数"
    )
    request_per_second: float | None = Field(
        default=None, alias="requestPerSecond", description="平均 RPS"
    )
    avg_throughput_mbps: float | None = Field(
        default=None, alias="avgThroughputMBps", description="平均吞吐量（MB/s）"
    )
    peak_throughput_mbps: float | None = Field(
        default=None, alias="peakThroughputMBps", description="峰值吞吐量（MB/s）"
    )
    avg_latency_ms: float | None = Field(
        default=None, alias="avgLatencyMs", description="平均请求时延（ms）"
    )
    p99_latency_ms: float | None = Field(
        default=None, alias="p99LatencyMs", description="P99 时延（ms）"
    )
    p95_latency_ms: float | None = Field(
        default=None, alias="p95LatencyMs", description="P95 时延（ms）"
    )
    p90_latency_ms: float | None = Field(
        default=None, alias="p90LatencyMs", description="P90 时延（ms）"
    )
    p80_latency_ms: float | None = Field(
        default=None, alias="p80LatencyMs", description="P80 时延（ms）"
    )
    p75_latency_ms: float | None = Field(
        default=None, alias="p75LatencyMs", description="P75 时延（ms）"
    )
    p50_latency_ms: float | None = Field(
        default=None, alias="p50LatencyMs", description="P50 时延（ms）"
    )


class MetricsBody(BaseModel):
    """指标日志 body（Spec §5.3 MetricsBody）。"""

    model_config = ConfigDict(populate_by_name=True)

    uptime_seconds: float | None = Field(
        default=None,
        alias="uptimeSeconds",
        description="系统自启动起的累计运行时间（秒）",
    )
    load_metrics: LoadMetrics | None = Field(
        default=None, alias="loadMetrics", description="即时负载指标"
    )
    window_metrics: list[WindowMetrics] | None = Field(
        default=None, alias="windowMetrics", description="多窗口汇总指标列表"
    )


class MetricsLogRecord(BaseModel):
    """完整的指标 LogRecord（发射侧强类型）。"""

    schema_version: str = "1.0"
    log_id: str
    log_type: str = "metrics"
    timestamp: str
    aic: str
    body: MetricsBody
    resource: dict[str, Any] | None = Field(
        default=None,
        description="日志来源描述（service.name / service.namespace / deployment.environment.name）",
    )
    integrity: LogRecordIntegrity | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    severity_text: str | None = None
    severity_number: int | None = None

    @staticmethod
    def new_log_id() -> str:
        try:
            return str(uuid.uuid7())  # type: ignore[attr-defined]
        except AttributeError:
            return str(uuid.uuid4())


# ─── 访问日志（Spec §5.4）─────────────────────────────────────────────────────


class ErrorInfo(BaseModel):
    """通用错误信息（Spec §5.1.2 ErrorInfo，AccessLog / MessageLog 共用）。"""

    model_config = ConfigDict(populate_by_name=True)

    code: int | str | None = Field(
        default=None, description="错误代码，如 404 / 'AUTH_FAILED'"
    )
    message: str | None = Field(default=None, description="错误消息")
    data: Any | None = Field(default=None, description="额外错误数据")
    stack_trace: str | None = Field(
        default=None, alias="stackTrace", description="堆栈追踪"
    )


class AccessRequest(BaseModel):
    """HTTP / RPC 请求信息（Spec §5.4 AccessBody.request）。"""

    model_config = ConfigDict(populate_by_name=True)

    method: str | None = Field(
        default=None, description="HTTP 动词或 RPC 方法名，如 'GET' / 'GetUser'"
    )
    url: str | None = Field(default=None, description="HTTP Path 或 RPC 服务全限定名")
    route: str | None = Field(
        default=None,
        description=(
            "归一化路由模板（Spec §5.4.1），稳定 endpoint 聚合维度，与 method 组合；"
            "源端缺省时由写入侧归一化 url 推导"
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None, description="请求头或 RPC Metadata"
    )
    body_size_bytes: int | None = Field(
        default=None, alias="bodySizeBytes", description="请求体大小（字节）"
    )


class AccessResponse(BaseModel):
    """HTTP / RPC 响应信息（Spec §5.4 AccessBody.response）。"""

    model_config = ConfigDict(populate_by_name=True)

    status_code: int | None = Field(
        default=None, alias="statusCode", description="HTTP 状态码或 gRPC 状态码"
    )
    headers: dict[str, str] | None = Field(
        default=None, description="响应头或 RPC Metadata"
    )
    body_size_bytes: int | None = Field(
        default=None, alias="bodySizeBytes", description="响应体大小（字节）"
    )


class AccessParticipant(BaseModel):
    """访问日志中的调用方或被调用方（Spec §5.4 caller / callee）。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str | None = Field(default=None, description="Agent Instance Context 标识符")
    service_name: str | None = Field(
        default=None, alias="serviceName", description="服务名称，如 'order-service'"
    )
    ip: str | None = Field(default=None, description="IP 地址")


class AccessBody(BaseModel):
    """访问日志 body（Spec §5.4 AccessBody）。

    兼容 HTTP 和 RPC（gRPC / Dubbo 等）场景。
    """

    model_config = ConfigDict(populate_by_name=True)

    duration_ms: float | None = Field(
        default=None, alias="durationMs", description="请求总耗时（毫秒）"
    )
    request: AccessRequest | None = Field(default=None, description="请求详情")
    response: AccessResponse | None = Field(default=None, description="响应详情")
    caller: AccessParticipant | None = Field(
        default=None, description="调用方信息（发起请求的服务）"
    )
    callee: AccessParticipant | None = Field(
        default=None, description="被调用方信息（处理请求的服务）"
    )
    error: ErrorInfo | None = Field(default=None, description="错误信息（如有）")


class AccessLogRecord(BaseModel):
    """完整的访问 LogRecord（发射侧强类型）。"""

    schema_version: str = "1.0"
    log_id: str
    log_type: str = "access"
    timestamp: str
    aic: str
    body: AccessBody
    integrity: LogRecordIntegrity | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    severity_text: str | None = None
    severity_number: int | None = None

    @staticmethod
    def new_log_id() -> str:
        try:
            return str(uuid.uuid7())  # type: ignore[attr-defined]
        except AttributeError:
            return str(uuid.uuid4())


# ─── 消息日志（Spec §5.5）─────────────────────────────────────────────────────


class MessageDestination(BaseModel):
    """消息目的地（Spec §5.5 MessageBody.destination）。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="Topic / Queue / Exchange 名称")
    kind: Literal["topic", "queue", "exchange"] | None = Field(
        default=None, description="目的地类型"
    )
    virtual_host: str | None = Field(
        default=None, alias="virtualHost", description="虚拟主机（RabbitMQ），默认 '/'"
    )


class MessageRouting(BaseModel):
    """消息路由信息（Spec §5.5 MessageBody.routing）。"""

    model_config = ConfigDict(populate_by_name=True)

    key: str | None = Field(
        default=None, description="路由键（RabbitMQ）或消息 Key（Kafka）"
    )
    partition: str | None = Field(
        default=None,
        description=(
            "分区标识，字符串类型以兼容非 Kafka 系统，"
            "并区分'无分区概念'（字段缺省）与'0 号分区'（值为 '0'）"
        ),
    )
    offset: int | None = Field(default=None, description="消息偏移量（Kafka）")


class MessageSettlement(BaseModel):
    """结算补充信息（Spec §5.5 MessageBody.settlement）。

    结算"结果"由 MessageBody.event_type 表达，此处仅补充耗时与原因。
    仅在 event_type 为 ack / nack / reject / timeout / dead_letter 的事件上填写。
    """

    model_config = ConfigDict(populate_by_name=True)

    latency_ms: float | None = Field(
        default=None,
        alias="latencyMs",
        description="从接收到本次结算的耗时（ms）",
    )
    reason: str | None = Field(
        default=None,
        description="结算原因（nack / reject / dead_letter / timeout 的具体原因）",
    )


#: 向后兼容别名；旧代码 ``MessageAck`` 不报错，但字段语义已变，建议迁移至 MessageSettlement。
MessageAck = MessageSettlement


class MessageBody(BaseModel):
    """消息日志 body（Spec §5.5 MessageBody）。

    兼容 Kafka、RabbitMQ 等常见消息中间件，与 OpenTelemetry Messaging 语义约定对齐。

    设计要点：每条消息日志通过 event_type 精确描述【一次】生命周期边事件；
    同一条消息的发送、接收、确认是多条独立日志（各自拥有独立 logId），
    通过相同 messageId / correlationId 在查询层归并为一条生命周期。
    """

    model_config = ConfigDict(populate_by_name=True)

    event_type: Literal[
        "send", "receive", "ack", "nack", "reject", "timeout", "dead_letter"
    ] = Field(
        alias="eventType",
        description=(
            "消息生命周期边事件类型。"
            "send=生产者发送/发布；receive=消费者取得/处理；"
            "ack/nack/reject/timeout/dead_letter=结算类事件。"
        ),
    )
    operation_name: str | None = Field(
        default=None,
        alias="operationName",
        description="系统原生操作名（可选，仅用于保真与排查，不参与标准计数），如 'publish' / 'poll'",
    )
    system: str = Field(description="消息系统类型，如 'kafka' / 'rabbitmq'")
    destination: MessageDestination = Field(description="目的地信息")
    consumer_group_name: str | None = Field(
        default=None,
        alias="consumerGroupName",
        description="消费分组名（消费侧事件填写）",
    )
    subscription_name: str | None = Field(
        default=None,
        alias="subscriptionName",
        description="订阅名（消费侧事件填写）",
    )
    routing: MessageRouting | None = Field(default=None, description="路由信息")
    message_id: str | None = Field(
        default=None, alias="messageId", description="消息唯一 ID"
    )
    payload_size_bytes: int | None = Field(
        default=None, alias="payloadSizeBytes", description="消息体大小（字节）"
    )
    delivery_attempt: int | None = Field(
        default=None,
        alias="deliveryAttempt",
        description="投递尝试次数，1=首次，>1=重试；仅消费侧（receive/结算）事件有值",
    )
    settlement: MessageSettlement | None = Field(
        default=None,
        description="结算补充信息（latencyMs/reason），仅结算类事件填写",
    )
    error: ErrorInfo | None = Field(default=None, description="错误信息（如有）")
    attributes: dict[str, Any] | None = Field(default=None, description="扩展属性")


class MessageLogRecord(BaseModel):
    """完整的消息 LogRecord（发射侧强类型）。"""

    schema_version: str = "1.0"
    log_id: str
    log_type: str = "message"
    timestamp: str
    aic: str
    body: MessageBody
    integrity: LogRecordIntegrity | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    severity_text: str | None = None
    severity_number: int | None = None

    @staticmethod
    def new_log_id() -> str:
        try:
            return str(uuid.uuid7())  # type: ignore[attr-defined]
        except AttributeError:
            return str(uuid.uuid4())


# ─── 审计日志（Spec §5.6）─────────────────────────────────────────────────────


class AuditActor(BaseModel):
    """审计操作者（Spec §5.6 AuditBody.actor）。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(description="用户或服务 ID，如 'user-12345'")
    type: str = Field(description="操作者类型：'user' / 'service' / 'agent' / 'bot'")
    name: str | None = Field(default=None, description="用户名或服务名，如 'alice'")
    role: str | None = Field(default=None, description="角色或权限组，如 'admin'")
    ip: str | None = Field(default=None, description="客户端 IP")
    user_agent: str | None = Field(
        default=None, alias="userAgent", description="客户端 UserAgent"
    )


class AuditAction(BaseModel):
    """审计操作行为（Spec §5.6 AuditBody.action）。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="事件名称，如 'user.delete' / 'order.create'")
    type: str = Field(description="事件领域 / 类型，如 'order' / 'task_management'")
    method: str | None = Field(
        default=None, description="具体 API 方法，如 'DELETE /api/users/bob'"
    )


class AuditTarget(BaseModel):
    """审计操作对象（Spec §5.6 AuditBody.target）。"""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(description="资源类型，如 'user' / 'order' / 'session'")
    id: str = Field(description="资源 ID")
    name: str | None = Field(default=None, description="资源名称")
    before: Any | None = Field(default=None, description="变更前快照")
    after: Any | None = Field(default=None, description="变更后快照")


class AuditResult(BaseModel):
    """审计操作结果（Spec §5.6 AuditBody.result）。"""

    model_config = ConfigDict(populate_by_name=True)

    status: Literal["success", "failure", "unknown"] = Field(description="结果状态")
    reason: str | None = Field(default=None, description="详细原因（failure 时填写）")
    error_code: str | None = Field(
        default=None, alias="errorCode", description="错误码，如 'RESOURCE_NOT_FOUND'"
    )


class AuditBody(BaseModel):
    """审计日志 body（Spec §5.6 AuditBody）。

    审计日志**必须签名**（integrity 字段不可省略）。
    签名范围：顶层公共字段 + 整个 body 对象（JCS 规范化后签名）。
    """

    actor: AuditActor = Field(description="操作者信息")
    action: AuditAction = Field(description="操作行为")
    target: AuditTarget = Field(description="操作对象")
    result: AuditResult = Field(description="操作结果")


#: 向后兼容别名，旧代码 ``from acps_sdk.amp import AuditLogBody`` 不报错。
#: 注意：字段结构已迁移为嵌套形式，调用方须更新实例化代码。
AuditLogBody = AuditBody


class AuditLogRecord(BaseModel):
    """完整的审计 LogRecord（发射侧强类型）。

    序列化：``record.model_dump(mode="json", by_alias=True, exclude_none=True)``
    - 顶层字段无 alias → snake_case（schema_version / log_id / trace_id …）
    - body 子字段带 camelCase alias → 序列化为 camelCase（userAgent / errorCode …）
    """

    schema_version: str = "1.0"
    log_id: str
    log_type: str = "audit"
    timestamp: str
    aic: str
    body: AuditBody
    integrity: LogRecordIntegrity | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    severity_text: str | None = None
    severity_number: int | None = None

    @staticmethod
    def new_log_id() -> str:
        """生成新的 log_id（Python 3.14+ uuid7，旧版本 fallback 到 uuid4）。"""
        try:
            return str(uuid.uuid7())  # type: ignore[attr-defined]
        except AttributeError:
            return str(uuid.uuid4())


# ─── 系统日志（Spec §5 — body 自由格式）──────────────────────────────────────
#
# 系统日志 body 为自由格式（Spec §4.1），不做结构约束。
# 发射侧直接使用 LogRecord 或构造 dict body 即可；
# 无需单独的 SystemBody / SystemLogRecord 类型。


# ─── 公共工具：log_id 兜底哈希（Spec §5.1.3）────────────────────────────────

# 参与兜底哈希的字段集（与签名范围一致，但不含 log_id 自身，见 Spec §5.1.3）。
# 使用 LogRecord 序列化的 JSON 键名（顶层无 alias → snake_case）。
LOG_ID_HASH_FIELDS: frozenset[str] = frozenset(
    {
        "timestamp",
        "aic",
        "trace_id",
        "span_id",
        "parent_span_id",
        "correlation_id",
        "log_type",
        "body",
    }
)


def compute_log_id_fallback(raw_dict: dict[str, Any]) -> str:
    """按 AMP Spec §5.1.3 对内容字段做 JCS 哈希，生成 log_id 的兜底值。

    当源端未在 LogRecord 中提供 log_id 时，消费端（Writer）**必须**调用此函数兜底，
    以保证同一物理事件在重投递时得到相同的幂等键。

    哈希字段（LOG_ID_HASH_FIELDS）：
        timestamp、aic、trace_id、span_id、parent_span_id、
        correlation_id、log_type、body。
    不含：log_id 自身、observed_timestamp 等管线追加字段（否则重投递产生不同键）。

    算法：JCS（RFC 8785）规范化 → SHA-256 → Base64 标准编码（无填充）。

    .. warning::
        此算法必须与 Spec §5.1.3 保持完全一致，任何 Writer 实现不得私自修改——
        log_id 会进入链哈希前像（audit §4.4），私自修改会破坏链的可验证性。

    Args:
        raw_dict: AMP LogRecord 的原始 JSON 反序列化 dict（键名同 JSON 线格式，
                  顶层字段为 snake_case，body 子字段为 camelCase）。

    Returns:
        ``sha256(jcs(payload))`` 的 Base64 编码字符串（无填充），可直接写入 log_id。

    Raises:
        ImportError: 未安装 jcs 库（``pip install jcs`` 或 ``pip install 'acps-sdk[amp-sign]'``）。
    """
    import base64
    import hashlib

    import jcs

    payload = {
        k: v for k, v in raw_dict.items() if k in LOG_ID_HASH_FIELDS and v is not None
    }
    canonical = jcs.canonicalize(payload)
    digest = hashlib.sha256(canonical).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")
