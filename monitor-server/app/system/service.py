"""app/system/service.py — events/query 编排（设计 §6.1）。

薄编排：planner（护栏）→ cursor（指纹/解码）→ dsl（编译）→ store（PIT 搜索）→ freshness（meta）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from app.system import cursor as cursor_mod
from app.system import dsl, freshness, planner, store
from app.system.exception import CursorInvalidError, OpenSearchQueryError, ReadModelLaggingError
from app.system.metrics import AMP_SYSTEM_EVENTS_QUERY_LATENCY_MS, metrics
from app.system.schema import SystemEventQueryRequest, SystemEventView

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_events(
    redis: Redis,
    req: SystemEventQueryRequest,
    *,
    principal: Any = None,
) -> tuple[list[SystemEventView], AMPResponseMeta]:
    """events/query 编排（设计 §6.1 执行路径）。

    步骤：
    1. 时间范围校验 + 保留窗口检查
    2. 编译过滤器 + keyword 护栏 + 排序 + 分页
    3. 游标指纹 + 解码
    4. PIT 生命周期：首页 open_pit，翻页复用游标内 PIT id
    5. 构建 DSL + 执行 search_events
    6. has_more / nextCursor / 末页 close_pit
    7. freshness meta 组装
    """
    from app.core.config import settings

    now_ms = int(time.time() * 1000)

    # 1. 时间范围
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(
        from_ms,
        archive_retention_days=settings.system_archive_retention_days,
        now_ms=now_ms,
    )

    # 2. 过滤器 / keyword / 排序 / 分页
    filter_clauses = dsl.compile_filter(req.filter)
    has_other_filter = bool(filter_clauses)
    window_seconds = max(1, (to_ms - from_ms) // 1000)
    planner.validate_keyword(
        req.keyword,
        min_length=settings.system_keyword_min_length,
        has_other_filter=has_other_filter,
        window_seconds=window_seconds,
        keyword_only_max_window_seconds=settings.system_keyword_only_max_window_seconds,
    )
    resolved_sort = planner.validate_sort(req.sort)
    limit = planner.resolve_page_limit(req.page)
    scope_clauses = planner.inject_scope_filter(principal=principal)

    # 3. 游标指纹 + 解码
    fp = cursor_mod.query_fingerprint(
        time_range=req.time_range,
        filter_=req.filter,
        sort=req.sort,
        keyword=req.keyword,
    )
    cursor_state = cursor_mod.decode_cursor(req.page.cursor, expected_fingerprint=fp)

    # 4. PIT 生命周期
    if cursor_state is not None:
        pit_id = cursor_state.pit_id
        search_after = cursor_state.search_after
    else:
        pit_id = await store.open_pit(keep_alive=settings.system_pit_keep_alive)
        search_after = None

    # 5. 构建 DSL + 搜索
    keyword_query = dsl.build_keyword_query(req.keyword)
    time_clause = dsl.build_time_range_clause(from_ms=from_ms, to_ms=to_ms)
    sort_dsl = dsl.build_sort(resolved_sort)
    search_body = dsl.build_search_body(
        filter_clauses=filter_clauses,
        keyword_query=keyword_query,
        time_clause=time_clause,
        scope_clauses=scope_clauses,
        sort=sort_dsl,
        search_after=search_after,
        size=limit + 1,  # +1 探 has_more
    )

    start_ms = int(time.time() * 1000)
    try:
        hits = await store.search_events(
            search_body,
            pit_id=pit_id,
            keep_alive=settings.system_pit_keep_alive,
            include_raw_log=req.include_raw_log,
        )
    except OpenSearchQueryError as exc:
        err_str = str(exc).lower()
        if "pit" in err_str or "not_found" in err_str:
            raise CursorInvalidError(
                "The pagination cursor's PIT handle has expired. Please restart pagination."
            ) from exc
        raise ReadModelLaggingError("OpenSearch query failed. Please retry later.") from exc
    finally:
        elapsed_ms = int(time.time() * 1000) - start_ms
        metrics.observe(AMP_SYSTEM_EVENTS_QUERY_LATENCY_MS, float(elapsed_ms))

    # 6. has_more / nextCursor / 末页 close_pit
    has_more = len(hits) > limit
    hits = hits[:limit]
    rows: list[SystemEventView] = [h.view for h in hits]

    if has_more:
        next_cursor: str | None = cursor_mod.encode_cursor(
            pit_id=pit_id,
            search_after=store.extract_search_after(hits[-1]),
            fingerprint=fp,
        )
    else:
        next_cursor = None
        await store.close_pit(pit_id)

    # 7. freshness meta
    freshness_view = await freshness.evaluate_freshness(
        redis,
        now_ms=now_ms,
        lagging_threshold_ms=settings.system_lagging_threshold_ms,
    )
    strict_503 = settings.system_lagging_response_mode == "503"
    partial_flag = freshness.apply_degrade_policy(freshness_view, strict_503=strict_503)
    meta = freshness.build_meta(
        freshness_view,
        now_ms=now_ms,
        next_cursor=next_cursor,
        partial=partial_flag or None,
        elapsed_ms=elapsed_ms,
    )

    return rows, meta
