"""app/access/store.py — ClickHouse 读侧执行 + DDL bootstrap + 写侧 insert。

唯一直接调 app.core.clickhouse_client 的读侧文件。
SQL 构造在 sql.py（纯函数、可单测）；执行在此（IO）——与 metrics 的 promql.py + tsdb.py 拆分同构。

C-ACCESS-MODEL-1：应用绝不写派生表，insert_events 只写主表。
C-ACCESS-QUERY-7/11/13
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from app.access import sql as sql_mod
from app.access import tables
from app.access.exception import ClickHouseInsertError
from app.access.schema import (
    AccessErrorAttribution,
    AccessEventView,
    AccessOperationSummary,
    AccessSlowRequestItem,
    AccessTopologyEdge,
    AccessTraceSpan,
    AccessTraceSummary,
)
from app.core.clickhouse_client import get_clickhouse_client
from app.core.config import settings

if TYPE_CHECKING:
    from app.access.events import EventRow

logger = structlog.get_logger(__name__)

# ── 维度列名 → API 名反向映射（供 run_operations_query / run_error_attribution 用）───

_OPERATIONS_CH_TO_API: Final[dict[str, str]] = {
    "aic": "aic",
    "service_name": "service",
    # endpoint: request_method + request_route → "endpoint"（特殊处理）
}

_ATTRIBUTION_CH_TO_API: Final[dict[str, str]] = {
    "error_code": "errorCode",
    "response_status": "statusCode",
    # endpoint: request_method + request_route → "endpoint"（特殊处理）
}

_ATTRIBUTION_FIXED_COLS: Final[frozenset[str]] = frozenset(
    {"error_total", "affected_endpoints", "affected_aics", "error_message_sample", "first_seen_at", "last_seen_at"}
)
_OPERATIONS_FIXED_COLS: Final[frozenset[str]] = frozenset(
    {"request_count", "error_count", "avg_duration_ms", "duration_quantiles", "last_seen_at", "bucket"}
)


# ── DDL bootstrap ─────────────────────────────────────────────────────────────


async def ensure_access_schema() -> None:
    """顺序执行三表两 MV DDL（IF NOT EXISTS，幂等）。

    保留天数/错误阈值从配置实例化；建表失败抛异常（runtime 视为启动失败）。
    """
    client = await get_clickhouse_client()
    stmts = tables.all_ddl_statements(
        raw_retention_days=settings.access_raw_retention_days,
        topology_retention_days=settings.access_topology_retention_days,
        error_status_threshold=settings.access_error_status_threshold,
    )
    for ddl in stmts:
        await client.command(ddl)


# ── 写侧 insert ───────────────────────────────────────────────────────────────


async def insert_events(rows: list[EventRow]) -> None:
    """原子写入 access_events 主表（MV 由 CH 同步派生，C-ACCESS-MODEL-1）。

    失败 → raise ClickHouseInsertError（Writer 据此不 commit、不推水位、不写去重标记，C-ACCESS-WRITE-7）。
    """
    if not rows:
        return
    client = await get_clickhouse_client()
    data = [r.as_tuple() for r in rows]
    try:
        await client.insert(
            tables.ACCESS_EVENTS,
            data,
            column_names=list(tables.INSERT_COLUMNS),
        )
    except Exception as exc:
        raise ClickHouseInsertError(f"Failed to insert {len(rows)} events into ClickHouse") from exc


# ── 查询执行辅助 ──────────────────────────────────────────────────────────────


def _query_settings() -> dict[str, Any]:
    return {"max_execution_time": settings.access_query_timeout_seconds}


async def _run_query(sql: str, params: dict[str, Any]) -> Any:
    client = await get_clickhouse_client()
    return await client.query(sql, parameters=params, settings=_query_settings())


def _coerce_ch_row(row_dict: dict[str, Any]) -> dict[str, Any]:
    """ClickHouse DateTime64 → ISO 字符串（schema 模型 timestamp/first_seen_at 等字段类型为 str）。"""
    from datetime import datetime

    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row_dict.items()}


# ── events/query ──────────────────────────────────────────────────────────────


async def run_events_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    limit: int,
    include_raw_log: bool,
) -> list[AccessEventView]:
    """执行 events/query SQL → AccessEventView 列表。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    rows = result.result_rows
    cols = list(tables.EVENT_VIEW_COLUMNS)
    views: list[AccessEventView] = []
    for row in rows:
        row_dict = dict(zip(cols, row, strict=False))
        if include_raw_log and len(row) > len(cols):
            row_dict["raw_log"] = row[len(cols)]
        views.append(AccessEventView.model_validate(_coerce_ch_row(row_dict)))
    return views


# ── operations/query ──────────────────────────────────────────────────────────


async def run_operations_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    group_by: list[str],
) -> list[AccessOperationSummary]:
    """执行 operations/query SQL → AccessOperationSummary 列表。"""
    from app.access.sql import _OPERATIONS_DIM_COLS

    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = result.column_names if hasattr(result, "column_names") else []
    summaries: list[AccessOperationSummary] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False))) if col_names else {}

        # Build dimensions dict from group_by spec
        dims: dict[str, str] = {}
        for g in group_by:
            dim_cols = _OPERATIONS_DIM_COLS.get(g, [g])
            if g == "endpoint":
                method = str(row_dict.get("request_method", ""))
                route = str(row_dict.get("request_route", ""))
                dims["endpoint"] = f"{method} {route}".strip()
            elif dim_cols:
                dims[g] = str(row_dict.get(dim_cols[0], ""))

        req_count = int(row_dict.get("request_count", 0))
        err_count = int(row_dict.get("error_count", 0))
        error_rate = (err_count / req_count) if req_count else 0.0
        quantiles = row_dict.get("duration_quantiles") or [0.0, 0.0]
        bucket_val = str(row_dict["bucket"]) if "bucket" in row_dict else None
        summaries.append(
            AccessOperationSummary(
                bucket=bucket_val,
                dimensions=dims,
                request_count=req_count,
                error_count=err_count,
                error_rate=float(error_rate),
                avg_duration_ms=float(row_dict.get("avg_duration_ms", 0.0)),
                p95_duration_ms=float(quantiles[0]) if quantiles else 0.0,
                p99_duration_ms=float(quantiles[1]) if len(quantiles) > 1 else 0.0,
                last_seen_at=str(row_dict.get("last_seen_at", "")),
            )
        )
    return summaries


# ── traces/query ──────────────────────────────────────────────────────────────


async def run_traces_query(
    stmt: tuple[str, dict[str, Any]],
) -> list[AccessTraceSummary]:
    """执行 traces/query SQL → AccessTraceSummary 列表（root_method+route → rootEndpoint）。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    summaries: list[AccessTraceSummary] = []
    for row in result.result_rows:
        # Expected: (trace_id, first_seen_at, last_seen_at, duration_ms, total_spans, error_count, root_aic, root_method, root_route)
        if len(row) >= 9:
            root_method = row[7] or ""
            root_route = row[8] or ""
            root_endpoint = f"{root_method} {root_route}".strip() if (root_method or root_route) else None
            summaries.append(
                AccessTraceSummary(
                    trace_id=row[0],
                    first_seen_at=str(row[1]),
                    last_seen_at=str(row[2]),
                    duration_ms=int(row[3]),
                    total_spans=int(row[4]),
                    error_count=int(row[5]),
                    root_aic=row[6] or None,
                    root_endpoint=root_endpoint,
                )
            )
    return summaries


async def fetch_trace_spans(
    trace_id: str,
    *,
    trace_max_spans: int,
) -> tuple[list[AccessTraceSpan], bool]:
    """精确拉取 trace 所有 span（C-ACCESS-QUERY-13 截断标记）。"""
    sql, _ = sql_mod.build_trace_spans_query(trace_max_spans=trace_max_spans)
    result = await _run_query(sql, {"tid": trace_id})
    rows = result.result_rows
    truncated = len(rows) > trace_max_spans
    spans: list[AccessTraceSpan] = []
    for row in rows[:trace_max_spans]:
        # access_trace_span columns (TRACE_SPAN_COLUMNS order)
        if len(row) >= len(tables.TRACE_SPAN_COLUMNS):
            row_dict = dict(zip(tables.TRACE_SPAN_COLUMNS, row, strict=False))
            spans.append(AccessTraceSpan.model_validate(_coerce_ch_row(row_dict)))
    return spans, truncated


async def fetch_trace_events(trace_id: str) -> list[AccessEventView]:
    """二次读 access_events（include_events=True，C-ACCESS-QUERY-11）。"""
    sql, _ = sql_mod.build_trace_events_query()
    result = await _run_query(sql, {"tid": trace_id})
    cols = list(tables.EVENT_VIEW_COLUMNS)
    views: list[AccessEventView] = []
    for row in result.result_rows:
        row_dict = dict(zip(cols, row, strict=False))
        views.append(AccessEventView.model_validate(_coerce_ch_row(row_dict)))
    return views


# ── errors/attribution ────────────────────────────────────────────────────────


async def run_error_attribution(
    stmt: tuple[str, dict[str, Any]],
) -> list[AccessErrorAttribution]:
    """执行 errors/attribution SQL → AccessErrorAttribution 列表。

    维度列名由 clickhouse-connect result.column_names 动态解析，
    经 _ATTRIBUTION_CH_TO_API 映射回 API field 名（errorCode/statusCode/endpoint）。
    """
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = result.column_names if hasattr(result, "column_names") else []
    attributions: list[AccessErrorAttribution] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False))) if col_names else {}

        # Build dimensions: columns before the fixed aggregation cols
        dims: dict[str, str] = {}
        method_val: str | None = None
        route_val: str | None = None
        for col in col_names:
            if col in _ATTRIBUTION_FIXED_COLS:
                break
            val = row_dict.get(col, "")
            if col == "request_method":
                method_val = str(val)
            elif col == "request_route":
                route_val = str(val)
            elif col in _ATTRIBUTION_CH_TO_API:
                dims[_ATTRIBUTION_CH_TO_API[col]] = str(val)
        # Combine endpoint dimension if both method and route are present
        if method_val is not None or route_val is not None:
            dims["endpoint"] = f"{method_val or ''} {route_val or ''}".strip()

        idx_base = sum(1 for c in col_names if c not in _ATTRIBUTION_FIXED_COLS)
        # Fallback positional extraction for robustness
        error_total = int(row_dict.get("error_total", row[idx_base] if len(row) > idx_base else 0))
        affected_endpoints_raw = row_dict.get(
            "affected_endpoints", row[idx_base + 1] if len(row) > idx_base + 1 else []
        )
        affected_aics_raw = row_dict.get("affected_aics", row[idx_base + 2] if len(row) > idx_base + 2 else [])
        error_message_sample = row_dict.get("error_message_sample") or None
        first_seen_at = str(row_dict.get("first_seen_at", ""))
        last_seen_at = str(row_dict.get("last_seen_at", ""))

        attributions.append(
            AccessErrorAttribution(
                dimensions=dims,
                count=error_total,
                affected_endpoints=list(affected_endpoints_raw) if affected_endpoints_raw else [],
                affected_aics=list(affected_aics_raw) if affected_aics_raw else [],
                error_message_sample=error_message_sample,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
            )
        )
    return attributions


# ── slow-requests/top ─────────────────────────────────────────────────────────


async def run_slow_requests(
    stmt: tuple[str, dict[str, Any]],
) -> list[AccessSlowRequestItem]:
    """执行 slow-requests/top SQL → AccessSlowRequestItem 列表。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    items: list[AccessSlowRequestItem] = []
    for row in result.result_rows:
        # cols: log_id, timestamp, aic, trace_id, request_method, request_route, request_url, duration_ms, response_status
        if len(row) >= 9:
            items.append(
                AccessSlowRequestItem(
                    log_id=row[0],
                    timestamp=str(row[1]),
                    aic=row[2],
                    trace_id=row[3] or None,
                    request_method=row[4] or None,
                    request_route=row[5] or None,
                    request_url=row[6] or None,
                    duration_ms=int(row[7]),
                    response_status=int(row[8]) if row[8] else None,
                )
            )
    return items


# ── topology/query ────────────────────────────────────────────────────────────


async def run_topology_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    group_by: str,
) -> list[AccessTopologyEdge]:
    """执行 topology/query SQL → AccessTopologyEdge 列表。

    groupedBy 回填；groupBy=service 时清空 callerAic/calleeAic（C-ACCESS-QUERY-7）。
    """
    sql, params = stmt
    result = await _run_query(sql, params)
    edges: list[AccessTopologyEdge] = []
    for row in result.result_rows:
        # cols: [bucket?, caller_aic, caller_service, callee_aic, callee_service, call_count, error_count, avg_duration_ms, duration_quantiles, last_seen_at]
        offset = 0
        bucket_val: str | None = None
        if len(row) > 9:
            bucket_val = str(row[0]) if row[0] else None
            offset = 1
        caller_aic = str(row[offset]) if group_by == "aic" else ""
        caller_service = str(row[offset + 1])
        callee_aic = str(row[offset + 2]) if group_by == "aic" else ""
        callee_service = str(row[offset + 3])
        call_count = int(row[offset + 4])
        error_count = int(row[offset + 5])
        avg_ms = float(row[offset + 6])
        quantiles = row[offset + 7] if len(row) > offset + 7 else [0.0, 0.0]
        last_seen_raw = row[offset + 8] if len(row) > offset + 8 else ""
        if hasattr(last_seen_raw, "isoformat"):
            last_seen = last_seen_raw.isoformat().replace("+00:00", "Z")
        else:
            last_seen = str(last_seen_raw) if last_seen_raw else ""

        error_rate = (error_count / call_count) if call_count else 0.0
        edges.append(
            AccessTopologyEdge(
                bucket=bucket_val,
                grouped_by=group_by,
                caller_aic=caller_aic,
                caller_service=caller_service,
                callee_aic=callee_aic,
                callee_service=callee_service,
                call_count=call_count,
                error_count=error_count,
                error_rate=error_rate,
                avg_duration_ms=avg_ms,
                p95_duration_ms=float(quantiles[0]) if quantiles else 0.0,
                p99_duration_ms=float(quantiles[1]) if len(quantiles) > 1 else 0.0,
                last_seen_at=last_seen,
            )
        )
    return edges


# ── 运维：desync 重放修复（§9.3，供运维脚本调用）─────────────────────────────


async def replay_partition(partition_yyyymmdd: int) -> None:
    """从 access_events 重建派生表对应分区（C-ACCESS-RETENTION-3）。

    只能以 access_events 为源重建，不以旧派生视图互相重建。
    """
    client = await get_clickhouse_client()
    # Drop partitions in derived tables
    for derived_table in (tables.ACCESS_TRACE_SPAN, tables.ACCESS_TOPOLOGY_EDGE_5M):
        await client.command(f"ALTER TABLE {derived_table} DROP PARTITION {partition_yyyymmdd}")
    # Rebuild trace_span from access_events
    trace_cols = ", ".join(tables.TRACE_SPAN_COLUMNS)
    await client.command(
        f"INSERT INTO {tables.ACCESS_TRACE_SPAN} ({trace_cols}) "  # nosec B608
        f"SELECT {trace_cols} FROM {tables.ACCESS_EVENTS} "
        f"WHERE toYYYYMMDD(timestamp) = {partition_yyyymmdd} AND trace_id != ''"
    )
    # Rebuild topology_edge_5m from access_events（镜像 MV 逻辑，C-ACCESS-RETENTION-3）
    threshold = settings.access_error_status_threshold
    topo_cols = (
        "bucket, caller_aic, caller_service, callee_aic, callee_service, "
        "call_count_state, error_count_state, avg_duration_state, "
        "duration_quantiles_state, last_seen_state"
    )
    await client.command(
        f"INSERT INTO {tables.ACCESS_TOPOLOGY_EDGE_5M} ({topo_cols}) "  # nosec B608
        f"SELECT "
        f"    toStartOfFiveMinutes(timestamp) AS bucket, "
        f"    caller_aic, caller_service, callee_aic, callee_service, "
        f"    sumState(toUInt64(1)) AS call_count_state, "
        f"    sumState(toUInt64(response_status >= {threshold} OR error_code != '')) AS error_count_state, "
        f"    avgState(duration_ms) AS avg_duration_state, "
        f"    quantilesTDigestState(0.95, 0.99)(duration_ms) AS duration_quantiles_state, "
        f"    maxState(timestamp) AS last_seen_state "
        f"FROM {tables.ACCESS_EVENTS} "
        f"WHERE toYYYYMMDD(timestamp) = {partition_yyyymmdd} "
        f"    AND (caller_aic != '' OR caller_service != '') "
        f"    AND (callee_aic != '' OR callee_service != '') "
        f"    AND (aic = callee_aic OR callee_aic = '') "
        f"GROUP BY bucket, caller_aic, caller_service, callee_aic, callee_service"
    )
    logger.info("replay_partition completed", partition=partition_yyyymmdd)
