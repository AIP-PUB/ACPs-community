"""app/access/service.py — Query Provider: events / operations / errors / slow（读 access_events）。

实现设计 §6.1/§6.2/§6.6/§6.7。C-ACCESS-QUERY-2：以 access_events 为输入，现算聚合。
统一 CH 异常处理：顶层捕获查询/超时异常，转 ReadModelLaggingError(503)。
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
from app.access.filters import EVENT_FILTER_FIELDS, compile_filter, validate_sort
from app.access.freshness import apply_degrade_policy, build_meta, evaluate_freshness
from app.access.metrics import metrics as access_metrics
from app.access.planner import (
    assert_within_retention,
    clamp_top_n,
    require_time_range,
    resolve_page_limit,
    validate_attribution_group_by,
    validate_operations_group_by,
)
from app.access.schema import (
    AccessErrorAttribution,
    AccessErrorAttributionRequest,
    AccessEventView,
    AccessOperationQueryRequest,
    AccessOperationSummary,
    AccessQueryRequest,
    AccessSlowRequestItem,
    AccessSlowRequestRequest,
)
from app.core.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_events(
    redis: Redis,
    req: AccessQueryRequest,
) -> tuple[list[AccessEventView], AMPResponseMeta]:
    """events/query（扫描 access_events，keyset 分页，设计 §6.2）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_raw_retention_days, now_ms=now_ms)

    where = compile_filter(req.filter, api="events", fields=EVENT_FILTER_FIELDS)
    sort = validate_sort(req.sort, api="events")
    limit = resolve_page_limit(req.page)

    fp = query_fingerprint(api="events", time_range=req.time_range, filter_=req.filter, sort=req.sort, extra={})
    cursor_state = decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = to_keyset_bound(cursor_state, sort, api="events") if cursor_state else None

    include_raw = bool(req.include_raw_log and settings.access_raw_log_enabled)
    time_params = {"from_ms": from_ms, "to_ms": to_ms}
    stmt = sql_mod.build_events_query(
        where=where, time_params=time_params, sort=sort, keyset=keyset, limit=limit, include_raw_log=include_raw
    )

    try:
        rows = await store.run_events_query(stmt, limit=limit, include_raw_log=include_raw)
    except Exception as exc:
        logger.exception("access events query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_events_latency_ms", (monotonic() - _t0) * 1000)

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            sort_values=[getattr(last, sort[0].column_or_alias, None) if sort else None],
            tiebreak={"timestamp": last.timestamp, "log_id": last.log_id},
            fingerprint=fp,
        )

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta


async def query_operations(
    redis: Redis,
    req: AccessOperationQueryRequest,
) -> tuple[list[AccessOperationSummary], AMPResponseMeta]:
    """operations/query（聚合 access_events，设计 §6.1）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_raw_retention_days, now_ms=now_ms)

    group_by = validate_operations_group_by(req.group_by)
    from app.access.planner import parse_bucket_size

    bucket_expr = parse_bucket_size(req.bucket_size, collapse=req.collapse_buckets)
    where = compile_filter(req.filter, api="operations", fields=EVENT_FILTER_FIELDS)
    sort = validate_sort(req.sort, api="operations")
    limit = resolve_page_limit(req.page)

    fp = query_fingerprint(
        api="operations",
        time_range=req.time_range,
        filter_=req.filter,
        sort=req.sort,
        extra={"group_by": group_by, "bucket_size": bucket_expr},
    )
    cursor_state = decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = to_keyset_bound(cursor_state, sort, api="operations") if cursor_state else None

    time_params = {"from_ms": from_ms, "to_ms": to_ms}
    stmt = sql_mod.build_operations_query(
        group_by=group_by,
        bucket_expr=bucket_expr,
        where=where,
        time_params=time_params,
        error_status_threshold=settings.access_error_status_threshold,
        having_min_request=req.min_request_count,
        sort=sort,
        keyset=keyset,
        limit=limit,
    )

    try:
        rows = await store.run_operations_query(stmt, group_by=group_by)
    except Exception as exc:
        logger.exception("access operations query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_operations_latency_ms", (monotonic() - _t0) * 1000)

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        sort_val = getattr(last, sort[0].column_or_alias, None) if sort else last.request_count
        next_cursor = encode_cursor(
            sort_values=[sort_val],
            tiebreak={"last_seen_at": last.last_seen_at},
            fingerprint=fp,
        )

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta


async def query_error_attribution(
    redis: Redis,
    req: AccessErrorAttributionRequest,
) -> tuple[list[AccessErrorAttribution], AMPResponseMeta]:
    """errors/attribution（两段式 CTE，设计 §6.6）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_raw_retention_days, now_ms=now_ms)

    group_dims = validate_attribution_group_by(req.group_by)
    where = compile_filter(req.filter, api="errors", fields=EVENT_FILTER_FIELDS)
    top_n = clamp_top_n(req.top_n, default=20, hard_max=settings.access_error_attribution_max_n)

    time_params = {"from_ms": from_ms, "to_ms": to_ms}
    stmt = sql_mod.build_error_attribution_query(
        group_dims=group_dims,
        where=where,
        time_params=time_params,
        error_status_threshold=settings.access_error_status_threshold,
        top_n=top_n,
    )

    try:
        rows = await store.run_error_attribution(stmt)
    except Exception as exc:
        logger.exception("access error attribution query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_errors_latency_ms", (monotonic() - _t0) * 1000)

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, partial=partial or None)
    return rows, meta


async def query_slow_requests(
    redis: Redis,
    req: AccessSlowRequestRequest,
) -> tuple[list[AccessSlowRequestItem], AMPResponseMeta]:
    """slow-requests/top（按耗时倒序 TopN，设计 §6.7）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_raw_retention_days, now_ms=now_ms)

    where = compile_filter(req.filter, api="slow", fields=EVENT_FILTER_FIELDS)
    top_n = clamp_top_n(req.top_n, default=20, hard_max=settings.access_slow_top_max_n)

    time_params = {"from_ms": from_ms, "to_ms": to_ms}
    stmt = sql_mod.build_slow_requests_query(
        where=where,
        time_params=time_params,
        min_duration_ms=req.min_duration_ms,
        top_n=top_n,
        keyset=None,
    )

    try:
        rows = await store.run_slow_requests(stmt)
    except Exception as exc:
        logger.exception("access slow requests query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_slow_requests_latency_ms", (monotonic() - _t0) * 1000)

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, partial=partial or None)
    return rows, meta
