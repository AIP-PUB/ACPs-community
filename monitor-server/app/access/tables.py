"""app/access/tables.py — Access 三表两视图 DDL 与 schema 常量（单一真相源）。

实现设计 §4.1/§4.2/§4.3。DDL 用 CREATE ... IF NOT EXISTS，保留天数由配置实例化。
所有表名、列名、列序常量在此定义——sql.py / store.py 引用，禁止硬编码。

C-ACCESS-MODEL-1/2/3/7/8/9
"""

from __future__ import annotations

from typing import Final

# ── 表名常量 ──────────────────────────────────────────────────────────────────

ACCESS_EVENTS: Final = "access_events"
ACCESS_TRACE_SPAN: Final = "access_trace_span"
ACCESS_TRACE_SPAN_MV: Final = "access_trace_span_mv"
ACCESS_TOPOLOGY_EDGE_5M: Final = "access_topology_edge_5m"
ACCESS_TOPOLOGY_EDGE_5M_MV: Final = "access_topology_edge_5m_mv"

# 拓扑 MV 固化的默认 error_status_threshold（建表时的 default.toml 值）
# runtime.py 用此常量与运行时配置比对，检测 MV 口径漂移。
TOPOLOGY_MV_DEFAULT_ERROR_THRESHOLD: Final[int] = 500

# ── 列投影常量 ────────────────────────────────────────────────────────────────

# events/query SELECT 投影；store 行映射复用，避免列序漂移。
# raw_log 仅在 includeRawLog 且部署启用时追加，不在默认投影内（C-ACCESS-QUERY-8）。
EVENT_VIEW_COLUMNS: Final[tuple[str, ...]] = (
    "log_id",
    "timestamp",
    "aic",
    "trace_id",
    "span_id",
    "parent_span_id",
    "correlation_id",
    "severity",
    "duration_ms",
    "request_method",
    "request_route",
    "request_url",
    "request_size",
    "response_status",
    "response_size",
    "caller_aic",
    "caller_service",
    "caller_ip",
    "callee_aic",
    "callee_service",
    "callee_ip",
    "error_code",
    "error_message",
    "service_name",
    "deployment_env",
    "request_headers",
    "response_headers",
    "attributes",
)

# Writer insert 列序；与 access_events DDL 列序一致。
INSERT_COLUMNS: Final[tuple[str, ...]] = (
    *EVENT_VIEW_COLUMNS,
    "observed_at",
    "raw_log",
)

# access_trace_span 写入列序（MV 投影列序）
TRACE_SPAN_COLUMNS: Final[tuple[str, ...]] = (
    "log_id",
    "timestamp",
    "aic",
    "trace_id",
    "span_id",
    "parent_span_id",
    "duration_ms",
    "request_method",
    "request_route",
    "request_url",
    "response_status",
    "caller_aic",
    "callee_aic",
    "error_code",
    "service_name",
)


# ── DDL 函数 ──────────────────────────────────────────────────────────────────


def ddl_access_events(*, raw_retention_days: int) -> str:
    """access_events 主表 DDL（§4.1，C-ACCESS-MODEL-2）。

    MergeTree，高基数列（request_url/caller_ip）不入 ORDER BY 前缀。
    含 trace_id bloom_filter skip index + error_code set skip index。
    """
    return f"""\
CREATE TABLE IF NOT EXISTS {ACCESS_EVENTS}
(
    log_id              String,
    timestamp           DateTime64(3) CODEC(DoubleDelta, ZSTD(1)),
    observed_at         DateTime64(3) CODEC(DoubleDelta, ZSTD(1)),
    aic                 LowCardinality(String),
    trace_id            String,
    span_id             String,
    parent_span_id      String,
    correlation_id      String,
    severity            LowCardinality(String),
    duration_ms         UInt32 CODEC(T64, ZSTD(1)),
    request_method      LowCardinality(String),
    request_route       String,
    request_url         String,
    request_size        UInt32 CODEC(T64, ZSTD(1)),
    response_status     UInt16,
    response_size       UInt32 CODEC(T64, ZSTD(1)),
    caller_aic          LowCardinality(String),
    caller_service      LowCardinality(String),
    caller_ip           String,
    callee_aic          LowCardinality(String),
    callee_service      LowCardinality(String),
    callee_ip           String,
    error_code          LowCardinality(String),
    error_message       String,
    service_name        LowCardinality(String),
    deployment_env      LowCardinality(String),
    request_headers     Map(String, String),
    response_headers    Map(String, String),
    attributes          Map(String, String),
    raw_log             String CODEC(ZSTD(6)),
    INDEX idx_trace     trace_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_error     error_code TYPE set(64) GRANULARITY 1,
    INDEX idx_callee    callee_aic TYPE set(256) GRANULARITY 1
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, callee_aic, aic, trace_id)
TTL toDateTime(timestamp) + INTERVAL {raw_retention_days} DAY
SETTINGS index_granularity = 8192\
"""  # nosec B608


def ddl_access_trace_span(*, raw_retention_days: int) -> str:
    """access_trace_span 派生表 DDL（§4.2，C-ACCESS-MODEL-1）。

    仅裁剪 trace 重建/搜索所需字段；ORDER BY (trace_id, timestamp, span_id) 支持按 traceId 精确拉取。
    """
    return f"""\
CREATE TABLE IF NOT EXISTS {ACCESS_TRACE_SPAN}
(
    log_id          String,
    timestamp       DateTime64(3) CODEC(DoubleDelta, ZSTD(1)),
    aic             LowCardinality(String),
    trace_id        String,
    span_id         String,
    parent_span_id  String,
    duration_ms     UInt32 CODEC(T64, ZSTD(1)),
    request_method  LowCardinality(String),
    request_route   String,
    request_url     String,
    response_status UInt16,
    caller_aic      LowCardinality(String),
    callee_aic      LowCardinality(String),
    error_code      LowCardinality(String),
    service_name    LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (trace_id, timestamp, span_id)
TTL toDateTime(timestamp) + INTERVAL {raw_retention_days} DAY
SETTINGS index_granularity = 8192\
"""  # nosec B608


def ddl_access_trace_span_mv() -> str:
    """access_trace_span 物化视图 DDL（§4.2）。

    仅收 trace_id != '' 的行（有分布式追踪上下文）。
    """
    cols = ",\n        ".join(TRACE_SPAN_COLUMNS)
    return f"""\
CREATE MATERIALIZED VIEW IF NOT EXISTS {ACCESS_TRACE_SPAN_MV}
TO {ACCESS_TRACE_SPAN}
AS
SELECT
        {cols}
FROM {ACCESS_EVENTS}
WHERE trace_id != ''\
"""  # nosec B608


def ddl_access_topology_edge_5m(*, topology_retention_days: int) -> str:
    """access_topology_edge_5m 聚合表 DDL（§4.3，C-ACCESS-MODEL-3/7）。

    AggregatingMergeTree；单 quantilesTDigest state 列同时支出 P95/P99（C-ACCESS-MODEL-7）。
    """
    return f"""\
CREATE TABLE IF NOT EXISTS {ACCESS_TOPOLOGY_EDGE_5M}
(
    bucket                  DateTime64(3) CODEC(DoubleDelta, ZSTD(1)),
    caller_aic              LowCardinality(String),
    caller_service          LowCardinality(String),
    callee_aic              LowCardinality(String),
    callee_service          LowCardinality(String),
    call_count_state        AggregateFunction(sum, UInt64),
    error_count_state       AggregateFunction(sum, UInt64),
    avg_duration_state      AggregateFunction(avg, UInt32),
    duration_quantiles_state AggregateFunction(quantilesTDigest(0.95, 0.99), UInt32),
    last_seen_state         AggregateFunction(max, DateTime64(3))
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(bucket)
ORDER BY (bucket, caller_aic, callee_aic, caller_service, callee_service)
TTL toDateTime(bucket) + INTERVAL {topology_retention_days} DAY
SETTINGS index_granularity = 8192\
"""  # nosec B608


def ddl_access_topology_edge_5m_mv(*, error_status_threshold: int) -> str:
    """access_topology_edge_5m 物化视图 DDL（§4.3，C-ACCESS-MODEL-8/9/C-ACCESS-QUERY-15）。

    入边过滤：caller/callee 至少有标识（C-ACCESS-MODEL-8）。
    方向收敛：AND (aic = callee_aic OR callee_aic = '')，防双端重复计数（C-ACCESS-MODEL-9）。
    错误判定阈值在此固化（C-ACCESS-QUERY-15）。
    """
    return f"""\
CREATE MATERIALIZED VIEW IF NOT EXISTS {ACCESS_TOPOLOGY_EDGE_5M_MV}
TO {ACCESS_TOPOLOGY_EDGE_5M}
AS
SELECT
    toStartOfFiveMinutes(timestamp) AS bucket,
    caller_aic,
    caller_service,
    callee_aic,
    callee_service,
    sumState(toUInt64(1))                                                                        AS call_count_state,
    sumState(toUInt64(response_status >= {error_status_threshold} OR error_code != ''))         AS error_count_state,
    avgState(duration_ms)                                                                        AS avg_duration_state,
    quantilesTDigestState(0.95, 0.99)(duration_ms)                                              AS duration_quantiles_state,
    maxState(timestamp)                                                                          AS last_seen_state
FROM {ACCESS_EVENTS}
WHERE
    (caller_aic != '' OR caller_service != '')
    AND (callee_aic != '' OR callee_service != '')
    AND (aic = callee_aic OR callee_aic = '')
GROUP BY bucket, caller_aic, caller_service, callee_aic, callee_service\
"""  # nosec B608


def all_ddl_statements(
    *,
    raw_retention_days: int,
    topology_retention_days: int,
    error_status_threshold: int,
) -> list[str]:
    """建表/建视图的有序 DDL 列表（主表 → 派生表 → MV）。

    供 store.ensure_access_schema() 顺序执行。顺序重要：MV 必须在目标表之后建立。
    """
    return [
        ddl_access_events(raw_retention_days=raw_retention_days),
        ddl_access_trace_span(raw_retention_days=raw_retention_days),
        ddl_access_topology_edge_5m(topology_retention_days=topology_retention_days),
        ddl_access_trace_span_mv(),
        ddl_access_topology_edge_5m_mv(error_status_threshold=error_status_threshold),
    ]
