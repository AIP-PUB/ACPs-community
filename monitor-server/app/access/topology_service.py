"""app/access/topology_service.py — APM 拓扑端点（读 access_topology_edge_5m）。

实现设计 §6.5。只读派生聚合表，绝不回退全量事件现场聚合（C-ACCESS-QUERY-5）。
统一 *Merge + 按目标维度重 GROUP BY（C-ACCESS-QUERY-6）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

import structlog

from app.access import sql as sql_mod
from app.access import store
from app.access.cursor import decode_cursor, encode_cursor, query_fingerprint, to_keyset_bound
from app.access.exception import ReadModelLaggingError
from app.access.filters import TOPOLOGY_FILTER_FIELDS, compile_filter, validate_sort
from app.access.freshness import apply_degrade_policy, build_meta, evaluate_freshness
from app.access.metrics import metrics as access_metrics
from app.access.planner import (
    align_topology_buckets,
    assert_within_retention,
    require_time_range,
    resolve_page_limit,
    validate_topology_group_by,
)
from app.access.schema import AccessTopologyEdge, AccessTopologyQueryRequest
from app.core.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_topology(
    redis: Redis,
    req: AccessTopologyQueryRequest,
) -> tuple[list[AccessTopologyEdge], AMPResponseMeta]:
    """topology/query（*Merge 重聚合，设计 §6.5）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_topology_retention_days, now_ms=now_ms)
    from_b, to_b = align_topology_buckets(from_ms, to_ms)

    group_by = validate_topology_group_by(req.group_by)
    edge_where = compile_filter(req.filter, api="topology", fields=TOPOLOGY_FILTER_FIELDS)
    sort = validate_sort(req.sort, api="topology")
    limit = resolve_page_limit(req.page)

    fp = query_fingerprint(
        api="topology",
        time_range=req.time_range,
        filter_=req.filter,
        sort=req.sort,
        extra={"group_by": group_by},
    )
    cursor_state = decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = to_keyset_bound(cursor_state, sort, api="topology") if cursor_state else None

    bucket_params = {"from_bucket_ms": from_b, "to_bucket_ms": to_b}
    # collapse_buckets=True → 跨时间聚合（不含 bucket 维度）
    # collapse_buckets=False → 按原始 5 分钟桶分组（时序视图）
    bucket_expr = None if req.collapse_buckets else "toStartOfFiveMinutes"
    stmt = sql_mod.build_topology_query(
        group_by=group_by,
        bucket_expr=bucket_expr,
        edge_where=edge_where,
        bucket_params=bucket_params,
        having_min_call=req.min_call_count,
        sort=sort,
        keyset=keyset,
        limit=limit,
    )

    try:
        rows = await store.run_topology_query(stmt, group_by=group_by)
    except Exception as exc:
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_topology_latency_ms", (monotonic() - _t0) * 1000)

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            sort_values=[last.call_count],
            tiebreak={"caller_aic": last.caller_aic, "callee_aic": last.callee_aic},
            fingerprint=fp,
        )

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta
