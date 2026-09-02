"""AMP (Agent Monitoring Protocol) 子模块 — 完整日志类型发射器与模型。

使用方式（发射侧）：
    from acps_sdk.amp import AuditEmitter, AuditBody, AuditLogRecord
    from acps_sdk.amp import AccessEmitter, HeartbeatBody, MetricsBody, AccessBody, MessageBody, MessageEmitter

使用方式（接收 / 消费侧）：
    from acps_sdk.amp import LogRecord, LogRecordIntegrity

使用方式（Heartbeat Sync Profile）：
    from acps_sdk.amp import AliveDeltaEnvelope, HeartbeatSyncInfo, AliveSnapshotMeta
    from acps_sdk.amp import alive_object_id, shard_id, seq_from_str
"""

from acps_sdk.amp.access_emitter import AccessEmitter
from acps_sdk.amp.message_emitter import MessageEmitter
from acps_sdk.amp.system_emitter import SystemEmitter
from acps_sdk.amp.emitter import AuditEmitter
from acps_sdk.amp.heartbeat_emitter import HeartbeatEmitter
from acps_sdk.amp.metrics_emitter import MetricsEmitter
from acps_sdk.amp.metrics_catalog import (
    KNOWN_QUANTILES,
    KNOWN_WINDOWS,
    PUBLIC_METRIC_CATALOG,
    MetricFamily,
    PublicMetricMeta,
    lookup_public_metric,
)
from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_SCHEMA_VERSION,
    ALIVE_DELTA_TOPIC_DEFAULT,
    ALIVE_DELTA_TYPE,
    ALIVE_OBJECT_ID_PREFIX,
    HEARTBEAT_TOPIC_DEFAULT,
    SNAPSHOT_CONTENT_TYPE,
    SNAPSHOT_META_RECORD_TYPE,
    AliveDeltaEnvelope,
    AliveSetEntry,
    AliveSnapshotMeta,
    DeltaKind,
    DeltaOp,
    HeartbeatSyncInfo,
    aic_from_object_id,
    alive_object_id,
    seq_from_str,
    seq_to_str,
    shard_id,
    shard_index_from_id,
)
from acps_sdk.amp.alive_sync import (
    AliveReader,
    AliveRecord,
    AliveSyncEngine,
    AliveSyncError,
    AliveSyncStore,
    AliveView,
    DeltaDecision,
    GapDetectedError,
    ResyncRequired,
    ShardCheckpoint,
    SnapshotProtocolError,
    classify_delta,
    is_gap,
    next_lookback_seconds,
    parse_meta_line,
    parse_snapshot_row,
    passes_seq_gate,
    passes_version,
    seek_timestamp_ms,
    snapshot_row_to_record,
)
from acps_sdk.amp.signer import AuditSigner, CertificateAuditSigner, Ed25519AuditSigner, load_signer_from_keys_json
from acps_sdk.amp.models import (
    # 接收侧通用模型
    LogRecord,
    LogRecordIntegrity,
    # log_id 兜底工具（Spec §5.1.3）
    LOG_ID_HASH_FIELDS,
    compute_log_id_fallback,
    # 心跳日志
    HeartbeatBody,
    HeartbeatLogRecord,
    # 指标日志
    LoadMetrics,
    WindowMetrics,
    MetricsBody,
    MetricsLogRecord,
    # 访问日志
    ErrorInfo,
    AccessRequest,
    AccessResponse,
    AccessParticipant,
    AccessBody,
    AccessLogRecord,
    # 消息日志
    MessageDestination,
    MessageRouting,
    MessageSettlement,
    MessageAck,
    MessageBody,
    MessageLogRecord,
    # 审计日志
    AuditActor,
    AuditAction,
    AuditTarget,
    AuditResult,
    AuditBody,
    AuditLogRecord,
    # 向后兼容别名
    AuditLogBody,
)

__all__ = [
    # 接收侧
    "LogRecord",
    "LogRecordIntegrity",
    # log_id 兜底工具
    "LOG_ID_HASH_FIELDS",
    "compute_log_id_fallback",
    # 发射器
    "AccessEmitter",
    "MessageEmitter",
    "SystemEmitter",
    "AuditEmitter",
    "HeartbeatEmitter",
    "MetricsEmitter",
    # 签名器
    "AuditSigner",
    "CertificateAuditSigner",
    "Ed25519AuditSigner",
    "load_signer_from_keys_json",
    # 心跳
    "HeartbeatBody",
    "HeartbeatLogRecord",
    # Heartbeat Sync Profile 常量
    "ALIVE_DELTA_TYPE",
    "ALIVE_DELTA_SCHEMA_VERSION",
    "SNAPSHOT_META_RECORD_TYPE",
    "SNAPSHOT_CONTENT_TYPE",
    "ALIVE_OBJECT_ID_PREFIX",
    "HEARTBEAT_TOPIC_DEFAULT",
    "ALIVE_DELTA_TOPIC_DEFAULT",
    # Heartbeat Sync Profile 类型别名
    "DeltaOp",
    "DeltaKind",
    # Heartbeat Sync Profile 编码工具
    "alive_object_id",
    "aic_from_object_id",
    "shard_id",
    "shard_index_from_id",
    "seq_to_str",
    "seq_from_str",
    # Heartbeat Sync Profile 模型
    "AliveSetEntry",
    "AliveDeltaEnvelope",
    "HeartbeatSyncInfo",
    "AliveSnapshotMeta",
    # Alive Sync Consumer 引擎（跨 Consumer 复用）
    "AliveSyncEngine",
    "AliveSyncStore",
    "AliveReader",
    "AliveRecord",
    "ShardCheckpoint",
    "AliveView",
    "DeltaDecision",
    "classify_delta",
    "passes_seq_gate",
    "is_gap",
    "passes_version",
    "parse_meta_line",
    "parse_snapshot_row",
    "snapshot_row_to_record",
    "seek_timestamp_ms",
    "next_lookback_seconds",
    "AliveSyncError",
    "GapDetectedError",
    "ResyncRequired",
    "SnapshotProtocolError",
    # 指标目录（公共 metric 名契约）
    "MetricFamily",
    "KNOWN_WINDOWS",
    "KNOWN_QUANTILES",
    "PublicMetricMeta",
    "PUBLIC_METRIC_CATALOG",
    "lookup_public_metric",
    # 指标
    "LoadMetrics",
    "WindowMetrics",
    "MetricsBody",
    "MetricsLogRecord",
    # 访问
    "ErrorInfo",
    "AccessRequest",
    "AccessResponse",
    "AccessParticipant",
    "AccessBody",
    "AccessLogRecord",
    # 消息
    "MessageDestination",
    "MessageRouting",
    "MessageSettlement",
    "MessageAck",
    "MessageBody",
    "MessageLogRecord",
    # 审计
    "AuditActor",
    "AuditAction",
    "AuditTarget",
    "AuditResult",
    "AuditBody",
    "AuditLogRecord",
    # 别名
    "AuditLogBody",
]
