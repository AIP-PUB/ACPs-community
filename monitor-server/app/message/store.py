"""app/message/store.py — ClickHouse 读写执行 + DDL bootstrap（唯一直接调 ClickHouse 的文件）。

SQL 构造在 sql.py（纯函数、可单测）；执行在此（IO）——同 access/store.py 结构。
应用显式写派生表是 compactor 模型本质（设计 §3.3）：
  message_events 由 Writer 写；lifecycle/stats_5m 由 compactor INSERT...SELECT 写；
  state_snapshot 由 collector 写；应用绝不从派生表互相重建（C-MESSAGE-RETENTION-3）。
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any

import structlog

from app.core.clickhouse_client import get_clickhouse_client
from app.core.config import settings
from app.message import sql as sql_mod
from app.message import tables
from app.message.exception import ClickHouseInsertError, MessageCompactionError
from app.message.schema import (
    MessageDeadLetterView,
    MessageDestinationStateView,
    MessageEventView,
    MessageLifecycleDetailView,
    MessageLifecycleView,
    MessageThroughputPoint,
)

if TYPE_CHECKING:
    from app.message.events import EventRow

logger = structlog.get_logger(__name__)


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _coerce_ch_row(row_dict: dict[str, Any]) -> dict[str, Any]:
    """ClickHouse DateTime64 → ISO 字符串（schema 模型字段类型为 str）。"""
    from datetime import datetime

    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row_dict.items()}


def _ch_dt_to_ms(dt: Any) -> int:
    """ClickHouse DateTime/DateTime64 → Unix milliseconds（正确处理 UTC）。

    clickhouse-connect 对 DateTime('UTC') 列返回 naive datetime；naive datetime 调用
    .timestamp() 在 UTC+8 机器上会被解释为本地时间，导致结果比实际 UTC 少 8 小时。
    显式将 naive datetime 标记为 UTC 再转换，消除本地时区影响。
    """
    from datetime import datetime

    if isinstance(dt, datetime):
        aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        return int(aware.timestamp() * 1000)
    return int(dt)


def _query_settings() -> dict[str, Any]:
    return {"max_execution_time": settings.message_query_timeout_seconds}


async def _run_query(sql: str, params: dict[str, Any]) -> Any:
    client = await get_clickhouse_client()
    return await client.query(sql, parameters=params, settings=_query_settings())


def _lifecycle_row_to_view(row_dict: dict[str, Any]) -> MessageLifecycleView:
    """行字典 → MessageLifecycleView，含派生字段（设计 §6.8 注）。

    - duplicate_consumed = receive_count > 1
    - unacked = terminal_state == ''（空字符串）
    - terminal_state == '' → None（spec §4.2 应用层约定）
    - avg_ack_latency_ms = ack_latency_sum_ms / ack_sample_count（ack_sample_count=0 → None）
    """
    receive_count = int(row_dict.get("receive_count", 0))
    terminal_state_raw = row_dict.get("terminal_state", "") or ""
    unacked = terminal_state_raw == ""
    terminal_state = terminal_state_raw if terminal_state_raw else None

    ack_sum = row_dict.get("ack_latency_sum_ms")
    ack_samples = row_dict.get("ack_sample_count")
    if ack_sum is not None and ack_samples and int(ack_samples) > 0:
        avg_ack_latency_ms: float | None = float(ack_sum) / float(ack_samples)
    else:
        avg_ack_latency_ms = None

    return MessageLifecycleView(
        lifecycle_key=row_dict.get("lifecycle_key", ""),
        message_id=row_dict.get("message_id"),
        correlation_id=row_dict.get("correlation_id"),
        trace_id=row_dict.get("trace_id"),
        system=row_dict.get("system", ""),
        destination_name=row_dict.get("destination_name", ""),
        destination_kind=row_dict.get("destination_kind", ""),
        virtual_host=row_dict.get("virtual_host"),
        subscription_name=row_dict.get("subscription_name"),
        consumer_group_name=row_dict.get("consumer_group_name"),
        first_seen_at=str(row_dict.get("first_seen_at", "")),
        last_seen_at=str(row_dict.get("last_seen_at", "")),
        dead_lettered_at=str(row_dict["dead_lettered_at"]) if row_dict.get("dead_lettered_at") else None,
        producer_aics=list(row_dict.get("producer_aics") or []),
        consumer_aics=list(row_dict.get("consumer_aics") or []),
        send_count=int(row_dict.get("send_count", 0)),
        receive_count=receive_count,
        max_delivery_attempt=row_dict.get("max_delivery_attempt"),
        terminal_state=terminal_state,
        dead_lettered=bool(row_dict.get("dead_lettered", False)),
        dead_letter_reason=row_dict.get("dead_letter_reason"),
        duplicate_consumed=receive_count > 1,
        unacked=unacked,
        avg_ack_latency_ms=avg_ack_latency_ms,
    )


def _throughput_row_to_point(row_dict: dict[str, Any]) -> MessageThroughputPoint:
    """行字典 → MessageThroughputPoint（avg_ack_latency_ms = sum/count，禁 avg-of-avg）。"""
    from datetime import datetime

    bucket_raw = row_dict.get("bucket")
    if isinstance(bucket_raw, datetime):
        bucket = bucket_raw if bucket_raw.tzinfo is not None else bucket_raw.replace(tzinfo=UTC)
    elif isinstance(bucket_raw, str):
        # _coerce_ch_row 已将 datetime 转为 ISO 字符串
        try:
            parsed = datetime.fromisoformat(bucket_raw)
            bucket = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError, AttributeError:
            bucket = datetime.fromtimestamp(0, tz=UTC)
    else:
        bucket = datetime.fromtimestamp(float(bucket_raw or 0), tz=UTC)

    ack_sum = row_dict.get("ack_latency_sum_ms")
    ack_samples = row_dict.get("ack_sample_count")
    if ack_sum is not None and ack_samples and int(ack_samples) > 0:
        avg_ack: float | None = float(ack_sum) / float(ack_samples)
    else:
        avg_ack = None

    return MessageThroughputPoint(
        bucket=bucket,
        produced_count=int(row_dict.get("produced_count", 0)),
        consumed_count=int(row_dict.get("consumed_count", 0)),
        ack_count=row_dict.get("ack_count"),
        nack_count=row_dict.get("nack_count"),
        reject_count=row_dict.get("reject_count"),
        timeout_count=row_dict.get("timeout_count"),
        dead_letter_count=row_dict.get("dead_letter_count"),
        retry_count=row_dict.get("retry_count"),
        avg_ack_latency_ms=avg_ack,
    )


# ── DDL bootstrap ──────────────────────────────────────────────────────────────


async def ensure_message_schema() -> None:
    """顺序执行四表 DDL（IF NOT EXISTS，幂等）。

    建表失败抛异常（runtime 视为启动失败）。
    """
    client = await get_clickhouse_client()
    stmts = tables.all_ddl_statements(
        raw_retention_days=settings.message_raw_retention_days,
        lifecycle_retention_days=settings.message_lifecycle_retention_days,
        destination_state_retention_days=settings.message_destination_state_retention_days,
        destination_stats_retention_days=settings.message_destination_stats_retention_days,
    )
    for ddl in stmts:
        await client.command(ddl)


# ── 写侧 ───────────────────────────────────────────────────────────────────────


async def insert_events(rows: list[EventRow]) -> None:
    """原子写入 message_events 主表（C-MESSAGE-WRITE-1）。

    失败 → raise ClickHouseInsertError（Writer 据此不 commit、不推水位、不写去重标记）。
    """
    if not rows:
        return
    client = await get_clickhouse_client()
    data = [r.as_tuple() for r in rows]
    try:
        await client.insert(
            tables.MESSAGE_EVENTS,
            data,
            column_names=list(tables.INSERT_COLUMNS),
        )
    except Exception as exc:
        raise ClickHouseInsertError(f"Failed to insert {len(rows)} events: {exc}") from exc


async def insert_destination_snapshot(rows: list[dict[str, Any]]) -> None:
    """写入 message_destination_state_snapshot（C-MESSAGE-WRITE-5）。"""
    if not rows:
        return
    client = await get_clickhouse_client()
    data = [tuple(r[c] for c in tables.STATE_SNAPSHOT_COLUMNS) for r in rows]
    try:
        await client.insert(
            tables.MESSAGE_DESTINATION_STATE,
            data,
            column_names=list(tables.STATE_SNAPSHOT_COLUMNS),
        )
    except Exception as exc:
        logger.warning("insert_destination_snapshot failed", error=str(exc))
        raise


# ── Compactor 读写 ─────────────────────────────────────────────────────────────


async def fetch_affected_lifecycle_keys(
    rebuild_from_ms: int,
) -> tuple[list[tuple[str, str, str, str, str]], int | None]:
    """第一阶段：受影响 lifecycle_key 五元组 + max_observed_at。"""
    stmt = sql_mod.build_affected_lifecycle_keys(rebuild_from_ms=rebuild_from_ms)
    result = await _run_query(*stmt)
    keys: list[tuple[str, str, str, str, str]] = []
    max_obs: int | None = None
    for row in result.result_rows:
        # DISTINCT lifecycle_key, system, destination_name, destination_kind, virtual_host, max_observed_at
        if len(row) >= 6:
            sys, dn, dk, vh, lk = row[0], row[1], row[2], row[3], row[4]
            keys.append((sys, dn, dk, vh, lk))
            obs = row[5]
            if obs is not None:
                obs_ms = _ch_dt_to_ms(obs)
                if max_obs is None or obs_ms > max_obs:
                    max_obs = obs_ms
    return keys, max_obs


async def recompute_lifecycles(
    key_tuples: list[tuple[str, str, str, str, str]],
    *,
    compacted_at_ms: int,
) -> int:
    """第二阶段：INSERT...SELECT 重算 → message_lifecycle（C-MESSAGE-MODEL-1）。

    失败 → raise MessageCompactionError（compactor 不推水位）。
    """
    if not key_tuples:
        return 0
    stmt = sql_mod.build_recompute_lifecycles(key_tuples=key_tuples, compacted_at_ms=compacted_at_ms)
    try:
        await _run_query(*stmt)
    except Exception as exc:
        raise MessageCompactionError(f"recompute_lifecycles failed: {exc}") from exc
    return len(key_tuples)


async def fetch_affected_buckets(
    rebuild_from_ms: int,
) -> tuple[list[tuple[int, str, str, str, str]], int | None]:
    """Throughput Compactor 第一阶段：受影响 5min 桶五元组。"""
    stmt = sql_mod.build_affected_buckets(rebuild_from_ms=rebuild_from_ms)
    result = await _run_query(*stmt)
    buckets: list[tuple[int, str, str, str, str]] = []
    max_obs: int | None = None
    for row in result.result_rows:
        if len(row) >= 6:
            bkt_ms = _ch_dt_to_ms(row[0])
            sys, dn, dk, vh = row[1], row[2], row[3], row[4]
            buckets.append((bkt_ms, sys, dn, dk, vh))
            obs = row[5]
            if obs is not None:
                obs_ms = _ch_dt_to_ms(obs)
                if max_obs is None or obs_ms > max_obs:
                    max_obs = obs_ms
    return buckets, max_obs


async def recompute_throughput_buckets(
    bucket_tuples: list[tuple[int, str, str, str, str]],
    *,
    compacted_at_ms: int,
) -> int:
    """Throughput Compactor 第二阶段：INSERT...SELECT 重算 → message_destination_stats_5m。

    失败 → raise MessageCompactionError。
    """
    if not bucket_tuples:
        return 0
    stmt = sql_mod.build_recompute_throughput(bucket_tuples=bucket_tuples, compacted_at_ms=compacted_at_ms)
    try:
        await _run_query(*stmt)
    except Exception as exc:
        raise MessageCompactionError(f"recompute_throughput_buckets failed: {exc}") from exc
    return len(bucket_tuples)


# ── 读查询执行 ─────────────────────────────────────────────────────────────────


async def run_events_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    limit: int,
    include_raw_log: bool,
) -> list[MessageEventView]:
    """执行 events/query SQL → MessageEventView 列表。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    cols = list(tables.EVENT_VIEW_COLUMNS)
    views: list[MessageEventView] = []
    for row in result.result_rows:
        row_dict = dict(zip(cols, row, strict=False))
        if include_raw_log and len(row) > len(cols):
            row_dict["raw_log"] = row[len(cols)]
        views.append(MessageEventView.model_validate(_coerce_ch_row(row_dict)))
    return views


async def run_lifecycles_query(
    stmt: tuple[str, dict[str, Any]],
) -> list[MessageLifecycleView]:
    """执行 lifecycles/query SQL → MessageLifecycleView 列表（含派生字段）。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = list(result.column_names) if hasattr(result, "column_names") else []
    views: list[MessageLifecycleView] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False)))
        views.append(_lifecycle_row_to_view(row_dict))
    return views


async def fetch_lifecycle_by_message_id(
    stmt: tuple[str, dict[str, Any]],
) -> list[MessageLifecycleDetailView]:
    """lifecycles/{messageId}：返回去重后行集（可能 0/1/多条，service 判断）。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = list(result.column_names) if hasattr(result, "column_names") else []
    views: list[MessageLifecycleDetailView] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False)))
        receive_count = int(row_dict.get("receive_count", 0))
        terminal_state_raw = row_dict.get("terminal_state", "") or ""
        unacked = terminal_state_raw == ""
        terminal_state = terminal_state_raw if terminal_state_raw else None
        ack_sum = row_dict.get("ack_latency_sum_ms")
        ack_samples = row_dict.get("ack_sample_count")
        avg_ack: float | None = None
        if ack_sum is not None and ack_samples and int(ack_samples) > 0:
            avg_ack = float(ack_sum) / float(ack_samples)
        views.append(
            MessageLifecycleDetailView(
                lifecycle_key=row_dict.get("lifecycle_key", ""),
                message_id=row_dict.get("message_id"),
                correlation_id=row_dict.get("correlation_id"),
                trace_id=row_dict.get("trace_id"),
                system=row_dict.get("system", ""),
                destination_name=row_dict.get("destination_name", ""),
                destination_kind=row_dict.get("destination_kind", ""),
                virtual_host=row_dict.get("virtual_host"),
                subscription_name=row_dict.get("subscription_name"),
                consumer_group_name=row_dict.get("consumer_group_name"),
                first_seen_at=str(row_dict.get("first_seen_at", "")),
                last_seen_at=str(row_dict.get("last_seen_at", "")),
                dead_lettered_at=str(row_dict["dead_lettered_at"]) if row_dict.get("dead_lettered_at") else None,
                producer_aics=list(row_dict.get("producer_aics") or []),
                consumer_aics=list(row_dict.get("consumer_aics") or []),
                send_count=int(row_dict.get("send_count", 0)),
                receive_count=receive_count,
                max_delivery_attempt=row_dict.get("max_delivery_attempt"),
                terminal_state=terminal_state,
                dead_lettered=bool(row_dict.get("dead_lettered", False)),
                dead_letter_reason=row_dict.get("dead_letter_reason"),
                duplicate_consumed=receive_count > 1,
                unacked=unacked,
                avg_ack_latency_ms=avg_ack,
            )
        )
    return views


async def run_deadletters_query(
    stmt: tuple[str, dict[str, Any]],
) -> list[MessageDeadLetterView]:
    """执行 deadletters/query SQL → MessageDeadLetterView 列表。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = list(result.column_names) if hasattr(result, "column_names") else []
    views: list[MessageDeadLetterView] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False)))
        views.append(
            MessageDeadLetterView(
                lifecycle_key=row_dict.get("lifecycle_key", ""),
                message_id=row_dict.get("message_id"),
                correlation_id=row_dict.get("correlation_id"),
                trace_id=row_dict.get("trace_id"),
                system=row_dict.get("system", ""),
                destination_name=row_dict.get("destination_name", ""),
                destination_kind=row_dict.get("destination_kind", ""),
                virtual_host=row_dict.get("virtual_host"),
                dead_lettered_at=str(row_dict["dead_lettered_at"]) if row_dict.get("dead_lettered_at") else None,
                dead_letter_reason=row_dict.get("dead_letter_reason"),
                receive_count=int(row_dict.get("receive_count", 0)),
                max_delivery_attempt=row_dict.get("max_delivery_attempt"),
                producer_aics=list(row_dict.get("producer_aics") or []),
                consumer_aics=list(row_dict.get("consumer_aics") or []),
            )
        )
    return views


async def run_destinations_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    group_by: list[str],
) -> tuple[list[MessageDestinationStateView], list[str], dict[str, Any]]:
    """执行 destinations/query SQL → (视图列表, partial_data_fields, sample_coverage)。

    Nullable 指标全缺的字段收入 partial_data_fields（C-MESSAGE-QUERY-5）。
    """
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = list(result.column_names) if hasattr(result, "column_names") else []
    views: list[MessageDestinationStateView] = []
    nullable_metrics = (
        "visible_messages",
        "inflight_messages",
        "delayed_messages",
        "dead_letter_messages",
        "oldest_message_age_seconds",
        "active_consumers",
        "size_bytes",
    )
    null_counts: dict[str, int] = dict.fromkeys(nullable_metrics, 0)
    total_rows = 0

    for row in result.result_rows:
        total_rows += 1
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False)))
        for m in nullable_metrics:
            if row_dict.get(m) is None:
                null_counts[m] += 1
        views.append(
            MessageDestinationStateView(
                captured_at=str(row_dict.get("captured_at", "")),
                system=row_dict.get("system"),
                destination_name=row_dict.get("destination_name"),
                destination_kind=row_dict.get("destination_kind"),
                virtual_host=row_dict.get("virtual_host"),
                visible_messages=row_dict.get("visible_messages"),
                inflight_messages=row_dict.get("inflight_messages"),
                delayed_messages=row_dict.get("delayed_messages"),
                dead_letter_messages=row_dict.get("dead_letter_messages"),
                oldest_message_age_seconds=row_dict.get("oldest_message_age_seconds"),
                active_consumers=row_dict.get("active_consumers"),
                size_bytes=row_dict.get("size_bytes"),
            )
        )

    partial_data_fields: list[str] = []
    if total_rows > 0:
        for m in nullable_metrics:
            if null_counts[m] == total_rows:
                partial_data_fields.append(m)

    return views, partial_data_fields, {}


async def run_throughput_query(
    stmt: tuple[str, dict[str, Any]],
    *,
    step_seconds: int,
) -> list[MessageThroughputPoint]:
    """执行 destinations/throughput SQL → MessageThroughputPoint 列表（avg 在此重算，禁 avg-of-avg）。"""
    sql, params = stmt
    result = await _run_query(sql, params)
    col_names: list[str] = list(result.column_names) if hasattr(result, "column_names") else []
    points: list[MessageThroughputPoint] = []
    for row in result.result_rows:
        row_dict = _coerce_ch_row(dict(zip(col_names, row, strict=False)))
        points.append(_throughput_row_to_point(row_dict))
    return points
