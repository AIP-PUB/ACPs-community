"""app/message/filters.py — AMPFilter → ClickHouse WHERE 编译 + 字段白名单（纯函数）。

实现设计 §3.4 第 1 步、§6.4 / spec §6.5.2。
所有值走参数绑定（{name:Type} 格式）；列名来自白名单常量，杜绝 SQL 注入。
首版仅支持单层 logic="and"（§12 O-6，不实现嵌套/or/not）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.message.exception import InvalidFilterError, UnsupportedFieldError, UnsupportedOperatorError

if TYPE_CHECKING:
    from app.core.amp_api_schema import AMPFilter, AMPSortSpec


# ── 数据类 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WhereClause:
    """参数化 WHERE 片段（不含 WHERE 关键字本身）。"""

    sql: str
    params: dict[str, Any]


@dataclass(frozen=True)
class FieldSpec:
    """单个可过滤字段的元数据。"""

    column: str
    ch_type: str
    apis: frozenset[str]


@dataclass(frozen=True)
class ResolvedSort:
    """已验证的排序条件（validate_sort 输出）。"""

    field: str
    column_or_alias: str
    order: str


# ── 保留字列集合（SQL 中需加反引号） ─────────────────────────────────────────
_RESERVED_COLUMNS: Final = frozenset({"partition", "offset"})


def _quote_col(col: str) -> str:
    return f"`{col}`" if col in _RESERVED_COLUMNS else col


# ── events/query 可过滤字段白名单（spec §6.5.2）──────────────────────────────

_EVENTS = frozenset({"events"})
_LIFECYCLES_DL = frozenset({"lifecycles", "deadletters"})
_DEST = frozenset({"destinations"})

EVENT_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    "logId": FieldSpec("log_id", "String", _EVENTS),
    "aic": FieldSpec("aic", "String", _EVENTS),
    "traceId": FieldSpec("trace_id", "String", _EVENTS),
    "correlationId": FieldSpec("correlation_id", "String", _EVENTS),
    "direction": FieldSpec("direction", "String", _EVENTS),
    "eventType": FieldSpec("event_type", "String", _EVENTS),
    "system": FieldSpec("system", "String", _EVENTS),
    "destination.name": FieldSpec("destination_name", "String", _EVENTS),
    "destination.kind": FieldSpec("destination_kind", "String", _EVENTS),
    "destination.virtualHost": FieldSpec("virtual_host", "String", _EVENTS),
    "subscriptionName": FieldSpec("subscription_name", "String", _EVENTS),
    "consumerGroupName": FieldSpec("consumer_group_name", "String", _EVENTS),
    "routing.key": FieldSpec("routing_key", "String", _EVENTS),
    "routing.partition": FieldSpec("partition", "String", _EVENTS),
    "routing.offset": FieldSpec("offset", "Int64", _EVENTS),
    "messageId": FieldSpec("message_id", "String", _EVENTS),
    "lifecycleKey": FieldSpec("lifecycle_key", "String", _EVENTS),
    "deliveryAttempt": FieldSpec("delivery_attempt", "UInt16", _EVENTS),
    "settlement.latencyMs": FieldSpec("settlement_latency_ms", "UInt32", _EVENTS),
    "deadLettered": FieldSpec("__dead_lettered_events", "UInt8", _EVENTS),
    "deadLetterReason": FieldSpec("settlement_reason", "String", _EVENTS),
    "error.code": FieldSpec("error_code", "String", _EVENTS),
}

# ── lifecycles/query & deadletters/query 字段白名单 ──────────────────────────
# 不变列（可安全下推内层 WHERE，C-MESSAGE-QUERY-2）
_LIFECYCLE_IMMUTABLE_FIELDS: Final = frozenset(
    {"system", "destination.name", "destination.kind", "destination.virtualHost", "lifecycleKey"}
)

LIFECYCLE_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    # 不变列（内层下推安全）
    "system": FieldSpec("system", "String", _LIFECYCLES_DL),
    "destination.name": FieldSpec("destination_name", "String", _LIFECYCLES_DL),
    "destination.kind": FieldSpec("destination_kind", "String", _LIFECYCLES_DL),
    "destination.virtualHost": FieldSpec("virtual_host", "String", _LIFECYCLES_DL),
    "lifecycleKey": FieldSpec("lifecycle_key", "String", _LIFECYCLES_DL),
    # 可变列（必须外层过滤，argMax 后才完整）
    "aic": FieldSpec("__aic_in_arrays", "String", _LIFECYCLES_DL),
    "traceId": FieldSpec("trace_id", "String", _LIFECYCLES_DL),
    "correlationId": FieldSpec("correlation_id", "String", _LIFECYCLES_DL),
    "messageId": FieldSpec("message_id", "String", _LIFECYCLES_DL),
    "subscriptionName": FieldSpec("subscription_name", "String", _LIFECYCLES_DL),
    "consumerGroupName": FieldSpec("consumer_group_name", "String", _LIFECYCLES_DL),
    "maxDeliveryAttempt": FieldSpec("max_delivery_attempt", "UInt16", _LIFECYCLES_DL),
    "terminalState": FieldSpec("terminal_state", "String", _LIFECYCLES_DL),
    "deadLettered": FieldSpec("dead_lettered", "UInt8", _LIFECYCLES_DL),
    "deadLetterReason": FieldSpec("dead_letter_reason", "String", _LIFECYCLES_DL),
}

# ── destinations/query 字段白名单 ─────────────────────────────────────────────
DESTINATION_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    "system": FieldSpec("system", "String", _DEST),
    "destination.name": FieldSpec("destination_name", "String", _DEST),
    "destination.kind": FieldSpec("destination_kind", "String", _DEST),
    "destination.virtualHost": FieldSpec("virtual_host", "String", _DEST),
}

# ── 运算符映射 ────────────────────────────────────────────────────────────────
_OP_SQL: Final[dict[str, str]] = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

_SUPPORTED_OPS: Final = frozenset(_OP_SQL) | frozenset({"in", "nin", "contains", "starts_with", "is_null"})


def _compile_single_condition(
    field_key: str,
    op: str,
    value: Any,
    *,
    api: str,
    fields: dict[str, FieldSpec],
    param_prefix: str,
) -> tuple[str, dict[str, Any]]:
    """编译单个 AMPFilterCondition → (sql_fragment, params)。"""
    spec = fields.get(field_key)
    if spec is None:
        raise UnsupportedFieldError(field_key, api)
    if api not in spec.apis:
        raise UnsupportedFieldError(field_key, api)
    if op not in _SUPPORTED_OPS:
        raise UnsupportedOperatorError(op)

    col = spec.column
    ch_type = spec.ch_type
    p = param_prefix

    # 特殊字段：__dead_lettered_events（events 域 deadLettered = event_type='dead_letter'）
    if col == "__dead_lettered_events":
        if op == "eq":
            if value:
                return "AND event_type = 'dead_letter'", {}
            return "AND event_type != 'dead_letter'", {}
        raise UnsupportedOperatorError(op)

    # 特殊字段：__aic_in_arrays（lifecycle 域 aic 过滤）
    if col == "__aic_in_arrays":
        if op == "eq":
            return f"AND has(arrayConcat(producer_aics, consumer_aics), {{{p}:String}})", {p: value}
        raise UnsupportedOperatorError(op)

    quoted_col = _quote_col(col)

    if op in _OP_SQL:
        sql_op = _OP_SQL[op]
        return f"AND {quoted_col} {sql_op} {{{p}:{ch_type}}}", {p: value}
    if op == "in":
        if not isinstance(value, list):
            raise InvalidFilterError(f"'in' operator requires a list value for field '{field_key}'")
        items = ", ".join(f"{{{p}_{i}:{ch_type}}}" for i in range(len(value)))
        params = {f"{p}_{i}": v for i, v in enumerate(value)}
        return f"AND {quoted_col} IN ({items})", params
    if op == "nin":
        if not isinstance(value, list):
            raise InvalidFilterError(f"'nin' operator requires a list value for field '{field_key}'")
        items = ", ".join(f"{{{p}_{i}:{ch_type}}}" for i in range(len(value)))
        params = {f"{p}_{i}": v for i, v in enumerate(value)}
        return f"AND {quoted_col} NOT IN ({items})", params
    if op == "contains":
        return f"AND positionCaseInsensitive({quoted_col}, {{{p}:String}}) > 0", {p: value}
    if op == "starts_with":
        return f"AND startsWith({quoted_col}, {{{p}:String}})", {p: value}
    if op == "is_null":
        return f"AND isNull({quoted_col})", {}
    raise UnsupportedOperatorError(op)


def compile_filter(
    filter_: AMPFilter | None,
    *,
    api: str,
    fields: dict[str, FieldSpec],
) -> WhereClause:
    """AMPFilter → 参数化 WHERE 片段。

    仅支持单层 logic="and"（§12 O-6）。全部值走 {name:Type} 绑定，杜绝注入。
    """
    if filter_ is None or (not filter_.conditions and not filter_.groups):
        return WhereClause(sql="", params={})

    if filter_.logic != "and":
        raise InvalidFilterError("Only logic='and' is supported in the first version.")

    sql_parts: list[str] = []
    params: dict[str, Any] = {}

    for i, cond in enumerate(filter_.conditions or []):
        frag, p = _compile_single_condition(
            cond.field, cond.op, cond.value, api=api, fields=fields, param_prefix=f"f{i}"
        )
        sql_parts.append(frag)
        params.update(p)

    return WhereClause(sql=" ".join(sql_parts), params=params)


def split_lifecycle_where(
    filter_: AMPFilter | None,
    *,
    api: str,
) -> tuple[WhereClause, WhereClause]:
    """把 lifecycle 过滤拆成（版本不变列内层，版本可变列外层）两段。

    C-MESSAGE-QUERY-2：不变列（system/destination/lifecycleKey）下推内层 WHERE；
    可变列（terminalState/maxDeliveryAttempt/aic/traceId 等）留外层 WHERE。
    """
    if filter_ is None or (not filter_.conditions and not filter_.groups):
        return WhereClause(sql="", params={}), WhereClause(sql="", params={})

    inner_sql: list[str] = []
    inner_params: dict[str, Any] = {}
    outer_sql: list[str] = []
    outer_params: dict[str, Any] = {}

    for i, cond in enumerate(filter_.conditions or []):
        spec = LIFECYCLE_FILTER_FIELDS.get(cond.field)
        if spec is None:
            raise UnsupportedFieldError(cond.field, api)
        if api not in spec.apis:
            raise UnsupportedFieldError(cond.field, api)

        frag, p = _compile_single_condition(
            cond.field, cond.op, cond.value, api=api, fields=LIFECYCLE_FILTER_FIELDS, param_prefix=f"lf{i}"
        )
        if cond.field in _LIFECYCLE_IMMUTABLE_FIELDS:
            inner_sql.append(frag)
            inner_params.update(p)
        else:
            outer_sql.append(frag)
            outer_params.update(p)

    return (
        WhereClause(sql=" ".join(inner_sql), params=inner_params),
        WhereClause(sql=" ".join(outer_sql), params=outer_params),
    )


# ── 排序白名单 ────────────────────────────────────────────────────────────────

_EVENTS_SORT_FIELDS: Final[dict[str, str]] = {
    "timestamp": "timestamp",
    "logId": "log_id",
}
_LIFECYCLES_SORT_FIELDS: Final[dict[str, str]] = {
    "lastSeenAt": "last_seen_at",
    "firstSeenAt": "first_seen_at",
    "receiveCount": "receive_count",
}
_DEADLETTERS_SORT_FIELDS: Final[dict[str, str]] = {
    "deadLetteredAt": "dead_lettered_at",
    "lastSeenAt": "last_seen_at",
}
_DESTINATIONS_SORT_FIELDS: Final[dict[str, str]] = {
    "capturedAt": "captured_at",
}

_SORT_FIELDS_BY_API: Final = {
    "events": _EVENTS_SORT_FIELDS,
    "lifecycles": _LIFECYCLES_SORT_FIELDS,
    "deadletters": _DEADLETTERS_SORT_FIELDS,
    "destinations": _DESTINATIONS_SORT_FIELDS,
}

_DEFAULT_SORT: Final[dict[str, list[ResolvedSort]]] = {
    "events": [
        ResolvedSort(field="timestamp", column_or_alias="timestamp", order="desc"),
        ResolvedSort(field="logId", column_or_alias="log_id", order="desc"),
    ],
    "lifecycles": [
        ResolvedSort(field="lastSeenAt", column_or_alias="last_seen_at", order="desc"),
        ResolvedSort(field="lifecycleKey", column_or_alias="lifecycle_key", order="asc"),
    ],
    "deadletters": [
        ResolvedSort(field="deadLetteredAt", column_or_alias="dead_lettered_at", order="desc"),
        ResolvedSort(field="lifecycleKey", column_or_alias="lifecycle_key", order="asc"),
    ],
    "destinations": [
        ResolvedSort(field="capturedAt", column_or_alias="captured_at", order="desc"),
    ],
}


def validate_sort(
    sort: list[AMPSortSpec] | None,
    *,
    api: str,
) -> list[ResolvedSort]:
    """验证排序条件并返回 ResolvedSort 列表；None 返回该 API 的默认排序。"""
    if sort is None:
        return _DEFAULT_SORT.get(api, [])

    allowed = _SORT_FIELDS_BY_API.get(api, {})
    result: list[ResolvedSort] = []
    for spec in sort:
        if spec.field not in allowed:
            raise UnsupportedFieldError(spec.field, api)
        result.append(ResolvedSort(field=spec.field, column_or_alias=allowed[spec.field], order=spec.order))
    return result
