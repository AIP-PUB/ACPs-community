"""app/access/sql.py — 七端点参数化 SQL 构造（纯函数，零 I/O）。

实现设计 §6.1~§6.7「存储查询路径」。所有函数返回 (sql, params)。
标识符（表名/列名）只来自 tables.py 与白名单常量；值只走 {name:Type} 参数绑定。

C-ACCESS-QUERY-2/4/6/10/11/12/13/15
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.access.tables import (
    ACCESS_EVENTS,
    ACCESS_TOPOLOGY_EDGE_5M,
    ACCESS_TRACE_SPAN,
    EVENT_VIEW_COLUMNS,
)

if TYPE_CHECKING:
    from app.access.filters import ResolvedSort, WhereClause


# ── 辅助数据类 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeysetBound:
    """keyset 游标条件（由 cursor.decode → planner 转为 WHERE 片段）。"""

    sql: str
    params: dict[str, Any]


@dataclass(frozen=True)
class TraceLevelHaving:
    """Trace 级 HAVING 条件（hasError / min/maxTraceDurationMs 编译结果）。"""

    sql: str
    params: dict[str, Any]


# ── 维度映射常量（C-ACCESS-QUERY-2，spec §6.4.4）────────────────────────────

_OPERATIONS_DIM_COLS: Final[dict[str, list[str]]] = {
    "aic": ["aic"],
    "service": ["service_name"],
    "endpoint": ["request_method", "request_route"],
}
_TOPOLOGY_DIM_COLS: Final[dict[str, list[str]]] = {
    "aic": ["caller_aic", "callee_aic"],
    "service": ["caller_service", "callee_service"],
}
# API groupBy field names → ClickHouse column names for errors/attribution
_ATTRIBUTION_DIM_COLS: Final[dict[str, list[str]]] = {
    "errorCode": ["error_code"],
    "statusCode": ["response_status"],
    "endpoint": ["request_method", "request_route"],
}


# ── §6.2 events/query ─────────────────────────────────────────────────────────


def build_events_query(
    *,
    where: WhereClause,
    time_params: dict[str, Any],
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
    include_raw_log: bool = False,
) -> tuple[str, dict[str, Any]]:
    """events/query SQL（扫描 access_events，keyset 分页，禁 OFFSET）。"""
    cols = list(EVENT_VIEW_COLUMNS)
    if include_raw_log:
        cols.append("raw_log")
    select = ", ".join(cols)

    order_sql = _build_order_by(sort, tiebreak="log_id")
    keyset_sql, keyset_params = _unpack_keyset(keyset)

    sql = f"""\
SELECT {select}
FROM {ACCESS_EVENTS}
WHERE timestamp >= fromUnixTimestamp64Milli({{from_ms:Int64}}) AND timestamp < fromUnixTimestamp64Milli({{to_ms:Int64}})
{where.sql}
{keyset_sql}
{order_sql}
LIMIT {limit + 1}\
"""  # nosec B608
    params = {**time_params, **where.params, **keyset_params}
    return _clean_sql(sql), params


# ── §6.1 operations/query ─────────────────────────────────────────────────────


def build_operations_query(
    *,
    group_by: list[str],
    bucket_expr: str | None,
    where: WhereClause,
    time_params: dict[str, Any],
    error_status_threshold: int,
    having_min_request: int | None,
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """operations/query SQL（聚合 access_events，error_count 注入阈值）。"""
    dim_cols: list[str] = []
    for g in group_by:
        dim_cols.extend(_OPERATIONS_DIM_COLS.get(g, []))

    # SELECT 构造
    select_parts = []
    group_cols = []
    if bucket_expr:
        select_parts.append(f"{bucket_expr}(timestamp) AS bucket")
        group_cols.append("bucket")
    for c in dim_cols:
        select_parts.append(c)
        group_cols.append(c)

    select_parts += [
        "count() AS request_count",
        f"countIf(response_status >= {error_status_threshold} OR error_code != '') AS error_count",
        "avg(duration_ms) AS avg_duration_ms",
        "quantilesTDigest(0.95, 0.99)(duration_ms) AS duration_quantiles",
        "max(timestamp) AS last_seen_at",
    ]

    group_by_sql = f"GROUP BY {', '.join(group_cols)}" if group_cols else "GROUP BY tuple()"

    having_parts = []
    if having_min_request:
        having_parts.append(f"request_count >= {having_min_request}")
    keyset_sql, keyset_params = _unpack_keyset(keyset)
    if keyset_sql:
        having_parts.append(keyset_sql.lstrip("AND ").lstrip())

    having_sql = f"HAVING {' AND '.join(having_parts)}" if having_parts else ""
    order_sql = _build_order_by(sort, tiebreak=None)

    sql = f"""\
SELECT {", ".join(select_parts)}
FROM {ACCESS_EVENTS}
WHERE timestamp >= fromUnixTimestamp64Milli({{from_ms:Int64}}) AND timestamp < fromUnixTimestamp64Milli({{to_ms:Int64}})
{where.sql}
{group_by_sql}
{having_sql}
{order_sql}
LIMIT {limit + 1}\
"""  # nosec B608
    params = {**time_params, **where.params, **keyset_params}
    return _clean_sql(sql), params


# ── §6.6 errors/attribution（两段聚合 + JOIN）──────────────────────────────


def build_error_attribution_query(
    *,
    group_dims: list[str],
    where: WhereClause,
    time_params: dict[str, Any],
    error_status_threshold: int,
    top_n: int,
) -> tuple[str, dict[str, Any]]:
    """errors/attribution SQL（两段式 CTE，C-ACCESS-QUERY-10/15）。"""
    # Translate API dim names (errorCode, statusCode, endpoint) → CH column names
    dim_cols: list[str] = []
    for d in group_dims:
        dim_cols.extend(_ATTRIBUTION_DIM_COLS.get(d, [d]))
    if not dim_cols:
        dim_cols = ["error_code"]

    is_endpoint_dim = dim_cols == ["request_method", "request_route"]
    dims_sql = ", ".join(dim_cols)
    outer_dims_sql = ", ".join(f"g.{c}" for c in dim_cols)
    error_cond = f"response_status >= {error_status_threshold} OR error_code != ''"

    # When the dimension IS endpoint, method/route are the dims — no separate aliases needed.
    # Otherwise, expose method and route as endpoint context columns.
    if is_endpoint_dim:
        endpoint_alias_cols = ""
        endpoint_group_extra = ""
        affected_endpoints_expr = "[] AS affected_endpoints"
    else:
        endpoint_alias_cols = ", request_method AS method, request_route AS route"
        endpoint_group_extra = ", method, route"
        affected_endpoints_expr = (
            "arrayMap("
            "x -> (tupleElement(x, 1), tupleElement(x, 2)), "
            "arraySlice("
            "arrayReverseSort(x -> tupleElement(x, 3), "
            "groupArray((e.method, e.route, e.ep_count))), 1, 100)) AS affected_endpoints"
        )

    sql = f"""\
WITH
per_endpoint AS (
    SELECT {dims_sql}{endpoint_alias_cols},
           countIf({error_cond}) AS ep_count
    FROM {ACCESS_EVENTS}
    WHERE timestamp >= fromUnixTimestamp64Milli({{from_ms:Int64}}) AND timestamp < fromUnixTimestamp64Milli({{to_ms:Int64}})
      AND ({error_cond})
    {where.sql}
    GROUP BY {dims_sql}{endpoint_group_extra}
),
per_group AS (
    SELECT {dims_sql},
           groupUniqArray(100)(aic) AS affected_aics,
           any(error_message) AS error_message_sample,
           min(timestamp) AS first_seen_at,
           max(timestamp) AS last_seen_at
    FROM {ACCESS_EVENTS}
    WHERE timestamp >= fromUnixTimestamp64Milli({{from_ms:Int64}}) AND timestamp < fromUnixTimestamp64Milli({{to_ms:Int64}})
      AND ({error_cond})
    {where.sql}
    GROUP BY {dims_sql}
)
SELECT
    {outer_dims_sql},
    sum(e.ep_count) AS error_total,
    {affected_endpoints_expr},
    any(g.affected_aics) AS affected_aics,
    any(g.error_message_sample) AS error_message_sample,
    any(g.first_seen_at) AS first_seen_at,
    any(g.last_seen_at) AS last_seen_at
FROM per_endpoint e
INNER JOIN per_group g USING ({dims_sql})
GROUP BY {outer_dims_sql}
ORDER BY error_total DESC
LIMIT {top_n}\
"""  # nosec B608
    params = {**time_params, **where.params}
    return _clean_sql(sql), params


# ── §6.7 slow-requests/top ────────────────────────────────────────────────────


def build_slow_requests_query(
    *,
    where: WhereClause,
    time_params: dict[str, Any],
    min_duration_ms: int | None,
    top_n: int,
    keyset: KeysetBound | None,
) -> tuple[str, dict[str, Any]]:
    """slow-requests/top SQL（按 duration_ms 倒序 TopN，禁 OFFSET）。"""
    min_filter = ""
    min_params: dict[str, Any] = {}
    if min_duration_ms is not None:
        min_filter = "AND duration_ms >= {min_duration_ms:Int64}"
        min_params = {"min_duration_ms": min_duration_ms}

    keyset_sql, keyset_params = _unpack_keyset(keyset)
    cols = ", ".join(
        [
            "log_id",
            "timestamp",
            "aic",
            "trace_id",
            "request_method",
            "request_route",
            "request_url",
            "duration_ms",
            "response_status",
        ]
    )

    sql = f"""\
SELECT {cols}
FROM {ACCESS_EVENTS}
WHERE timestamp >= fromUnixTimestamp64Milli({{from_ms:Int64}}) AND timestamp < fromUnixTimestamp64Milli({{to_ms:Int64}})
{min_filter}
{where.sql}
{keyset_sql}
ORDER BY duration_ms DESC, log_id DESC
LIMIT {top_n}\
"""  # nosec B608
    params = {**time_params, **min_params, **where.params, **keyset_params}
    return _clean_sql(sql), params


# ── §6.3 traces/query（matching_traces + summary 聚合）────────────────────────


def build_traces_query(
    *,
    span_where: WhereClause,
    time_params: dict[str, Any],
    error_status_threshold: int,
    trace_level_having: TraceLevelHaving,
    trace_max_duration_hours: int,
    keyset: KeysetBound | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """traces/query SQL（两段式：matching_traces + summary 聚合，C-ACCESS-QUERY-4）。"""
    error_cond = f"error_code != '' OR response_status >= {error_status_threshold}"
    keyset_sql, keyset_params = _unpack_keyset(keyset)

    having_parts = []
    if trace_level_having.sql:
        having_parts.append(trace_level_having.sql)
    if keyset_sql:
        having_parts.append(keyset_sql.lstrip("AND ").lstrip())
    having_sql = f"HAVING {' AND '.join(having_parts)}" if having_parts else ""

    sql = f"""\
WITH matching_traces AS (
    SELECT DISTINCT trace_id
    FROM {ACCESS_TRACE_SPAN}
    WHERE timestamp BETWEEN fromUnixTimestamp64Milli({{from_ms:Int64}}) AND fromUnixTimestamp64Milli({{to_ms:Int64}})
    {span_where.sql}
)
SELECT
    trace_id,
    min(timestamp) AS first_seen_at,
    max(timestamp) AS last_seen_at,
    toUInt32(
        max(toUnixTimestamp64Milli(timestamp) + duration_ms) -
        min(toUnixTimestamp64Milli(timestamp))
    ) AS duration_ms,
    count() AS total_spans,
    countIf({error_cond}) AS error_count,
    argMinIf(aic, timestamp, parent_span_id = '') AS root_aic,
    argMinIf(request_method, timestamp, parent_span_id = '') AS root_method,
    argMinIf(request_route, timestamp, parent_span_id = '') AS root_route
FROM {ACCESS_TRACE_SPAN}
WHERE trace_id IN (SELECT trace_id FROM matching_traces)
  AND timestamp BETWEEN
        (fromUnixTimestamp64Milli({{from_ms:Int64}}) - INTERVAL {trace_max_duration_hours} HOUR) AND
        (fromUnixTimestamp64Milli({{to_ms:Int64}}) + INTERVAL {trace_max_duration_hours} HOUR)
GROUP BY trace_id
{having_sql}
ORDER BY last_seen_at DESC, trace_id DESC
LIMIT {limit + 1}\
"""  # nosec B608
    params = {**time_params, **span_where.params, **trace_level_having.params, **keyset_params}
    return _clean_sql(sql), params


# ── §6.4 traces/{traceId} ─────────────────────────────────────────────────────


def build_trace_spans_query(*, trace_max_spans: int) -> tuple[str, dict[str, Any]]:
    """traces/{traceId} SQL — 精确拉取 trace 的所有 span（C-ACCESS-QUERY-13）。"""
    sql = f"""\
SELECT *
FROM {ACCESS_TRACE_SPAN}
WHERE trace_id = {{tid:String}}
ORDER BY timestamp, span_id
LIMIT {trace_max_spans + 1}\
"""  # nosec B608
    return sql, {}


def build_trace_events_query() -> tuple[str, dict[str, Any]]:
    """traces/{traceId} include_events=true：二次读 access_events（C-ACCESS-QUERY-11）。"""
    select = ", ".join(EVENT_VIEW_COLUMNS)
    sql = f"""\
SELECT {select}
FROM {ACCESS_EVENTS}
WHERE trace_id = {{tid:String}}
ORDER BY timestamp, span_id\
"""  # nosec B608
    return sql, {}


# ── §6.5 topology/query（*Merge 重聚合）──────────────────────────────────────


def build_topology_query(
    *,
    group_by: str,
    bucket_expr: str | None,
    edge_where: WhereClause,
    bucket_params: dict[str, Any],
    having_min_call: int | None,
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """topology/query SQL（*Merge 重聚合，C-ACCESS-QUERY-6）。

    不得返回 state 列/未合并 partial；必须按目标维度重新 GROUP BY。
    """
    dim_cols = _TOPOLOGY_DIM_COLS.get(group_by, ["caller_aic", "callee_aic"])

    select_parts = []
    group_cols = list(dim_cols)
    if bucket_expr:
        select_parts.append(f"{bucket_expr}(bucket) AS bucket")
        group_cols.insert(0, "bucket")
    # 固定四列身份字段顺序，与 store.run_topology_query 列解析对齐（C-ACCESS-QUERY-7）。
    if group_by == "aic":
        select_parts += [
            "caller_aic",
            "any(caller_service) AS caller_service",
            "callee_aic",
            "any(callee_service) AS callee_service",
        ]
    else:
        select_parts += [
            "'' AS caller_aic",
            "caller_service",
            "'' AS callee_aic",
            "callee_service",
        ]

    select_parts += [
        "sumMerge(call_count_state) AS call_count",
        "sumMerge(error_count_state) AS error_count",
        "avgMerge(avg_duration_state) AS avg_duration_ms",
        "quantilesTDigestMerge(0.95, 0.99)(duration_quantiles_state) AS duration_quantiles",
        "maxMerge(last_seen_state) AS last_seen_at",
    ]

    # groupby_edge_filter：C-ACCESS-MODEL-8 补充约束（非空过滤）
    if group_by == "aic":
        edge_filter = "AND caller_aic != '' AND callee_aic != ''"
    else:
        edge_filter = "AND caller_service != '' AND callee_service != ''"

    group_by_sql = f"GROUP BY {', '.join(group_cols)}"
    having_parts = []
    if having_min_call:
        having_parts.append(f"call_count >= {having_min_call}")
    keyset_sql, keyset_params = _unpack_keyset(keyset)
    if keyset_sql:
        having_parts.append(keyset_sql.lstrip("AND ").lstrip())
    having_sql = f"HAVING {' AND '.join(having_parts)}" if having_parts else ""
    order_sql = _build_order_by(sort, tiebreak=None)

    sql = f"""\
SELECT {", ".join(select_parts)}
FROM {ACCESS_TOPOLOGY_EDGE_5M}
WHERE bucket BETWEEN fromUnixTimestamp64Milli({{from_bucket_ms:Int64}}) AND fromUnixTimestamp64Milli({{to_bucket_ms:Int64}})
{edge_where.sql}
{edge_filter}
{group_by_sql}
{having_sql}
{order_sql}
LIMIT {limit + 1}\
"""  # nosec B608
    params = {**bucket_params, **edge_where.params, **keyset_params}
    return _clean_sql(sql), params


# ── 私有辅助 ──────────────────────────────────────────────────────────────────


def _build_order_by(sort: list[ResolvedSort], *, tiebreak: str | None) -> str:
    """将 ResolvedSort 列表转为 ORDER BY 子句（含 tiebreak）。"""
    parts = [] if not sort else [f"{s.column_or_alias} {s.order.upper()}" for s in sort]
    if tiebreak:
        parts.append(f"{tiebreak} DESC")
    if not parts:
        return ""
    return "ORDER BY " + ", ".join(parts)


def _unpack_keyset(keyset: KeysetBound | None) -> tuple[str, dict[str, Any]]:
    if keyset is None:
        return "", {}
    return keyset.sql, keyset.params


def _clean_sql(sql: str) -> str:
    """合并多余空行，返回整洁 SQL。"""
    lines = [line for line in sql.splitlines() if line.strip()]
    return "\n".join(lines)
