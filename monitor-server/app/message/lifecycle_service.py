"""app/message/lifecycle_service.py — Reliability Profile 查询编排（设计 §6.19）。

三端点共用 lifecycle compactor 水位：
  lifecycles/query、lifecycles/{messageId}（裸资源）、deadletters/query。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.message import cursor as cursor_mod
from app.message import filters, freshness, planner, store
from app.message import sql as sql_mod
from app.message.exception import LifecycleAmbiguousError, MessageNotFoundError, ReadModelLaggingError
from app.message.filters import split_lifecycle_where
from app.message.schema import (
    MessageDeadletterQueryRequest,
    MessageDeadLetterView,
    MessageLifecycleDetailView,
    MessageLifecycleQueryRequest,
    MessageLifecycleView,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_lifecycles(
    redis: Redis,
    req: MessageLifecycleQueryRequest,
) -> tuple[list[MessageLifecycleView], AMPResponseMeta]:
    """lifecycles/query — 两层 argMax，lifecycle compactor 水位（设计 §6.2）。"""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(from_ms, retention_days=settings.message_lifecycle_retention_days, now_ms=now_ms)
    planner.require_lifecycle_selectivity(filter_=req.filter, time_range=req.time_range)

    inner_where, outer_where = split_lifecycle_where(req.filter, api="lifecycles")
    sort = filters.validate_sort(req.sort, api="lifecycles")
    limit = planner.resolve_page_limit(req.page)

    fp = cursor_mod.query_fingerprint(
        api="lifecycles", time_range=req.time_range, filter_=req.filter, sort=req.sort, extra={}
    )
    cursor_state = cursor_mod.decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = cursor_mod.to_keyset_bound(cursor_state, sort, api="lifecycles") if cursor_state else None

    time_params: dict[str, Any] = {"_from": from_ms, "_to": to_ms}
    stmt = sql_mod.build_lifecycles_query(
        inner_where=inner_where,
        outer_where=outer_where,
        time_params=time_params,
        lifecycle_retention_days=settings.message_lifecycle_retention_days,
        sort=sort,
        keyset=keyset,
        limit=limit,
    )

    try:
        rows = await store.run_lifecycles_query(stmt)
    except Exception as exc:
        logger.exception("lifecycles query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed.") from exc

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = cursor_mod.encode_cursor(
            sort_values=[last.last_seen_at],
            tiebreak={"last_seen_at": last.last_seen_at, "lifecycle_key": last.lifecycle_key},
            fingerprint=fp,
        )

    fv = await freshness.evaluate_freshness(redis, read_model="lifecycle", now_ms=now_ms)
    partial = freshness.apply_degrade_policy(fv, strict_503=(settings.message_lagging_response_mode == "503"))
    meta = freshness.build_meta(fv, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta


async def get_lifecycle_by_message_id(
    redis: Redis,
    message_id: str,
    *,
    system: str | None = None,
    destination_name: str | None = None,
    destination_kind: str | None = None,
    virtual_host: str | None = None,
) -> tuple[MessageLifecycleDetailView, dict[str, str]]:
    """lifecycles/{messageId} — 精确拉取，裸资源响应（设计 §6.3）。"""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    lifecycle_key = f"mid:{message_id}"

    stmt = sql_mod.build_lifecycle_by_message_id_query(
        lifecycle_key=lifecycle_key,
        system=system,
        destination_name=destination_name,
        destination_kind=destination_kind,
        virtual_host=virtual_host,
        lifecycle_retention_days=settings.message_lifecycle_retention_days,
        now_ms=now_ms,
    )
    rows = await store.fetch_lifecycle_by_message_id(stmt)

    fv = await freshness.evaluate_freshness(redis, read_model="lifecycle", now_ms=now_ms)

    if len(rows) == 0:
        raise MessageNotFoundError(message_id)
    if len(rows) > 1:
        raise LifecycleAmbiguousError()

    headers = freshness.freshness_headers(fv)
    return rows[0], headers


async def query_deadletters(
    redis: Redis,
    req: MessageDeadletterQueryRequest,
) -> tuple[list[MessageDeadLetterView], AMPResponseMeta]:
    """deadletters/query — 两层 argMax + 外层 dead_lettered=1（设计 §6.5）。"""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(from_ms, retention_days=settings.message_lifecycle_retention_days, now_ms=now_ms)
    planner.require_lifecycle_selectivity(filter_=req.filter, time_range=req.time_range)

    inner_where, outer_where = split_lifecycle_where(req.filter, api="deadletters")
    sort = filters.validate_sort(req.sort, api="deadletters")
    limit = planner.clamp_deadletter_n(
        planner.resolve_page_limit(req.page),
        hard_max=settings.message_deadletter_query_max_n,
    )

    fp = cursor_mod.query_fingerprint(
        api="deadletters", time_range=req.time_range, filter_=req.filter, sort=req.sort, extra={}
    )
    cursor_state = cursor_mod.decode_cursor(req.page.cursor if req.page else None, expected_fingerprint=fp)
    keyset = cursor_mod.to_keyset_bound(cursor_state, sort, api="deadletters") if cursor_state else None

    time_params: dict[str, Any] = {"_from": from_ms, "_to": to_ms}
    stmt = sql_mod.build_deadletters_query(
        inner_where=inner_where,
        outer_where=outer_where,
        time_params=time_params,
        lifecycle_retention_days=settings.message_lifecycle_retention_days,
        sort=sort,
        keyset=keyset,
        limit=limit,
    )

    try:
        rows = await store.run_deadletters_query(stmt)
    except Exception as exc:
        logger.exception("deadletters query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed.") from exc

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = cursor_mod.encode_cursor(
            sort_values=[last.dead_lettered_at or ""],
            tiebreak={"dead_lettered_at": last.dead_lettered_at or "", "lifecycle_key": last.lifecycle_key},
            fingerprint=fp,
        )

    fv = await freshness.evaluate_freshness(redis, read_model="lifecycle", now_ms=now_ms)
    partial = freshness.apply_degrade_policy(fv, strict_503=(settings.message_lagging_response_mode == "503"))
    meta = freshness.build_meta(fv, now_ms=now_ms, next_cursor=next_cursor, partial=partial or None)
    return rows, meta
