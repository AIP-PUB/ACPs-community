"""app/message/tables.py — 四表 DDL 常量 + 表名/列名/列序常量（schema 单一真相源）。

无 MV DDL（派生表由应用层 compactor 周期重算，设计 §2 / §3.3）。
DDL 均为 CREATE ... IF NOT EXISTS（幂等），保留天数通过参数注入（不硬编码）。
"""

from __future__ import annotations

from typing import Final

# ── 表名常量（唯一来源；sql.py / store.py 引用） ──────────────────────────────

MESSAGE_EVENTS: Final = "message_events"
MESSAGE_LIFECYCLE: Final = "message_lifecycle"
MESSAGE_DESTINATION_STATE: Final = "message_destination_state_snapshot"
MESSAGE_DESTINATION_STATS_5M: Final = "message_destination_stats_5m"

# ── message_events 写入列序（Writer insert 用；与 DDL 列序一致） ──────────────
# 注意：`partition`/`offset` 是 CH 保留字，列名本身不带反引号
# （clickhouse-connect column_names 用裸名），仅在 SQL 文本中由 sql.py 加反引号转义。
INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "log_id",
    "timestamp",
    "observed_at",
    "aic",
    "trace_id",
    "correlation_id",
    "direction",
    "event_type",
    "system",
    "destination_name",
    "destination_kind",
    "virtual_host",
    "subscription_name",
    "consumer_group_name",
    "routing_key",
    "partition",
    "offset",
    "message_id",
    "lifecycle_key",
    "payload_size_bytes",
    "delivery_attempt",
    "settlement_latency_ms",
    "settlement_reason",
    "error_code",
    "error_message",
    "attributes",
    "raw_log",
)

# ── events/query SELECT 投影（不含 raw_log / observed_at；store 行映射复用） ──
# raw_log 仅在 includeRawLog 且部署启用时追加（C-MESSAGE-QUERY-9）
EVENT_VIEW_COLUMNS: Final[tuple[str, ...]] = tuple(c for c in INSERT_COLUMNS if c not in {"raw_log", "observed_at"})

# ── message_lifecycle 列序（compactor INSERT 目标列 + 读路径列） ──────────────
LIFECYCLE_COLUMNS: Final[tuple[str, ...]] = (
    "lifecycle_key",
    "message_id",
    "correlation_id",
    "trace_id",
    "system",
    "destination_name",
    "destination_kind",
    "virtual_host",
    "subscription_name",
    "consumer_group_name",
    "first_seen_at",
    "last_seen_at",
    "compacted_at",
    "dead_lettered_at",
    "producer_aics",
    "consumer_aics",
    "send_count",
    "receive_count",
    "max_delivery_attempt",
    "dead_lettered",
    "dead_letter_reason",
    "terminal_state",
)

# ── lifecycle 读路径列（不含版本列 compacted_at，供 SELECT 投影用） ──────────
LIFECYCLE_READ_COLUMNS: Final[tuple[str, ...]] = tuple(c for c in LIFECYCLE_COLUMNS if c != "compacted_at")

# ── 逻辑主键五元组（argMax 读路径 GROUP BY；C-MESSAGE-MODEL-8，不得省略任一） ──
LIFECYCLE_LOGICAL_KEY: Final[tuple[str, ...]] = (
    "system",
    "destination_name",
    "destination_kind",
    "virtual_host",
    "lifecycle_key",
)

# ── message_destination_state_snapshot 列序 ──────────────────────────────────
STATE_SNAPSHOT_COLUMNS: Final[tuple[str, ...]] = (
    "captured_at",
    "system",
    "destination_name",
    "destination_kind",
    "virtual_host",
    "visible_messages",
    "inflight_messages",
    "delayed_messages",
    "dead_letter_messages",
    "oldest_message_age_seconds",
    "active_consumers",
    "size_bytes",
)

# ── message_destination_stats_5m 列序 ────────────────────────────────────────
STATS_5M_COLUMNS: Final[tuple[str, ...]] = (
    "bucket",
    "system",
    "destination_name",
    "destination_kind",
    "virtual_host",
    "compacted_at",
    "produced_count",
    "consumed_count",
    "ack_count",
    "nack_count",
    "reject_count",
    "timeout_count",
    "dead_letter_count",
    "retry_count",
    "ack_latency_sum_ms",
    "ack_sample_count",
    "last_seen_at",
)


# ── DDL 函数 ──────────────────────────────────────────────────────────────────


def ddl_message_events(*, raw_retention_days: int) -> str:
    """生成 message_events 主表 DDL（MergeTree，非 ReplacingMergeTree，设计 §3.1 / §4.1）。"""
    return f"""\
CREATE TABLE IF NOT EXISTS message_events
(
    log_id             String,
    timestamp          DateTime64(3, 'UTC'),
    observed_at        DateTime64(3, 'UTC'),
    aic                LowCardinality(String),
    trace_id           String,
    correlation_id     String,
    direction          LowCardinality(String),
    event_type         LowCardinality(String),
    system             LowCardinality(String),
    destination_name   LowCardinality(String),
    destination_kind   LowCardinality(String),
    virtual_host       LowCardinality(String),
    subscription_name  LowCardinality(String),
    consumer_group_name LowCardinality(String),
    routing_key        String,
    `partition`        Nullable(String),
    `offset`           Nullable(Int64),
    message_id         String,
    lifecycle_key      String,
    payload_size_bytes UInt32,
    delivery_attempt   Nullable(UInt16),
    settlement_latency_ms Nullable(UInt32),
    settlement_reason  String,
    error_code         String,
    error_message      String,
    attributes         Map(String, String),
    raw_log            String CODEC(ZSTD(6)),
    INDEX idx_msg_id message_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_lifecycle lifecycle_key TYPE bloom_filter GRANULARITY 1,
    INDEX idx_correlation correlation_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_trace trace_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_observed_at observed_at TYPE minmax GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, system, destination_name, lifecycle_key)
TTL toDateTime(timestamp) + INTERVAL {raw_retention_days} DAY
"""


def ddl_message_lifecycle(*, lifecycle_retention_days: int) -> str:
    """生成 message_lifecycle 派生表 DDL（ReplacingMergeTree(compacted_at)，设计 §4.2）。"""
    return f"""\
CREATE TABLE IF NOT EXISTS message_lifecycle
(
    lifecycle_key       String,
    message_id          String,
    correlation_id      String,
    trace_id            String,
    system              LowCardinality(String),
    destination_name    LowCardinality(String),
    destination_kind    LowCardinality(String),
    virtual_host        LowCardinality(String),
    subscription_name   LowCardinality(String),
    consumer_group_name LowCardinality(String),
    first_seen_at       DateTime64(3, 'UTC'),
    last_seen_at        DateTime64(3, 'UTC'),
    compacted_at        DateTime64(3, 'UTC'),
    dead_lettered_at    Nullable(DateTime64(3, 'UTC')),
    producer_aics       Array(String),
    consumer_aics       Array(String),
    send_count          UInt32,
    receive_count       UInt32,
    max_delivery_attempt Nullable(UInt16),
    dead_lettered       UInt8,
    dead_letter_reason  String,
    terminal_state      LowCardinality(String)
)
ENGINE = ReplacingMergeTree(compacted_at)
PARTITION BY toYYYYMM(first_seen_at)
ORDER BY (system, destination_name, destination_kind, virtual_host, lifecycle_key)
TTL toDateTime(last_seen_at) + INTERVAL {lifecycle_retention_days} DAY
"""


def ddl_message_destination_state_snapshot(*, destination_state_retention_days: int) -> str:
    """生成 message_destination_state_snapshot 快照表 DDL（ReplacingMergeTree(captured_at)，设计 §4.3）。"""
    return f"""\
CREATE TABLE IF NOT EXISTS message_destination_state_snapshot
(
    captured_at                DateTime64(3, 'UTC'),
    system                     LowCardinality(String),
    destination_name           LowCardinality(String),
    destination_kind           LowCardinality(String),
    virtual_host               LowCardinality(String),
    visible_messages           UInt64,
    inflight_messages          Nullable(UInt64),
    delayed_messages           Nullable(UInt64),
    dead_letter_messages       Nullable(UInt64),
    oldest_message_age_seconds Nullable(UInt32),
    active_consumers           Nullable(UInt32),
    size_bytes                 Nullable(UInt64)
)
ENGINE = ReplacingMergeTree(captured_at)
PARTITION BY toYYYYMMDD(captured_at)
ORDER BY (system, destination_name, destination_kind, virtual_host, captured_at)
TTL toDateTime(captured_at) + INTERVAL {destination_state_retention_days} DAY
"""


def ddl_message_destination_stats_5m(*, destination_stats_retention_days: int) -> str:
    """生成 message_destination_stats_5m 吞吐派生表 DDL（ReplacingMergeTree(compacted_at)，设计 §4.4）。"""
    return f"""\
CREATE TABLE IF NOT EXISTS message_destination_stats_5m
(
    bucket              DateTime('UTC'),
    system              LowCardinality(String),
    destination_name    LowCardinality(String),
    destination_kind    LowCardinality(String),
    virtual_host        LowCardinality(String),
    compacted_at        DateTime64(3, 'UTC'),
    produced_count      UInt64,
    consumed_count      UInt64,
    ack_count           UInt64,
    nack_count          UInt64,
    reject_count        UInt64,
    timeout_count       UInt64,
    dead_letter_count   UInt64,
    retry_count         UInt64,
    ack_latency_sum_ms  UInt64,
    ack_sample_count    UInt64,
    last_seen_at        DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(compacted_at)
PARTITION BY toYYYYMMDD(bucket)
ORDER BY (bucket, system, destination_name, destination_kind, virtual_host)
TTL toDateTime(bucket) + INTERVAL {destination_stats_retention_days} DAY
"""


def all_ddl_statements(
    *,
    raw_retention_days: int,
    lifecycle_retention_days: int,
    destination_state_retention_days: int,
    destination_stats_retention_days: int,
) -> list[str]:
    """返回四表 DDL 列表（主表→lifecycle→state snapshot→stats_5m），供 store.ensure_message_schema 顺序执行。

    无 MV DDL（区别于 access 的两个 MV，compactor 模型不依赖 MV）。
    """
    return [
        ddl_message_events(raw_retention_days=raw_retention_days),
        ddl_message_lifecycle(lifecycle_retention_days=lifecycle_retention_days),
        ddl_message_destination_state_snapshot(destination_state_retention_days=destination_state_retention_days),
        ddl_message_destination_stats_5m(destination_stats_retention_days=destination_stats_retention_days),
    ]
