"""app/message/service.py — events/query 编排（设计 §6.18）。

薄编排层：planner → cursor → filters → sql → store → freshness。
不含 SQL 字符串，不直接调 clickhouse_client。
返回 tuple[list[View], AMPResponseMeta]；信封 AMPQueryResponse 在 api 层组装。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.message import cursor as cursor_mod
from app.message import filters, freshness, planner, store
from app.message import sql as sql_mod
from app.message.exception import ReadModelLaggingError
from app.message.filters import EVENT_FILTER_FIELDS
from app.message.schema import MessageEventView, MessageQueryRequest

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_events(
    redis: Redis,
    req: MessageQueryRequest,
) -> tuple[list[MessageEventView], AMPResponseMeta]:
    """events/query — 直接扫 message_events，keyset 分页（设计 §6.1）。"""
    _t0 = monotonic()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(from_ms, retention_days=settings.message_raw_retention_days, now_ms=now_ms)

    where = filters.compile_filter(req.filter, api="events", fields=EVENT_FILTER_FIELDS)
    sort = filters.validate_sort(req.sort, api="events")
    limit = planner.resolve_page_limit(req.page)

    fp = cursor_mod.query_fingerprint(
        api="events", time_range=req.time_range, filter_=req.filter, sort=req.sort, extra={}
    )
    cursor_state = cursor_mod.decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = cursor_mod.to_keyset_bound(cursor_state, sort, api="events") if cursor_state else None

    include_raw = bool(req.include_raw_log and settings.message_raw_log_enabled)
    time_params: dict[str, Any] = {"_from": from_ms, "_to": to_ms}
    stmt = sql_mod.build_events_query(
        where=where,
        time_params=time_params,
        sort=sort,
        keyset=keyset,
        limit=limit,
        include_raw_log=include_raw,
    )

    try:
        rows = await store.run_events_query(stmt, limit=limit, include_raw_log=include_raw)
    except Exception as exc:
        logger.exception("events query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed.") from exc
    finally:
        elapsed_ms = (monotonic() - _t0) * 1000
        logger.debug("query_events", elapsed_ms=elapsed_ms)

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = cursor_mod.encode_cursor(
            sort_values=[last.timestamp],
            tiebreak={"timestamp": last.timestamp, "log_id": last.log_id},
            fingerprint=fp,
        )

    fv = await freshness.evaluate_freshness(redis, read_model="events", now_ms=now_ms)
    partial = freshness.apply_degrade_policy(fv, strict_503=(settings.message_lagging_response_mode == "503"))
    meta = freshness.build_meta(fv, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta
