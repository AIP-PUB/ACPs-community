"""app/message/destination_service.py — Destination Profile 查询编排（设计 §6.4 / §6.6）。

两端点：
  destinations/query  — 目的地状态快照查询。
  destinations/throughput — 吞吐时序裸资源（无 meta，只含 freshness headers）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import settings
from app.message import filters, freshness, planner, store
from app.message import sql as sql_mod
from app.message.exception import ReadModelLaggingError, StateSnapshotUnavailableError
from app.message.schema import (
    MessageDestinationStateQueryRequest,
    MessageDestinationStateView,
    MessageThroughputRequest,
    MessageThroughputSeries,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)


async def query_destination_states(
    redis: Redis,
    req: MessageDestinationStateQueryRequest,
) -> tuple[list[MessageDestinationStateView], AMPResponseMeta]:
    """destinations/query — 最新快照 + 可选聚合（设计 §6.4）。"""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(
        from_ms,
        retention_days=settings.message_destination_state_retention_days,
        now_ms=now_ms,
    )

    group_by = planner.validate_destination_group_by(req.group_by)
    edge_where = filters.compile_filter(req.filter, api="destinations", fields=filters.DESTINATION_FILTER_FIELDS)
    time_params: dict[str, Any] = {"_from": from_ms, "_to": to_ms}

    stmt = sql_mod.build_destinations_query(
        edge_where=edge_where,
        time_params=time_params,
        group_by=group_by,
    )

    try:
        views, partial_fields, _ = await store.run_destinations_query(stmt, group_by=group_by)
    except Exception as exc:
        logger.exception("destinations query failed", exc_info=exc)
        raise ReadModelLaggingError("ClickHouse query failed.") from exc

    fv = await freshness.evaluate_freshness(redis, read_model="state", now_ms=now_ms)

    if not views:
        raise StateSnapshotUnavailableError()

    partial = freshness.apply_degrade_policy(fv, strict_503=(settings.message_lagging_response_mode == "503"))
    meta = freshness.build_meta(
        fv,
        now_ms=now_ms,
        next_cursor=None,
        partial=partial or None,
        partial_data_fields=partial_fields if partial_fields else None,
    )
    return views, meta


async def get_throughput(
    redis: Redis,
    req: MessageThroughputRequest,
) -> tuple[MessageThroughputSeries, dict[str, str]]:
    """destinations/throughput — 5-min 桶 argMax + 可选二次聚合（设计 §6.6）。"""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    from_ms, to_ms = planner.require_time_range(req.time_range)
    planner.assert_within_retention(
        from_ms,
        retention_days=settings.message_destination_stats_retention_days,
        now_ms=now_ms,
    )

    system, destination_name = planner.require_throughput_destination(
        system=req.system,
        destination_name=req.destination_name,
    )
    step_seconds = planner.parse_throughput_step(req.step, from_ms=from_ms, to_ms=to_ms)
    time_params: dict[str, Any] = {"_from": from_ms, "_to": to_ms}

    stmt = sql_mod.build_throughput_query(
        system=system,
        destination_name=destination_name,
        destination_kind=req.destination_kind,
        virtual_host=req.virtual_host,
        time_params=time_params,
        step_seconds=step_seconds,
    )

    points = await store.run_throughput_query(stmt, step_seconds=step_seconds)

    fv = await freshness.evaluate_freshness(redis, read_model="throughput", now_ms=now_ms)
    headers = freshness.freshness_headers(fv)

    series = MessageThroughputSeries(
        system=system,
        destination_name=destination_name,
        destination_kind=req.destination_kind,
        virtual_host=req.virtual_host,
        points=points,
    )
    return series, headers
