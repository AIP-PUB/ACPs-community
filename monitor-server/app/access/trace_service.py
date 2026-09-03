"""app/access/trace_service.py — APM trace 端点（读 access_trace_span）。

实现设计 §6.3（traces/query 两段式）+ §6.4（traces/{traceId} 裸资源响应）。
C-ACCESS-QUERY-3/4/5/11/13
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

import structlog

from app.access import sql as sql_mod
from app.access import store
from app.access.cursor import decode_cursor, encode_cursor, query_fingerprint, to_keyset_bound
from app.access.exception import ReadModelLaggingError, TraceNotFoundError
from app.access.filters import TRACE_SPAN_FILTER_FIELDS, compile_filter, validate_sort
from app.access.freshness import (
    apply_degrade_policy,
    build_meta,
    evaluate_freshness,
    freshness_headers,
)
from app.access.metrics import metrics as access_metrics
from app.access.planner import (
    assert_within_retention,
    require_time_range,
    resolve_page_limit,
    trace_partition_expand_hours,
)
from app.access.schema import (
    AccessTraceQueryRequest,
    AccessTraceSummary,
    AccessTraceSummaryMeta,
    AccessTraceView,
)
from app.access.sql import TraceLevelHaving
from app.core.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_traces(
    redis: Redis,
    req: AccessTraceQueryRequest,
) -> tuple[list[AccessTraceSummary], AMPResponseMeta]:
    """traces/query（两段式：matching_traces + summary 聚合，设计 §6.3）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = require_time_range(req.time_range)
    assert_within_retention(from_ms, retention_days=settings.access_raw_retention_days, now_ms=now_ms)

    span_where = compile_filter(req.filter, api="traces", fields=TRACE_SPAN_FILTER_FIELDS)
    sort = validate_sort(req.sort, api="traces")
    limit = resolve_page_limit(req.page)
    threshold = settings.access_error_status_threshold
    expand_hours = trace_partition_expand_hours(configured=settings.access_trace_max_duration_hours)
    trace_having = _compile_trace_level_having(
        req.has_error, req.min_trace_duration_ms, req.max_trace_duration_ms, threshold
    )

    fp = query_fingerprint(api="traces", time_range=req.time_range, filter_=req.filter, sort=req.sort, extra={})
    cursor_state = decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = to_keyset_bound(cursor_state, sort, api="traces") if cursor_state else None

    time_params = {"from_ms": from_ms, "to_ms": to_ms}
    stmt = sql_mod.build_traces_query(
        span_where=span_where,
        time_params=time_params,
        error_status_threshold=threshold,
        trace_level_having=trace_having,
        trace_max_duration_hours=expand_hours,
        keyset=keyset,
        limit=limit,
    )

    try:
        rows = await store.run_traces_query(stmt)
    except Exception as exc:
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    finally:
        access_metrics.observe("amp_access_query_traces_latency_ms", (monotonic() - _t0) * 1000)

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            sort_values=[last.last_seen_at],
            tiebreak={"last_seen_at": last.last_seen_at, "trace_id": last.trace_id},
            fingerprint=fp,
        )

    view = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(view, strict_503=(settings.access_lagging_response_mode == "503"))
    meta = build_meta(view, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta


async def get_trace(
    redis: Redis,
    trace_id: str,
    *,
    include_events: bool,
) -> tuple[AccessTraceView, dict[str, str]]:
    """traces/{traceId} 裸资源响应（设计 §6.4，C-ACCESS-QUERY-11/13）。"""
    # 可选预检：trace_hint 告知 false 时仍继续查 CH（hint 不决定 404，C-ACCESS-MODEL-5）
    if settings.access_trace_seen_hint_enabled:
        from app.access import trace_hint

        seen = await trace_hint.maybe_seen(redis, trace_id)
        if seen:
            access_metrics.inc("amp_access_trace_hint_hits_total")
        else:
            access_metrics.inc("amp_access_trace_hint_misses_total")
            logger.debug("get_trace: hint miss, querying CH anyway", trace_id=trace_id)

    spans, truncated = None, False
    try:
        spans, truncated = await store.fetch_trace_spans(trace_id, trace_max_spans=settings.access_trace_max_spans)
    except Exception as exc:
        raise ReadModelLaggingError("ClickHouse query failed") from exc
    if not spans:
        raise TraceNotFoundError(trace_id)

    access_metrics.inc("amp_access_trace_spans_returned_total", by=len(spans))

    # 应用层重建 summary
    root_span_id: str | None = None
    error_count = 0
    threshold = settings.access_error_status_threshold
    for span in spans:
        if not span.parent_span_id:
            root_span_id = span.span_id
        if span.error_code or (span.response_status and span.response_status >= threshold):
            error_count += 1
    first_ts = str(spans[0].timestamp) if spans else ""
    last_ts = str(spans[-1].timestamp) if spans else ""

    # Calculate trace duration from first span start to last span end (start + duration)
    total_dur = 0
    try:
        from datetime import datetime as _dt

        def _ts_to_ms(ts: str) -> int:
            return int(_dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)

        span_start_ms = _ts_to_ms(first_ts) if first_ts else 0
        # last span end = last span start + last span duration_ms
        last_span_start_ms = _ts_to_ms(spans[-1].timestamp) if spans else span_start_ms
        last_span_end_ms = last_span_start_ms + (spans[-1].duration_ms if spans else 0)
        total_dur = max(0, last_span_end_ms - span_start_ms)
    except Exception:
        total_dur = 0

    summary = AccessTraceSummaryMeta(
        first_seen_at=first_ts,
        last_seen_at=last_ts,
        duration_ms=total_dur,
        total_spans=len(spans),
        error_count=error_count,
        root_span_id=root_span_id,
        root_aic=spans[0].aic if spans and root_span_id == spans[0].span_id else None,
    )

    events = None
    if include_events:
        try:
            events = await store.fetch_trace_events(trace_id)
        except Exception:
            logger.warning("get_trace: failed to fetch events", trace_id=trace_id, exc_info=True)

    view = AccessTraceView(trace_id=trace_id, spans=spans, events=events, summary=summary)

    fview = await evaluate_freshness(redis)
    headers = freshness_headers(fview)
    if truncated:
        headers["AMP-Trace-Truncated"] = "true"

    return view, headers


def _compile_trace_level_having(
    has_error: bool | None,
    min_dur: int | None,
    max_dur: int | None,
    threshold: int,
) -> TraceLevelHaving:
    """编译 trace 级 HAVING 条件（C-ACCESS-QUERY-4，仅在 summary 聚合后应用）。"""
    parts: list[str] = []
    params: dict[str, int | str] = {}
    i = 0

    if has_error is True:
        parts.append(f"countIf(error_code != '' OR response_status >= {threshold}) > 0")
    elif has_error is False:
        parts.append(f"countIf(error_code != '' OR response_status >= {threshold}) = 0")

    if min_dur is not None:
        params[f"th_min_dur_{i}"] = min_dur
        parts.append(f"duration_ms >= {{th_min_dur_{i}:UInt32}}")
        i += 1

    if max_dur is not None:
        params[f"th_max_dur_{i}"] = max_dur
        parts.append(f"duration_ms <= {{th_max_dur_{i}:UInt32}}")

    sql = " AND ".join(parts)
    return TraceLevelHaving(sql=sql, params=params)
