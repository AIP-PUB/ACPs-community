"""app/message/sql.py — 全端点 SQL + Compactor SQL 构造（纯函数，零 I/O）。

TDD 核心靶点：把过滤、两层 argMax、compactor INSERT...SELECT、桶聚合、keyset 渲染为
参数化 SQL。所有函数返回 (sql, params)，由 store.py 执行。

标识符（表名/列名）只来自 tables.py 常量；值只走 {name:Type} 绑定，杜绝注入。
partition/offset 列在 SQL 文本中加反引号（ClickHouse 保留字，§4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.message.tables import (
    EVENT_VIEW_COLUMNS,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_LOGICAL_KEY,
    LIFECYCLE_READ_COLUMNS,
    MESSAGE_DESTINATION_STATE,
    MESSAGE_DESTINATION_STATS_5M,
    MESSAGE_EVENTS,
    MESSAGE_LIFECYCLE,
    STATS_5M_COLUMNS,
)

if TYPE_CHECKING:
    from app.message.filters import ResolvedSort, WhereClause

# ── 保留字列名需加反引号 ──────────────────────────────────────────────────────
_RESERVED = frozenset({"partition", "offset"})


def _q(col: str) -> str:
    return f"`{col}`" if col in _RESERVED else col


# ── KeysetBound ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeysetBound:
    """cursor.to_keyset_bound 输出；注入 WHERE 之后（禁 OFFSET）。"""

    sql: str
    params: dict[str, Any]


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _merge_params(*dicts: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for d in dicts:
        result.update(d)
    return result


def _order_by_clause(sort: list[ResolvedSort]) -> str:
    parts = [f"{_q(s.column_or_alias)} {s.order.upper()}" for s in sort]
    return "ORDER BY " + ", ".join(parts) if parts else ""


def _event_select_cols(*, include_raw_log: bool) -> str:
    cols = [_q(c) for c in EVENT_VIEW_COLUMNS]
    if include_raw_log:
        cols.append("raw_log")
    return ", ".join(cols)


# ── §6.1 events/query ─────────────────────────────────────────────────────────


def build_events_query(
    *,
    where: WhereClause,
    time_params: dict[str, Any],
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
    include_raw_log: bool,
) -> tuple[str, dict[str, Any]]:
    """直接扫 message_events 主表，事件级 keyset 分页（§6.1）。"""
    select = _event_select_cols(include_raw_log=include_raw_log)
    ks_sql = keyset.sql if keyset else ""
    ks_params = keyset.params if keyset else {}
    order = _order_by_clause(sort)

    sql = (
        f"SELECT {select}\n"  # nosec B608
        f"FROM {MESSAGE_EVENTS}\n"
        f"WHERE timestamp >= fromUnixTimestamp64Milli({{_from:Int64}})"
        f" AND timestamp < fromUnixTimestamp64Milli({{_to:Int64}})"
        f" {where.sql} {ks_sql}\n"
        f"{order}\n"
        f"LIMIT {limit + 1}"
    )
    params = _merge_params(time_params, where.params, ks_params)
    return sql.strip(), params


# ── §6.2 lifecycles/query（两层 argMax） ──────────────────────────────────────

_LIFECYCLE_ARGMAX_COLS = tuple(c for c in LIFECYCLE_READ_COLUMNS if c not in LIFECYCLE_LOGICAL_KEY)


def _argmax_select(cols: tuple[str, ...], version_col: str = "compacted_at") -> str:
    parts: list[str] = []
    for col in cols:
        parts.append(f"argMax({_q(col)}, {version_col}) AS {_q(col)}")
    return ", ".join(parts)


def build_lifecycles_query(
    *,
    inner_where: WhereClause,
    outer_where: WhereClause,
    time_params: dict[str, Any],
    lifecycle_retention_days: int,
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """两层 argMax 结构（§6.2、C-MESSAGE-QUERY-2）。

    内层：版本不变列 GROUP BY + argMax 聚合；
    外层：版本可变列过滤 + 时间窗 + keyset + 排序 + limit。
    """
    group_by_cols = ", ".join(_q(c) for c in LIFECYCLE_LOGICAL_KEY)
    argmax_part = _argmax_select(_LIFECYCLE_ARGMAX_COLS)
    ks_sql = keyset.sql if keyset else ""
    ks_params = keyset.params if keyset else {}
    order = _order_by_clause(sort)

    sql = f"""SELECT *  # noqa: S608
FROM (
    SELECT {group_by_cols}, {argmax_part}
    FROM {MESSAGE_LIFECYCLE} AS ml
    WHERE 1=1 {inner_where.sql}
      AND ml.first_seen_at >= fromUnixTimestamp64Milli({{_from:Int64}}) - INTERVAL {lifecycle_retention_days} DAY
    GROUP BY {group_by_cols}
)
WHERE last_seen_at BETWEEN fromUnixTimestamp64Milli({{_from:Int64}})
    AND fromUnixTimestamp64Milli({{_to:Int64}})
{outer_where.sql}
{ks_sql}
{order}
LIMIT {limit + 1}"""  # nosec B608

    params = _merge_params(time_params, inner_where.params, outer_where.params, ks_params)
    return sql.strip(), params


# ── §6.3 lifecycles/{messageId}（精确拉取 + 五元组去重）────────────────────────


def build_lifecycle_by_message_id_query(
    *,
    lifecycle_key: str,
    system: str | None,
    destination_name: str | None,
    destination_kind: str | None,
    virtual_host: str | None,
    lifecycle_retention_days: int,
    now_ms: int,
) -> tuple[str, dict[str, Any]]:
    """按 lifecycle_key 精确拉取，五元组 argMax 去重（§6.3、C-MESSAGE-QUERY-7）。"""
    group_by_cols = ", ".join(_q(c) for c in LIFECYCLE_LOGICAL_KEY)
    argmax_part = _argmax_select(_LIFECYCLE_ARGMAX_COLS)

    extra_filters = "AND lifecycle_key = {_lifecycle_key:String}"
    extra_params: dict[str, Any] = {"_lifecycle_key": lifecycle_key}

    if system is not None:
        extra_filters += " AND system = {_mid_system:String}"
        extra_params["_mid_system"] = system
    if destination_name is not None:
        extra_filters += " AND destination_name = {_mid_dest_name:String}"
        extra_params["_mid_dest_name"] = destination_name
    if destination_kind is not None:
        extra_filters += " AND destination_kind = {_mid_dest_kind:String}"
        extra_params["_mid_dest_kind"] = destination_kind
    if virtual_host is not None:
        extra_filters += " AND virtual_host = {_mid_vhost:String}"
        extra_params["_mid_vhost"] = virtual_host

    sql = f"""SELECT {group_by_cols}, {argmax_part}  # noqa: S608
FROM {MESSAGE_LIFECYCLE} AS ml
WHERE 1=1
  {extra_filters}
  AND ml.first_seen_at >= fromUnixTimestamp64Milli({{_mid_now_ms:Int64}}) - INTERVAL {lifecycle_retention_days} DAY
GROUP BY {group_by_cols}"""  # nosec B608

    params = _merge_params({"_mid_now_ms": now_ms}, extra_params)
    return sql.strip(), params


# ── §6.5 deadletters/query（两层 argMax + 外层 dead_lettered=1）──────────────


def build_deadletters_query(
    *,
    inner_where: WhereClause,
    outer_where: WhereClause,
    time_params: dict[str, Any],
    lifecycle_retention_days: int,
    sort: list[ResolvedSort],
    keyset: KeysetBound | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """dead_lettered=1 必须在外层（版本共存期，C-MESSAGE-QUERY-3）。"""
    group_by_cols = ", ".join(_q(c) for c in LIFECYCLE_LOGICAL_KEY)
    argmax_part = _argmax_select(_LIFECYCLE_ARGMAX_COLS)
    ks_sql = keyset.sql if keyset else ""
    ks_params = keyset.params if keyset else {}
    order = _order_by_clause(sort)

    sql = f"""SELECT *  # noqa: S608
FROM (
    SELECT {group_by_cols}, {argmax_part}
    FROM {MESSAGE_LIFECYCLE} AS ml
    WHERE 1=1 {inner_where.sql}
      AND ml.first_seen_at >= fromUnixTimestamp64Milli({{_from:Int64}}) - INTERVAL {lifecycle_retention_days} DAY
    GROUP BY {group_by_cols}
)
WHERE dead_lettered = 1
  AND dead_lettered_at BETWEEN fromUnixTimestamp64Milli({{_from:Int64}})
      AND fromUnixTimestamp64Milli({{_to:Int64}})
{outer_where.sql}
{ks_sql}
{order}
LIMIT {limit + 1}"""  # nosec B608

    params = _merge_params(time_params, inner_where.params, outer_where.params, ks_params)
    return sql.strip(), params


# ── §6.4 destinations/query（窗口内每目的地最新快照 + 聚合）────────────────────


_DEST_DIMS = ("system", "destination_name", "destination_kind", "virtual_host")
_DEST_NULLABLE_METRICS = (
    "visible_messages",
    "inflight_messages",
    "delayed_messages",
    "dead_letter_messages",
    "oldest_message_age_seconds",
    "active_consumers",
    "size_bytes",
)


def build_destinations_query(
    *,
    edge_where: WhereClause,
    time_params: dict[str, Any],
    group_by: list[str],
) -> tuple[str, dict[str, Any]]:
    """每目的地最新快照 CTE + 可选聚合（§6.4）。"""
    argmax_metrics = ", ".join(f"argMax(ds.{m}, ds.captured_at) AS {m}" for m in _DEST_NULLABLE_METRICS)
    dims_select = ", ".join(f"ds.{d}" for d in _DEST_DIMS)

    inner_sql = (
        f"SELECT {dims_select}, {argmax_metrics}, max(ds.captured_at) AS captured_at\n"  # nosec B608
        f"    FROM {MESSAGE_DESTINATION_STATE} AS ds\n"
        f"    WHERE ds.captured_at BETWEEN fromUnixTimestamp64Milli({{_from:Int64}})"
        f" AND fromUnixTimestamp64Milli({{_to:Int64}}) {edge_where.sql}\n"
        f"    GROUP BY {dims_select}"
    )

    if group_by:
        agg_dims = ", ".join(group_by)
        agg_metrics = ", ".join(
            [
                "sum(visible_messages) AS visible_messages",
                "sum(inflight_messages) AS inflight_messages",
                "sum(delayed_messages) AS delayed_messages",
                "sum(dead_letter_messages) AS dead_letter_messages",
                "max(oldest_message_age_seconds) AS oldest_message_age_seconds",
                "sum(active_consumers) AS active_consumers",
                "sum(size_bytes) AS size_bytes",
                "max(captured_at) AS captured_at",
            ]
        )
        sql = (
            f"WITH latest AS (\n    {inner_sql}\n)\nSELECT {agg_dims}, {agg_metrics}\nFROM latest\nGROUP BY {agg_dims}"  # nosec B608
        )
    else:
        sql = f"WITH latest AS (\n    {inner_sql}\n)\nSELECT *\nFROM latest"  # nosec B608

    params = _merge_params(time_params, edge_where.params)
    return sql.strip(), params


# ── §6.6 destinations/throughput（每桶最新版本 + 可选二次聚合）─────────────────


def build_throughput_query(
    *,
    system: str,
    destination_name: str,
    destination_kind: str | None,
    virtual_host: str | None,
    time_params: dict[str, Any],
    step_seconds: int,
) -> tuple[str, dict[str, Any]]:
    """每桶 argMax 取最新 compacted 版本；step>300s 时二次按 toStartOfInterval 聚合（§6.6）。"""
    count_cols = (
        "produced_count",
        "consumed_count",
        "ack_count",
        "nack_count",
        "reject_count",
        "timeout_count",
        "dead_letter_count",
        "retry_count",
        "ack_latency_sum_ms",
        "ack_sample_count",
    )
    argmax_counts = ", ".join(f"argMax({c}, compacted_at) AS {c}" for c in count_cols)
    argmax_last = "argMax(last_seen_at, compacted_at) AS last_seen_at"

    extra_filters = ""
    extra_params: dict[str, Any] = {}
    if destination_kind is not None:
        extra_filters += " AND destination_kind = {_th_dk:String}"
        extra_params["_th_dk"] = destination_kind
    if virtual_host is not None:
        extra_filters += " AND virtual_host = {_th_vh:String}"
        extra_params["_th_vh"] = virtual_host

    base_sql = (
        f"SELECT bucket, {argmax_counts}, {argmax_last}\n"  # nosec B608
        f"    FROM {MESSAGE_DESTINATION_STATS_5M}\n"
        f"    WHERE bucket >= fromUnixTimestamp64Milli({{_from:Int64}})"
        f" AND bucket < fromUnixTimestamp64Milli({{_to:Int64}})\n"
        f"      AND system = {{_th_sys:String}}"
        f" AND destination_name = {{_th_dn:String}}"
        f" {extra_filters}\n"
        f"    GROUP BY bucket"
    )
    base_params: dict[str, Any] = {
        "_th_sys": system,
        "_th_dn": destination_name,
        **extra_params,
    }

    if step_seconds > 300:
        sum_counts = ", ".join(f"sum({c}) AS {c}" for c in count_cols)
        sql = (
            f"WITH base AS (\n    {base_sql}\n)\n"  # nosec B608
            f"SELECT toStartOfInterval(bucket, INTERVAL {step_seconds} SECOND) AS bucket,\n"
            f"       {sum_counts}, max(last_seen_at) AS last_seen_at\n"
            f"FROM base\n"
            f"GROUP BY bucket\n"
            f"ORDER BY bucket ASC"
        )
    else:
        sql = (
            f"WITH base AS (\n    {base_sql}\n)\n"  # nosec B608
            f"SELECT bucket, {', '.join(count_cols)}, last_seen_at\n"
            f"FROM base\n"
            f"ORDER BY bucket ASC"
        )

    params = _merge_params(time_params, base_params)
    return sql.strip(), params


# ── Compactor SQL ─────────────────────────────────────────────────────────────


def build_affected_lifecycle_keys(*, rebuild_from_ms: int) -> tuple[str, dict[str, Any]]:
    """第一阶段：受影响 lifecycle_key 五元组（观测时间 >= rebuild_from）。

    同时 SELECT max(observed_at) 供水位推进。
    """
    key_cols = ", ".join(LIFECYCLE_LOGICAL_KEY)
    sql = (
        f"SELECT DISTINCT {key_cols}, max(observed_at) AS max_observed_at\n"  # nosec B608
        f"FROM {MESSAGE_EVENTS}\n"
        f"WHERE lifecycle_key != ''\n"
        f"  AND observed_at >= fromUnixTimestamp64Milli({{_rebuild_from:Int64}})\n"
        f"GROUP BY {key_cols}"
    )
    return sql.strip(), {"_rebuild_from": rebuild_from_ms}


def build_recompute_lifecycles(
    *, key_tuples: list[tuple[str, str, str, str, str]], compacted_at_ms: int
) -> tuple[str, dict[str, Any]]:
    """第二阶段：对受影响 key 全量回查重算 → INSERT INTO message_lifecycle。

    INSERT 必须显式列出目标列（设计 §4.2）。
    terminal_state 派生：死信 → 'dead_lettered'；否则最后一条 ack/nack/reject/timeout 状态。
    """
    insert_cols = ", ".join(LIFECYCLE_COLUMNS)
    group_by_cols = ", ".join(_q(c) for c in LIFECYCLE_LOGICAL_KEY)

    # IN (val tuples) 参数化：每个元组的每个字段各占一个参数
    tuple_params: dict[str, Any] = {}
    in_parts: list[str] = []
    for i, kt in enumerate(key_tuples):
        sys_k = f"_rk_{i}_0"
        dn_k = f"_rk_{i}_1"
        dk_k = f"_rk_{i}_2"
        vh_k = f"_rk_{i}_3"
        lk_k = f"_rk_{i}_4"
        tuple_params[sys_k] = kt[0]
        tuple_params[dn_k] = kt[1]
        tuple_params[dk_k] = kt[2]
        tuple_params[vh_k] = kt[3]
        tuple_params[lk_k] = kt[4]
        in_parts.append(
            f"({{{sys_k}:String}}, {{{dn_k}:String}}, {{{dk_k}:String}}, {{{vh_k}:String}}, {{{lk_k}:String}})"
        )

    in_clause = ", ".join(in_parts)

    sql = f"""INSERT INTO {MESSAGE_LIFECYCLE} ({insert_cols})  # noqa: S608
SELECT
    lifecycle_key,
    argMax(message_id, (timestamp, log_id)) AS message_id,
    argMax(correlation_id, (timestamp, log_id)) AS correlation_id,
    argMax(trace_id, (timestamp, log_id)) AS trace_id,
    system,
    destination_name,
    destination_kind,
    virtual_host,
    argMax(subscription_name, (timestamp, log_id)) AS subscription_name,
    argMax(consumer_group_name, (timestamp, log_id)) AS consumer_group_name,
    min(timestamp) AS first_seen_at,
    max(timestamp) AS last_seen_at,
    fromUnixTimestamp64Milli({{_compacted_at:Int64}}) AS compacted_at,
    nullIf(maxIf(timestamp, event_type = 'dead_letter'), toDateTime64(0, 3, 'UTC')) AS dead_lettered_at,
    groupUniqArrayIf(aic, event_type = 'send') AS producer_aics,
    groupUniqArrayIf(aic, event_type = 'receive') AS consumer_aics,
    countIf(event_type = 'send') AS send_count,
    countIf(event_type = 'receive') AS receive_count,
    max(delivery_attempt) AS max_delivery_attempt,
    max(event_type = 'dead_letter') AS dead_lettered,
    argMaxIf(settlement_reason, (timestamp, log_id), event_type = 'dead_letter') AS dead_letter_reason,
    if(
        max(event_type = 'dead_letter') = 1,
        'dead_lettered',
        argMaxIf(event_type, (timestamp, log_id), event_type IN ('ack', 'nack', 'reject', 'timeout'))
    ) AS terminal_state
FROM {MESSAGE_EVENTS} AS e
WHERE (system, destination_name, destination_kind, virtual_host, lifecycle_key) IN ({in_clause})
GROUP BY {group_by_cols}"""  # nosec B608

    params = _merge_params({"_compacted_at": compacted_at_ms}, tuple_params)
    return sql.strip(), params


def build_affected_buckets(*, rebuild_from_ms: int) -> tuple[str, dict[str, Any]]:
    """Throughput Compactor 第一阶段：受影响 5min 桶五元组。"""
    key_cols = ", ".join(c for c in LIFECYCLE_LOGICAL_KEY if c != "lifecycle_key")
    sql = (
        f"SELECT DISTINCT toStartOfFiveMinutes(timestamp) AS bucket, {key_cols},"  # nosec B608
        f" max(observed_at) AS max_observed_at\n"
        f"FROM {MESSAGE_EVENTS}\n"
        f"WHERE observed_at >= fromUnixTimestamp64Milli({{_rebuild_from:Int64}})\n"
        f"GROUP BY bucket, {key_cols}"
    )
    return sql.strip(), {"_rebuild_from": rebuild_from_ms}


def build_recompute_throughput(
    *, bucket_tuples: list[tuple[int, str, str, str, str]], compacted_at_ms: int
) -> tuple[str, dict[str, Any]]:
    """Throughput Compactor 第二阶段：对受影响桶回查重算 → INSERT INTO stats_5m。"""
    insert_cols = ", ".join(STATS_5M_COLUMNS)
    dims = ("bucket", "system", "destination_name", "destination_kind", "virtual_host")
    group_by_dims = ", ".join(dims)

    tuple_params: dict[str, Any] = {}
    in_parts: list[str] = []
    for i, kt in enumerate(bucket_tuples):
        bk = f"_tb_{i}_0"
        sys_k = f"_tb_{i}_1"
        dn_k = f"_tb_{i}_2"
        dk_k = f"_tb_{i}_3"
        vh_k = f"_tb_{i}_4"
        tuple_params[bk] = kt[0]
        tuple_params[sys_k] = kt[1]
        tuple_params[dn_k] = kt[2]
        tuple_params[dk_k] = kt[3]
        tuple_params[vh_k] = kt[4]
        in_parts.append(
            f"(toDateTime(intDiv({{{bk}:Int64}}, 1000), 'UTC'), {{{sys_k}:String}}, "
            f"{{{dn_k}:String}}, {{{dk_k}:String}}, {{{vh_k}:String}})"
        )

    in_clause = ", ".join(in_parts)

    sql = f"""INSERT INTO {MESSAGE_DESTINATION_STATS_5M} ({insert_cols})  # noqa: S608
SELECT
    toStartOfFiveMinutes(timestamp) AS bucket,
    system,
    destination_name,
    destination_kind,
    virtual_host,
    fromUnixTimestamp64Milli({{_compacted_at:Int64}}) AS compacted_at,
    countIf(event_type = 'send') AS produced_count,
    countIf(event_type = 'receive') AS consumed_count,
    countIf(event_type = 'ack') AS ack_count,
    countIf(event_type = 'nack') AS nack_count,
    countIf(event_type = 'reject') AS reject_count,
    countIf(event_type = 'timeout') AS timeout_count,
    countIf(event_type = 'dead_letter') AS dead_letter_count,
    countIf(event_type = 'receive' AND delivery_attempt > 1) AS retry_count,
    coalesce(sumIf(settlement_latency_ms, event_type = 'ack'), 0) AS ack_latency_sum_ms,
    countIf(event_type = 'ack' AND settlement_latency_ms IS NOT NULL) AS ack_sample_count,
    max(timestamp) AS last_seen_at
FROM {MESSAGE_EVENTS} AS e
WHERE (toStartOfFiveMinutes(timestamp), system, destination_name, destination_kind, virtual_host)
    IN ({in_clause})
GROUP BY {group_by_dims}"""  # nosec B608

    params = _merge_params({"_compacted_at": compacted_at_ms}, tuple_params)
    return sql.strip(), params
