/**
 * AMP (Agent Monitoring Protocol) 子模块 — 完整日志类型发射器与模型（对应 Python `acps_sdk.amp`）。
 *
 * 使用方式（发射侧）：
 *
 * ```ts
 * import { AuditEmitter, AccessEmitter, MessageEmitter, type AuditBody } from "acps-sdk/amp";
 * ```
 *
 * 使用方式（接收 / 消费侧）：
 *
 * ```ts
 * import { LogRecordSchema, computeLogIdFallback } from "acps-sdk/amp";
 * ```
 *
 * 使用方式（Heartbeat Sync Profile）：
 *
 * ```ts
 * import { AliveDeltaEnvelopeSchema, aliveObjectId, shardId, seqFromStr } from "acps-sdk/amp";
 * ```
 *
 * 导出名与 Python `acps_sdk.amp.__all__` 一一对应（按 docs/porting-notes.md 改名：模型导出
 * `XxxSchema` + 同名类型，函数改 camelCase；Python 模型 / dataclass 上的方法放在同名伴生对象上，
 * 如 `AuditLogRecord.newLogId()`、`AliveView.toOutputDict(view)`）。文件末尾另有 Python 版通过子模块导入、
 * 而 Node 版只能经包入口访问的名字（trace context、DemoMetricsSampler 及选项类型）。
 */

// 接收侧
export { LogRecordSchema, LogRecordIntegritySchema } from "./models.js";
export type { LogRecord, LogRecordIntegrity } from "./models.js";
// log_id 兜底工具
export { LOG_ID_HASH_FIELDS, computeLogIdFallback } from "./models.js";

// 发射器
export { AccessEmitter } from "./accessEmitter.js";
export { MessageEmitter } from "./messageEmitter.js";
export { SystemEmitter } from "./systemEmitter.js";
export { AuditEmitter } from "./emitter.js";
export { HeartbeatEmitter } from "./heartbeatEmitter.js";
export { MetricsEmitter } from "./metricsEmitter.js";

// 签名器
export { CertificateAuditSigner, Ed25519AuditSigner, loadSignerFromKeysJson } from "./signer.js";
export type { AuditSigner } from "./signer.js";

// 心跳
export { HeartbeatBodySchema, HeartbeatLogRecordSchema, HeartbeatLogRecord } from "./models.js";
export type { HeartbeatBody } from "./models.js";

// Heartbeat Sync Profile 常量
export {
  ALIVE_DELTA_TYPE,
  ALIVE_DELTA_SCHEMA_VERSION,
  SNAPSHOT_META_RECORD_TYPE,
  SNAPSHOT_CONTENT_TYPE,
  ALIVE_OBJECT_ID_PREFIX,
  HEARTBEAT_TOPIC_DEFAULT,
  ALIVE_DELTA_TOPIC_DEFAULT,
} from "./heartbeatSync.js";
// Heartbeat Sync Profile 类型别名
export { DeltaOpSchema, DeltaKindSchema } from "./heartbeatSync.js";
export type { DeltaOp, DeltaKind } from "./heartbeatSync.js";
// Heartbeat Sync Profile 编码工具
export {
  aliveObjectId,
  aicFromObjectId,
  shardId,
  shardIndexFromId,
  seqToStr,
  seqFromStr,
} from "./heartbeatSync.js";
// Heartbeat Sync Profile 模型
export {
  AliveSetEntrySchema,
  AliveDeltaEnvelopeSchema,
  HeartbeatSyncInfoSchema,
  AliveSnapshotMetaSchema,
} from "./heartbeatSync.js";
export type {
  AliveSetEntry,
  AliveDeltaEnvelope,
  HeartbeatSyncInfo,
  AliveSnapshotMeta,
} from "./heartbeatSync.js";

// Alive Sync Consumer 引擎（跨 Consumer 复用）
export {
  AliveSyncEngine,
  DeltaDecision,
  classifyDelta,
  passesSeqGate,
  isGap,
  passesVersion,
  parseMetaLine,
  parseSnapshotRow,
  snapshotRowToRecord,
  seekTimestampMs,
  nextLookbackSeconds,
  AliveSyncError,
  GapDetectedError,
  ResyncRequired,
  SnapshotProtocolError,
} from "./aliveSync/index.js";
export { AliveView } from "./aliveSync/index.js";
export type {
  AliveSyncStore,
  AliveReader,
  AliveRecord,
  ShardCheckpoint,
} from "./aliveSync/index.js";

// 指标目录（公共 metric 名契约）
export {
  MetricFamily,
  MetricFamilySchema,
  KNOWN_WINDOWS,
  KNOWN_QUANTILES,
  PublicMetricMetaSchema,
  PUBLIC_METRIC_CATALOG,
  lookupPublicMetric,
} from "./metricsCatalog.js";
export type { PublicMetricMeta } from "./metricsCatalog.js";

// 指标
export {
  LoadMetricsSchema,
  WindowMetricsSchema,
  MetricsBodySchema,
  MetricsLogRecordSchema,
  MetricsLogRecord,
} from "./models.js";
export type { LoadMetrics, WindowMetrics, MetricsBody } from "./models.js";

// 访问
export {
  ErrorInfoSchema,
  AccessRequestSchema,
  AccessResponseSchema,
  AccessParticipantSchema,
  AccessBodySchema,
  AccessLogRecordSchema,
  AccessLogRecord,
} from "./models.js";
export type {
  ErrorInfo,
  AccessRequest,
  AccessResponse,
  AccessParticipant,
  AccessBody,
} from "./models.js";

// 消息
export {
  MessageDestinationSchema,
  MessageRoutingSchema,
  MessageSettlementSchema,
  MessageAckSchema,
  MessageBodySchema,
  MessageLogRecordSchema,
  MessageLogRecord,
} from "./models.js";
export type {
  MessageDestination,
  MessageRouting,
  MessageSettlement,
  MessageAck,
  MessageBody,
} from "./models.js";

// 审计
export {
  AuditActorSchema,
  AuditActionSchema,
  AuditTargetSchema,
  AuditResultSchema,
  AuditBodySchema,
  AuditLogRecordSchema,
  AuditLogRecord,
} from "./models.js";
export type { AuditActor, AuditAction, AuditTarget, AuditResult, AuditBody } from "./models.js";

// 别名
export { AuditLogBodySchema } from "./models.js";
export type { AuditLogBody } from "./models.js";

// ─── 以下为 Python 版经子模块导入的公共名（Node 包只暴露 `acps-sdk/amp` 入口） ───

// acps_sdk.amp.trace_context
export {
  TRACEPARENT_HEADER,
  formatTraceparent,
  newSpanId,
  newTraceId,
  parseTraceparent,
} from "./traceContext.js";
export type { TraceContext } from "./traceContext.js";
// acps_sdk.amp.metrics_demo / metrics_emitter.SampleProvider
export { DemoMetricsSampler } from "./metricsDemo.js";
export type { SampleProvider, MetricsEmitterOptions } from "./metricsEmitter.js";
export type { AliveOutputDict, ApplyDeltaOptions, NextLookbackOptions } from "./aliveSync/index.js";
// 发射器入参类型（z.input：可选字段允许 null，对应 Python 的 None）
export type {
  AccessBodyInput,
  AuditBodyInput,
  MessageBodyInput,
  MetricsBodyInput,
} from "./models.js";
// 发射器选项类型
export type { RecordEmitOptions, RunPeriodicOptions } from "./emitterSupport.js";
export type { AuditEmitterOptions } from "./emitter.js";
export type { AccessEmitterOptions } from "./accessEmitter.js";
export type { MessageEmitterOptions } from "./messageEmitter.js";
export type { SystemEmitterOptions, SystemEmitOptions } from "./systemEmitter.js";
