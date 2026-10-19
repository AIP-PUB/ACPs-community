/**
 * AMP (Agent Monitoring Protocol) 全量日志数据模型（对应 Python `acps_sdk.amp.models`）。
 *
 * 遵循 AMP Spec v02.00 §5 的完整定义，覆盖六种日志类型。
 *
 * 设计原则：
 * - 接收侧（monitor-server 等消费端）使用泛型 LogRecord（body: Record<string, unknown>）
 * - 发射侧（Agent / SDK 客户端）使用各日志类型的强类型 XxxBody + XxxLogRecord
 * - 顶层字段无 alias → 保持 snake_case（schema_version / log_id / trace_id …）
 * - body 子字段为 camelCase 线上名（uptimeSeconds / durationMs …）
 *
 * zod 对象按声明顺序输出键，各 schema 的字段顺序与 Python 模型定义顺序一致，
 * 这样 NDJSON 行的键顺序与 Python `model_dump()` 相同。
 *
 * 日志类型：
 *     heartbeat / metrics / access / message / audit / system
 *     系统日志（system）body 为自由格式，直接使用 LogRecord 即可。
 */

import { createHash, randomUUID } from "node:crypto";

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";
import { canonicalize } from "./jcs.js";
import { PyFloat } from "../internal/pyCompat.js";

/** pydantic `int` 字段（限 JS 安全整数范围）。 */
const Int = z.number().int();

/**
 * pydantic `float` 字段：接受任意 JS number，包括 NaN 与 ±Infinity（pydantic 默认 `allow_inf_nan=True`）。
 * 序列化时与 Python `json.dumps` 一样写出 `NaN` / `Infinity` / `-Infinity`。
 */
const Float = z.custom<number>((value) => typeof value === "number", {
  message: "Invalid input: expected number",
});

/**
 * 生成新的 log_id（各 `XxxLogRecord.newLogId()` 的实现）。
 *
 * Python 版 `XxxLogRecord.new_log_id()` 在 Python 3.14+ 用 uuid7、旧版本回退 uuid4；
 * Node 版与 Python ≤ 3.13 的实际行为一致，使用 `crypto.randomUUID()`（UUID v4）。
 */
export function newLogId(): string {
  return randomUUID();
}

/** 发射侧 LogRecord 伴生对象上的方法（Python 模型的 staticmethod）。 */
const logRecordMethods = {
  /** 生成新的 log_id（UUID 字符串）。 */
  newLogId,
} as const;

// ─── 共享：完整性签名 ──────────────────────────────────────────────────────────

/**
 * AMP 数字签名信息（Spec §5.1.2 integrity 字段）。
 *
 * 接收侧与发射侧共用。算法推荐优先级：EdDSA > ES256 > RS256（见 Spec §5.1.2）。
 * - alg：签名算法 'EdDSA'（首选）/ 'ES256' / 'RS256'
 * - kid：签名证书的 X.509 序列号（大写十六进制字符串，如 '3F8A91B2C4D5E6F7'），
 *   验签方通过 kid 向 CA 服务查询对应证书的公钥（GET /acps-atr-v2/ca/keys/{kid}）
 * - sig：Base64url 编码的数字签名（不含填充 '='）
 */
export const LogRecordIntegritySchema = z.object({
  alg: z.string(),
  kid: z.string(),
  sig: z.string(),
});
export type LogRecordIntegrity = z.output<typeof LogRecordIntegritySchema>;

// ─── 接收侧通用 LogRecord ─────────────────────────────────────────────────────

/**
 * AMP 顶层日志记录（接收 / 消费侧通用模型）。
 *
 * 从 Kafka / 日志文件反序列化后，按 log_type 分派给各处理器。body 保留为对象，由处理器自行解析。
 * timestamp 保留原始字符串，不做时间转换（链哈希依赖字节级一致性）。
 */
export const LogRecordSchema = z.object({
  /** Schema 版本，如 '1.0' */
  schema_version: z.string(),
  /** ISO 8601 带时区时间戳（事件产生时间） */
  timestamp: z.string(),
  /** Agent Instance Context 标识符 */
  aic: z.string(),
  /** heartbeat / metrics / access / message / audit / system */
  log_type: z.string(),
  /**
   * 单事件幂等键（Spec §5.1.3）。源端一次性生成，推荐 UUIDv7；下游 Forwarder / Writer 全程不得重新生成。
   * 源端未提供时，Writer 须按 spec §5.1.3 的 JCS 内容哈希兜底（{@link computeLogIdFallback}）。
   */
  log_id: optional(z.string()),
  /** 观测时间（监控系统接收时间），ISO 8601 */
  observed_timestamp: optional(z.string()),
  /** 分布式追踪链路 ID */
  trace_id: optional(z.string()),
  /** 当前 Span ID */
  span_id: optional(z.string()),
  /** 父 Span ID */
  parent_span_id: optional(z.string()),
  /** 业务相关 ID */
  correlation_id: optional(z.string()),
  /** 日志级别文本，如 'INFO' / 'ERROR' */
  severity_text: optional(z.string()),
  /** 规范化数值级别（1-24，见 Spec §5.1.2 SeverityNumber） */
  severity_number: optional(Int),
  /** 日志 body，各日志类型格式不同 */
  body: optional(AnyDict),
  /** 日志来源描述（服务、主机、容器等） */
  resource: optional(AnyDict),
  /** 附加键值对属性 */
  attributes: optional(AnyDict),
  /** 数字签名信息（审计日志必须携带） */
  integrity: optional(LogRecordIntegritySchema),
});
export type LogRecord = z.output<typeof LogRecordSchema>;

/** 发射侧强类型 LogRecord 的公共尾部字段（integrity 之后的追踪 / 级别字段）。 */
const recordTailShape = {
  integrity: optional(LogRecordIntegritySchema),
  trace_id: optional(z.string()),
  span_id: optional(z.string()),
  parent_span_id: optional(z.string()),
  correlation_id: optional(z.string()),
  severity_text: optional(z.string()),
  severity_number: optional(Int),
};

// ─── 心跳日志（Spec §5.2）─────────────────────────────────────────────────────

/** 心跳日志 body（Spec §5.2 HeartbeatBody）。 */
export const HeartbeatBodySchema = z.object({
  /** 系统自启动起的累计运行时间（秒） */
  uptimeSeconds: optional(Float),
});
export type HeartbeatBody = z.output<typeof HeartbeatBodySchema>;

/** 完整的心跳 LogRecord（发射侧强类型）。 */
export const HeartbeatLogRecordSchema = z.object({
  schema_version: z.string().default("1.0"),
  /** 全局唯一日志 ID，推荐 UUID v7 */
  log_id: z.string(),
  log_type: z.string().default("heartbeat"),
  /** ISO 8601 带时区时间戳 */
  timestamp: z.string(),
  aic: z.string(),
  body: HeartbeatBodySchema,
  ...recordTailShape,
});
export type HeartbeatLogRecord = z.output<typeof HeartbeatLogRecordSchema>;
/** HeartbeatLogRecord 伴生对象：`HeartbeatLogRecord.newLogId()`。 */
export const HeartbeatLogRecord = { ...logRecordMethods } as const;

// ─── 指标日志（Spec §5.3）─────────────────────────────────────────────────────

/** 即时负载指标（Spec §5.3 LoadMetrics）。 */
export const LoadMetricsSchema = z.object({
  /** 当前执行中的任务数 */
  activeTasks: Int,
  /** 队列中等待的任务数 */
  queuedTasks: Int,
  /** 系统最大并发任务数上限 */
  maxActiveTasks: optional(Int),
  /** 系统最大排队长度上限 */
  maxQueuedTasks: optional(Int),
  /** CPU 使用率，百分比 0-100 */
  cpuUsage: optional(Float),
  /** 内存使用率，百分比 0-100 */
  memoryUsage: optional(Float),
  /** 磁盘使用率，百分比 0-100 */
  diskUsage: optional(Float),
  /** 入站网络带宽使用率 0-100 */
  networkInUsage: optional(Float),
  /** 出站网络带宽使用率 0-100 */
  networkOutUsage: optional(Float),
});
export type LoadMetrics = z.output<typeof LoadMetricsSchema>;

/** 滑动窗口汇总指标（Spec §5.3 WindowMetrics）。 */
export const WindowMetricsSchema = z.object({
  /** ISO 8601 Duration，如 'PT5M'（5分钟窗口） */
  window: z.string(),
  /** 成功率，百分比 0-100 */
  successRate: Float,
  /** 窗口内总请求数 */
  requestTotal: optional(Int),
  /** 平均 RPS */
  requestPerSecond: optional(Float),
  /** 平均吞吐量（MB/s） */
  avgThroughputMBps: optional(Float),
  /** 峰值吞吐量（MB/s） */
  peakThroughputMBps: optional(Float),
  /** 平均请求时延（ms） */
  avgLatencyMs: optional(Float),
  /** P99 时延（ms） */
  p99LatencyMs: optional(Float),
  /** P95 时延（ms） */
  p95LatencyMs: optional(Float),
  /** P90 时延（ms） */
  p90LatencyMs: optional(Float),
  /** P80 时延（ms） */
  p80LatencyMs: optional(Float),
  /** P75 时延（ms） */
  p75LatencyMs: optional(Float),
  /** P50 时延（ms） */
  p50LatencyMs: optional(Float),
});
export type WindowMetrics = z.output<typeof WindowMetricsSchema>;

/** 指标日志 body（Spec §5.3 MetricsBody）。 */
export const MetricsBodySchema = z.object({
  /** 系统自启动起的累计运行时间（秒） */
  uptimeSeconds: optional(Float),
  /** 即时负载指标 */
  loadMetrics: optional(LoadMetricsSchema),
  /** 多窗口汇总指标列表 */
  windowMetrics: optional(z.array(WindowMetricsSchema)),
});
export type MetricsBody = z.output<typeof MetricsBodySchema>;
export type MetricsBodyInput = z.input<typeof MetricsBodySchema>;

/** 完整的指标 LogRecord（发射侧强类型）。 */
export const MetricsLogRecordSchema = z.object({
  schema_version: z.string().default("1.0"),
  log_id: z.string(),
  log_type: z.string().default("metrics"),
  timestamp: z.string(),
  aic: z.string(),
  body: MetricsBodySchema,
  /** 日志来源描述（service.name / service.namespace / deployment.environment.name） */
  resource: optional(AnyDict),
  ...recordTailShape,
});
export type MetricsLogRecord = z.output<typeof MetricsLogRecordSchema>;
/** MetricsLogRecord 伴生对象：`MetricsLogRecord.newLogId()`。 */
export const MetricsLogRecord = { ...logRecordMethods } as const;

// ─── 访问日志（Spec §5.4）─────────────────────────────────────────────────────

/** 通用错误信息（Spec §5.1.2 ErrorInfo，AccessLog / MessageLog 共用）。 */
export const ErrorInfoSchema = z.object({
  /** 错误代码，如 404 / 'AUTH_FAILED' */
  code: optional(z.union([Int, z.string()])),
  /** 错误消息 */
  message: optional(z.string()),
  /** 额外错误数据 */
  data: optional(z.unknown()),
  /** 堆栈追踪 */
  stackTrace: optional(z.string()),
});
export type ErrorInfo = z.output<typeof ErrorInfoSchema>;

/** HTTP / RPC 请求信息（Spec §5.4 AccessBody.request）。 */
export const AccessRequestSchema = z.object({
  /** HTTP 动词或 RPC 方法名，如 'GET' / 'GetUser' */
  method: optional(z.string()),
  /** HTTP Path 或 RPC 服务全限定名 */
  url: optional(z.string()),
  /**
   * 归一化路由模板（Spec §5.4.1），稳定 endpoint 聚合维度，与 method 组合；
   * 源端缺省时由写入侧归一化 url 推导
   */
  route: optional(z.string()),
  /** 请求头或 RPC Metadata */
  headers: optional(z.record(z.string(), z.string())),
  /** 请求体大小（字节） */
  bodySizeBytes: optional(Int),
});
export type AccessRequest = z.output<typeof AccessRequestSchema>;

/** HTTP / RPC 响应信息（Spec §5.4 AccessBody.response）。 */
export const AccessResponseSchema = z.object({
  /** HTTP 状态码或 gRPC 状态码 */
  statusCode: optional(Int),
  /** 响应头或 RPC Metadata */
  headers: optional(z.record(z.string(), z.string())),
  /** 响应体大小（字节） */
  bodySizeBytes: optional(Int),
});
export type AccessResponse = z.output<typeof AccessResponseSchema>;

/** 访问日志中的调用方或被调用方（Spec §5.4 caller / callee）。 */
export const AccessParticipantSchema = z.object({
  /** Agent Instance Context 标识符 */
  aic: optional(z.string()),
  /** 服务名称，如 'order-service' */
  serviceName: optional(z.string()),
  /** IP 地址 */
  ip: optional(z.string()),
});
export type AccessParticipant = z.output<typeof AccessParticipantSchema>;

/** 访问日志 body（Spec §5.4 AccessBody），兼容 HTTP 和 RPC（gRPC / Dubbo 等）场景。 */
export const AccessBodySchema = z.object({
  /** 请求总耗时（毫秒） */
  durationMs: optional(Float),
  /** 请求详情 */
  request: optional(AccessRequestSchema),
  /** 响应详情 */
  response: optional(AccessResponseSchema),
  /** 调用方信息（发起请求的服务） */
  caller: optional(AccessParticipantSchema),
  /** 被调用方信息（处理请求的服务） */
  callee: optional(AccessParticipantSchema),
  /** 错误信息（如有） */
  error: optional(ErrorInfoSchema),
});
export type AccessBody = z.output<typeof AccessBodySchema>;
export type AccessBodyInput = z.input<typeof AccessBodySchema>;

/** 完整的访问 LogRecord（发射侧强类型）。 */
export const AccessLogRecordSchema = z.object({
  schema_version: z.string().default("1.0"),
  log_id: z.string(),
  log_type: z.string().default("access"),
  timestamp: z.string(),
  aic: z.string(),
  body: AccessBodySchema,
  ...recordTailShape,
});
export type AccessLogRecord = z.output<typeof AccessLogRecordSchema>;
/** AccessLogRecord 伴生对象：`AccessLogRecord.newLogId()`。 */
export const AccessLogRecord = { ...logRecordMethods } as const;

// ─── 消息日志（Spec §5.5）─────────────────────────────────────────────────────

/** 消息目的地（Spec §5.5 MessageBody.destination）。 */
export const MessageDestinationSchema = z.object({
  /** Topic / Queue / Exchange 名称 */
  name: z.string(),
  /** 目的地类型 */
  kind: optional(z.enum(["topic", "queue", "exchange"])),
  /** 虚拟主机（RabbitMQ），默认 '/' */
  virtualHost: optional(z.string()),
});
export type MessageDestination = z.output<typeof MessageDestinationSchema>;

/** 消息路由信息（Spec §5.5 MessageBody.routing）。 */
export const MessageRoutingSchema = z.object({
  /** 路由键（RabbitMQ）或消息 Key（Kafka） */
  key: optional(z.string()),
  /** 分区标识，字符串类型以兼容非 Kafka 系统，并区分「无分区概念」（字段缺省）与「0 号分区」（值为 '0'） */
  partition: optional(z.string()),
  /** 消息偏移量（Kafka） */
  offset: optional(Int),
});
export type MessageRouting = z.output<typeof MessageRoutingSchema>;

/**
 * 结算补充信息（Spec §5.5 MessageBody.settlement）。
 *
 * 结算「结果」由 MessageBody.eventType 表达，此处仅补充耗时与原因。
 * 仅在 eventType 为 ack / nack / reject / timeout / dead_letter 的事件上填写。
 */
export const MessageSettlementSchema = z.object({
  /** 从接收到本次结算的耗时（ms） */
  latencyMs: optional(Float),
  /** 结算原因（nack / reject / dead_letter / timeout 的具体原因） */
  reason: optional(z.string()),
});
export type MessageSettlement = z.output<typeof MessageSettlementSchema>;

/** 向后兼容别名；旧代码 `MessageAck` 不报错，但字段语义已变，建议迁移至 MessageSettlement。 */
export const MessageAckSchema = MessageSettlementSchema;
export type MessageAck = MessageSettlement;

/**
 * 消息日志 body（Spec §5.5 MessageBody）。
 *
 * 兼容 Kafka、RabbitMQ 等常见消息中间件，与 OpenTelemetry Messaging 语义约定对齐。
 *
 * 设计要点：每条消息日志通过 eventType 精确描述【一次】生命周期边事件；
 * 同一条消息的发送、接收、确认是多条独立日志（各自拥有独立 logId），
 * 通过相同 messageId / correlationId 在查询层归并为一条生命周期。
 */
export const MessageBodySchema = z.object({
  /**
   * 消息生命周期边事件类型。send=生产者发送/发布；receive=消费者取得/处理；
   * ack/nack/reject/timeout/dead_letter=结算类事件。
   */
  eventType: z.enum(["send", "receive", "ack", "nack", "reject", "timeout", "dead_letter"]),
  /** 系统原生操作名（可选，仅用于保真与排查，不参与标准计数），如 'publish' / 'poll' */
  operationName: optional(z.string()),
  /** 消息系统类型，如 'kafka' / 'rabbitmq' */
  system: z.string(),
  /** 目的地信息 */
  destination: MessageDestinationSchema,
  /** 消费分组名（消费侧事件填写） */
  consumerGroupName: optional(z.string()),
  /** 订阅名（消费侧事件填写） */
  subscriptionName: optional(z.string()),
  /** 路由信息 */
  routing: optional(MessageRoutingSchema),
  /** 消息唯一 ID */
  messageId: optional(z.string()),
  /** 消息体大小（字节） */
  payloadSizeBytes: optional(Int),
  /** 投递尝试次数，1=首次，>1=重试；仅消费侧（receive/结算）事件有值 */
  deliveryAttempt: optional(Int),
  /** 结算补充信息（latencyMs/reason），仅结算类事件填写 */
  settlement: optional(MessageSettlementSchema),
  /** 错误信息（如有） */
  error: optional(ErrorInfoSchema),
  /** 扩展属性 */
  attributes: optional(AnyDict),
});
export type MessageBody = z.output<typeof MessageBodySchema>;
export type MessageBodyInput = z.input<typeof MessageBodySchema>;

/** 完整的消息 LogRecord（发射侧强类型）。 */
export const MessageLogRecordSchema = z.object({
  schema_version: z.string().default("1.0"),
  log_id: z.string(),
  log_type: z.string().default("message"),
  timestamp: z.string(),
  aic: z.string(),
  body: MessageBodySchema,
  ...recordTailShape,
});
export type MessageLogRecord = z.output<typeof MessageLogRecordSchema>;
/** MessageLogRecord 伴生对象：`MessageLogRecord.newLogId()`。 */
export const MessageLogRecord = { ...logRecordMethods } as const;

// ─── 审计日志（Spec §5.6）─────────────────────────────────────────────────────

/** 审计操作者（Spec §5.6 AuditBody.actor）。 */
export const AuditActorSchema = z.object({
  /** 用户或服务 ID，如 'user-12345' */
  id: z.string(),
  /** 操作者类型：'user' / 'service' / 'agent' / 'bot' */
  type: z.string(),
  /** 用户名或服务名，如 'alice' */
  name: optional(z.string()),
  /** 角色或权限组，如 'admin' */
  role: optional(z.string()),
  /** 客户端 IP */
  ip: optional(z.string()),
  /** 客户端 UserAgent */
  userAgent: optional(z.string()),
});
export type AuditActor = z.output<typeof AuditActorSchema>;

/** 审计操作行为（Spec §5.6 AuditBody.action）。 */
export const AuditActionSchema = z.object({
  /** 事件名称，如 'user.delete' / 'order.create' */
  name: z.string(),
  /** 事件领域 / 类型，如 'order' / 'task_management' */
  type: z.string(),
  /** 具体 API 方法，如 'DELETE /api/users/bob' */
  method: optional(z.string()),
});
export type AuditAction = z.output<typeof AuditActionSchema>;

/** 审计操作对象（Spec §5.6 AuditBody.target）。 */
export const AuditTargetSchema = z.object({
  /** 资源类型，如 'user' / 'order' / 'session' */
  type: z.string(),
  /** 资源 ID */
  id: z.string(),
  /** 资源名称 */
  name: optional(z.string()),
  /** 变更前快照 */
  before: optional(z.unknown()),
  /** 变更后快照 */
  after: optional(z.unknown()),
});
export type AuditTarget = z.output<typeof AuditTargetSchema>;

/** 审计操作结果（Spec §5.6 AuditBody.result）。 */
export const AuditResultSchema = z.object({
  /** 结果状态 */
  status: z.enum(["success", "failure", "unknown"]),
  /** 详细原因（failure 时填写） */
  reason: optional(z.string()),
  /** 错误码，如 'RESOURCE_NOT_FOUND' */
  errorCode: optional(z.string()),
});
export type AuditResult = z.output<typeof AuditResultSchema>;

/**
 * 审计日志 body（Spec §5.6 AuditBody）。
 *
 * 审计日志**必须签名**（integrity 字段不可省略）。
 * 签名范围：顶层公共字段 + 整个 body 对象（JCS 规范化后签名）。
 */
export const AuditBodySchema = z.object({
  /** 操作者信息 */
  actor: AuditActorSchema,
  /** 操作行为 */
  action: AuditActionSchema,
  /** 操作对象 */
  target: AuditTargetSchema,
  /** 操作结果 */
  result: AuditResultSchema,
});
export type AuditBody = z.output<typeof AuditBodySchema>;
export type AuditBodyInput = z.input<typeof AuditBodySchema>;

/** 向后兼容别名。注意：字段结构已迁移为嵌套形式，调用方须更新实例化代码。 */
export const AuditLogBodySchema = AuditBodySchema;
export type AuditLogBody = AuditBody;

/**
 * 完整的审计 LogRecord（发射侧强类型）。
 *
 * 顶层字段为 snake_case（schema_version / log_id / trace_id …），body 子字段为 camelCase（userAgent / errorCode …）。
 */
export const AuditLogRecordSchema = z.object({
  schema_version: z.string().default("1.0"),
  log_id: z.string(),
  log_type: z.string().default("audit"),
  timestamp: z.string(),
  aic: z.string(),
  body: AuditBodySchema,
  ...recordTailShape,
});
export type AuditLogRecord = z.output<typeof AuditLogRecordSchema>;
/** AuditLogRecord 伴生对象：`AuditLogRecord.newLogId()`。 */
export const AuditLogRecord = { ...logRecordMethods } as const;

// ─── 系统日志（Spec §5 — body 自由格式）──────────────────────────────────────
//
// 系统日志 body 为自由格式（Spec §4.1），不做结构约束。
// 发射侧直接使用 LogRecord 即可；无需单独的 SystemBody / SystemLogRecord 类型。

// ─── 序列化：model_dump(mode="json", by_alias=True, exclude_none=True) ────────

/**
 * 序列化描述：标出 pydantic 模型里需要特殊处理的字段（内部使用）。
 * - `"float"`：pydantic `float` 字段，整数值也按 float 输出（`12` → `12.0`）；
 * - `"any"`：`Any` / `Dict[str, Any]` 字段，按 pydantic JSON 模式把 NaN / ±Infinity 转成 null；
 * - 对象：按字段名递归；数组：`[元素描述]`；未列出的字段原样输出。
 */
export type DumpSpec =
  "float" | "any" | { readonly [field: string]: DumpSpec } | readonly [DumpSpec];

function dumpAny(value: unknown): unknown {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (Array.isArray(value)) {
    return value.map((item) => dumpAny(item));
  }
  if (value !== null && typeof value === "object" && !(value instanceof Date)) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (item !== undefined) {
        out[key] = dumpAny(item);
      }
    }
    return out;
  }
  return value;
}

function dumpValue(value: unknown, spec: DumpSpec | undefined): unknown {
  if (spec === undefined || value === undefined || value === null) {
    return value;
  }
  if (spec === "float") {
    return typeof value === "number" ? new PyFloat(value) : value;
  }
  if (spec === "any") {
    return dumpAny(value);
  }
  if (Array.isArray(spec)) {
    const itemSpec = (spec as readonly [DumpSpec])[0];
    return Array.isArray(value) ? value.map((item) => dumpValue(item, itemSpec)) : value;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  const fields = spec as { readonly [field: string]: DumpSpec };
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (item !== undefined) {
      out[key] = dumpValue(item, fields[key]);
    }
  }
  return out;
}

/**
 * 解析后的模型 → 可直接交给 `pythonJsonDumps` / `canonicalize` 的新对象（内部使用）。
 *
 * 等价于 Python `model.model_dump(mode="json", by_alias=True, exclude_none=True)`：
 * 省略值为 undefined 的字段，自由结构字段内部的 null 原样保留。
 */
export function modelDump(
  model: object,
  spec: { readonly [field: string]: DumpSpec },
): Record<string, unknown> {
  return dumpValue(model, spec) as Record<string, unknown>;
}

const ERROR_INFO_DUMP = { data: "any" } as const;

/** @internal */
export const HEARTBEAT_LOG_RECORD_DUMP = {
  body: { uptimeSeconds: "float" },
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const METRICS_BODY_DUMP = {
  uptimeSeconds: "float",
  loadMetrics: {
    cpuUsage: "float",
    memoryUsage: "float",
    diskUsage: "float",
    networkInUsage: "float",
    networkOutUsage: "float",
  },
  windowMetrics: [
    {
      successRate: "float",
      requestPerSecond: "float",
      avgThroughputMBps: "float",
      peakThroughputMBps: "float",
      avgLatencyMs: "float",
      p99LatencyMs: "float",
      p95LatencyMs: "float",
      p90LatencyMs: "float",
      p80LatencyMs: "float",
      p75LatencyMs: "float",
      p50LatencyMs: "float",
    },
  ],
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const METRICS_LOG_RECORD_DUMP = {
  body: METRICS_BODY_DUMP,
  resource: "any",
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const ACCESS_LOG_RECORD_DUMP = {
  body: { durationMs: "float", error: ERROR_INFO_DUMP },
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const MESSAGE_BODY_DUMP = {
  settlement: { latencyMs: "float" },
  error: ERROR_INFO_DUMP,
  attributes: "any",
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const MESSAGE_LOG_RECORD_DUMP = {
  body: MESSAGE_BODY_DUMP,
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const AUDIT_LOG_RECORD_DUMP = {
  body: { target: { before: "any", after: "any" } },
} as const satisfies Record<string, DumpSpec>;

/** @internal */
export const LOG_RECORD_DUMP = {
  body: "any",
  resource: "any",
  attributes: "any",
} as const satisfies Record<string, DumpSpec>;

// ─── 公共工具：log_id 兜底哈希（Spec §5.1.3）────────────────────────────────

/**
 * 参与兜底哈希的字段集（与签名范围一致，但不含 log_id 自身，见 Spec §5.1.3）。
 * 使用 LogRecord 序列化的 JSON 键名（顶层无 alias → snake_case）。
 */
export const LOG_ID_HASH_FIELDS: ReadonlySet<string> = new Set([
  "timestamp",
  "aic",
  "trace_id",
  "span_id",
  "parent_span_id",
  "correlation_id",
  "log_type",
  "body",
]);

/**
 * 按 AMP Spec §5.1.3 对内容字段做 JCS 哈希，生成 log_id 的兜底值。
 *
 * 当源端未在 LogRecord 中提供 log_id 时，消费端（Writer）**必须**调用此函数兜底，
 * 以保证同一物理事件在重投递时得到相同的幂等键。
 *
 * 哈希字段（{@link LOG_ID_HASH_FIELDS}）：timestamp、aic、trace_id、span_id、parent_span_id、
 * correlation_id、log_type、body。不含：log_id 自身、observed_timestamp 等管线追加字段（否则重投递产生不同键）。
 *
 * 算法：JCS（RFC 8785）规范化 → SHA-256 → Base64 标准编码（无填充）。
 *
 * **警告**：此算法必须与 Spec §5.1.3 保持完全一致，任何 Writer 实现不得私自修改——
 * log_id 会进入链哈希前像（audit §4.4），私自修改会破坏链的可验证性。
 *
 * @param rawDict AMP LogRecord 的原始 JSON 反序列化对象（键名同 JSON 线格式，顶层 snake_case，body 子字段 camelCase）
 * @returns `sha256(jcs(payload))` 的 Base64 编码字符串（无填充），可直接写入 log_id
 */
export function computeLogIdFallback(rawDict: Readonly<Record<string, unknown>>): string {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(rawDict)) {
    if (LOG_ID_HASH_FIELDS.has(key) && value !== null && value !== undefined) {
      payload[key] = value;
    }
  }
  const digest = createHash("sha256").update(canonicalize(payload)).digest();
  return digest.toString("base64").replace(/=+$/, "");
}
