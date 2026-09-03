"""app/access/filters.py — AMPFilter → ClickHouse WHERE 编译 + 字段/排序白名单（纯函数）。

实现设计 §3.3 第 1 步、§5.2，spec §6.4.2/§6.1.3。
所有值走参数绑定；列名/表名来自白名单常量，绝不拼接用户输入（防 SQL 注入）。

首版支持单层 logic="and" + 已声明字段/运算符；
嵌套 groups 与 logic="or/not" 的递归编译预留接口（设计 §12 O-6）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.access.exception import InvalidFilterError, UnsupportedFieldError, UnsupportedOperatorError

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
    """单个可过滤字段的元数据（compile_filter 用于白名单校验与 SQL 生成）。"""

    column: str
    ch_type: str
    apis: frozenset[str]


@dataclass(frozen=True)
class ResolvedSort:
    """已验证的排序条件（validate_sort 输出）。"""

    field: str
    column_or_alias: str
    order: str


# ── 可过滤字段白名单 ──────────────────────────────────────────────────────────

_ALL = frozenset({"events", "operations", "traces", "topology", "errors", "slow"})
_EVENTS_OPS_ERR_SLOW = frozenset({"events", "operations", "errors", "slow"})
_EVENTS_OPS_TRACE_SLOW = frozenset({"events", "operations", "traces", "slow"})
_EVENTS_TRACE_SLOW = frozenset({"events", "traces", "slow"})
_EVENTS_ONLY = frozenset({"events"})

EVENT_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    "aic": FieldSpec("aic", "String", frozenset({"events", "operations", "traces", "errors", "slow"})),
    "traceId": FieldSpec("trace_id", "String", frozenset({"events", "operations", "traces", "errors", "slow"})),
    "spanId": FieldSpec("span_id", "String", _EVENTS_ONLY),
    "parentSpanId": FieldSpec("parent_span_id", "String", _EVENTS_ONLY),
    "correlationId": FieldSpec("correlation_id", "String", _EVENTS_OPS_ERR_SLOW),
    "severity": FieldSpec("severity", "String", _EVENTS_OPS_ERR_SLOW),
    "request.method": FieldSpec(
        "request_method", "String", frozenset({"events", "operations", "traces", "errors", "slow"})
    ),
    "request.route": FieldSpec(
        "request_route", "String", frozenset({"events", "operations", "traces", "errors", "slow"})
    ),
    "request.url": FieldSpec("request_url", "String", _EVENTS_TRACE_SLOW),
    "response.statusCode": FieldSpec(
        "response_status", "UInt16", frozenset({"events", "operations", "traces", "errors", "slow"})
    ),
    "caller.aic": FieldSpec("caller_aic", "String", frozenset({"events", "operations", "errors", "slow"})),
    "caller.serviceName": FieldSpec("caller_service", "String", frozenset({"events", "operations", "errors", "slow"})),
    "caller.ip": FieldSpec("caller_ip", "String", _EVENTS_OPS_ERR_SLOW),
    "callee.aic": FieldSpec("callee_aic", "String", frozenset({"events", "operations", "errors", "slow"})),
    "callee.serviceName": FieldSpec("callee_service", "String", frozenset({"events", "operations", "errors", "slow"})),
    "callee.ip": FieldSpec("callee_ip", "String", _EVENTS_OPS_ERR_SLOW),
    "durationMs": FieldSpec("duration_ms", "UInt32", _EVENTS_OPS_TRACE_SLOW),
    "error.code": FieldSpec("error_code", "String", frozenset({"events", "operations", "errors", "slow"})),
    "error.message": FieldSpec("error_message", "String", _EVENTS_OPS_ERR_SLOW),
    "serviceName": FieldSpec("service_name", "String", frozenset({"events", "operations", "errors", "slow"})),
    "deploymentEnv": FieldSpec("deployment_env", "String", frozenset({"events", "operations", "errors", "slow"})),
}

# access_trace_span 已物化列子集（C-ACCESS-QUERY-3）
TRACE_SPAN_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    k: FieldSpec(v.column, v.ch_type, frozenset({"traces"}))
    for k, v in EVENT_FILTER_FIELDS.items()
    if k
    not in {
        "error.message",
        "caller.ip",
        "callee.ip",
        "correlationId",
        "severity",
        "deploymentEnv",
    }
}

# topology/query 仅支持 caller/callee.aic|serviceName 与时间窗口（字段层）
TOPOLOGY_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    "caller.aic": FieldSpec("caller_aic", "String", frozenset({"topology"})),
    "caller.serviceName": FieldSpec("caller_service", "String", frozenset({"topology"})),
    "callee.aic": FieldSpec("callee_aic", "String", frozenset({"topology"})),
    "callee.serviceName": FieldSpec("callee_service", "String", frozenset({"topology"})),
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
# 非简单运算符标识（使用特殊渲染逻辑）
_SPECIAL_OPS: Final[frozenset[str]] = frozenset(
    {"contains", "containsCs", "in", "nin", "startsWith", "between", "exists"}
)


# ── 排序白名单 ────────────────────────────────────────────────────────────────

_SORT_FIELDS: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "events": {
        "timestamp": ("timestamp", "desc"),
        "durationMs": ("duration_ms", "desc"),
    },
    "operations": {
        "requestCount": ("request_count", "desc"),
        "errorRate": ("error_rate", "desc"),
        "avgDurationMs": ("avg_duration_ms", "desc"),
        "p95DurationMs": ("p95_duration_ms", "desc"),
        "lastSeenAt": ("last_seen_at", "desc"),
    },
    "traces": {
        "lastSeenAt": ("last_seen_at", "desc"),
        "firstSeenAt": ("first_seen_at", "asc"),
        "durationMs": ("duration_ms", "desc"),
        "totalSpans": ("total_spans", "desc"),
        "errorCount": ("error_count", "desc"),
    },
    "topology": {
        "callCount": ("call_count", "desc"),
        "errorRate": ("error_rate", "desc"),
        "avgDurationMs": ("avg_duration_ms", "desc"),
        "p95DurationMs": ("p95_duration_ms", "desc"),
        "lastSeenAt": ("last_seen_at", "desc"),
    },
    "errors": {
        "count": ("error_total", "desc"),
        "lastSeenAt": ("last_seen_at", "desc"),
    },
    "slow": {
        "durationMs": ("duration_ms", "desc"),
    },
}

_DEFAULT_SORT: Final[dict[str, list[ResolvedSort]]] = {
    "events": [ResolvedSort("timestamp", "timestamp", "desc")],
    "operations": [ResolvedSort("requestCount", "request_count", "desc")],
    "traces": [ResolvedSort("lastSeenAt", "last_seen_at", "desc")],
    "topology": [ResolvedSort("callCount", "call_count", "desc")],
    "errors": [ResolvedSort("count", "error_total", "desc")],
    "slow": [ResolvedSort("durationMs", "duration_ms", "desc")],
}


# ── 公开函数 ──────────────────────────────────────────────────────────────────


def compile_filter(
    filter_: AMPFilter | None,
    *,
    api: str,
    fields: dict[str, FieldSpec],
) -> WhereClause:
    """将 AMPFilter 编译为参数化 WHERE 片段（不含 WHERE 关键字）。

    首版支持单层 logic="and" 的 conditions 列表；
    空/None filter → WhereClause(sql="", params={})。

    字段不在 fields 或不适用该 api → raise InvalidFilterError(422)。
    op 不支持 → raise InvalidFilterError(422)。
    """
    if filter_ is None:
        return WhereClause(sql="", params={})

    conditions = filter_.conditions or []
    parts: list[str] = []
    params: dict[str, Any] = {}

    for i, cond in enumerate(conditions):
        param_key = f"f{i}"
        spec = fields.get(cond.field)
        if spec is None:
            raise UnsupportedFieldError(cond.field, api)
        if api not in spec.apis:
            raise UnsupportedFieldError(cond.field, api)
        part, extra_params = _compile_condition(cond.field, spec, cond.op, cond.value, param_key)
        parts.append(part)
        params.update(extra_params)

    if not parts:
        return WhereClause(sql="", params={})

    sql = " AND " + " AND ".join(parts)
    return WhereClause(sql=sql, params=params)


def validate_sort(
    sort: list[AMPSortSpec] | None,
    *,
    api: str,
) -> list[ResolvedSort]:
    """校验并解析排序字段（spec §6.4.2 排序白名单）。

    None → 返回 api 默认排序（含稳定 tiebreak）。
    未知字段或不适用该 api → raise InvalidFilterError(422)。
    """
    if sort is None:
        return _DEFAULT_SORT.get(api, [ResolvedSort("timestamp", "timestamp", "desc")])

    allowed = _SORT_FIELDS.get(api, {})
    result: list[ResolvedSort] = []
    for spec in sort:
        if spec.field not in allowed:
            raise UnsupportedFieldError(spec.field, api)
        col, _ = allowed[spec.field]
        result.append(ResolvedSort(field=spec.field, column_or_alias=col, order=spec.order))
    return result


# ── 私有辅助 ──────────────────────────────────────────────────────────────────


def _compile_condition(
    field: str,
    spec: FieldSpec,
    op: str,
    value: Any,
    param_key: str,
) -> tuple[str, dict[str, Any]]:
    """将单个 AMPFilterCondition 编译为 SQL 片段 + 参数绑定。"""
    col = spec.column
    ch_type = spec.ch_type

    if op in _OP_SQL:
        sql_op = _OP_SQL[op]
        return f"{col} {sql_op} {{{param_key}:{ch_type}}}", {param_key: value}

    if op in ("contains", "containsCs"):
        func = "positionCaseInsensitive" if op == "contains" else "position"
        return f"{func}({col}, {{{param_key}:{ch_type}}}) > 0", {param_key: value}

    if op == "startsWith":
        return (
            f"startsWith(lower({col}), lower({{{param_key}:{ch_type}}}))",
            {param_key: value},
        )

    if op == "in":
        if not isinstance(value, (list, tuple)):
            raise InvalidFilterError(f"'in' operator requires a list value, got {type(value)}")
        keys = {f"{param_key}_{j}": v for j, v in enumerate(value)}
        placeholders = ", ".join(f"{{{k}:{ch_type}}}" for k in keys)
        return f"{col} IN ({placeholders})", keys

    if op == "nin":
        if not isinstance(value, (list, tuple)):
            raise InvalidFilterError(f"'nin' operator requires a list value, got {type(value)}")
        keys = {f"{param_key}_{j}": v for j, v in enumerate(value)}
        placeholders = ", ".join(f"{{{k}:{ch_type}}}" for k in keys)
        return f"{col} NOT IN ({placeholders})", keys

    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise InvalidFilterError("'between' operator requires [min, max] value")
        lo_key, hi_key = f"{param_key}_lo", f"{param_key}_hi"
        return (
            f"{col} BETWEEN {{{lo_key}:{ch_type}}} AND {{{hi_key}:{ch_type}}}",
            {lo_key: value[0], hi_key: value[1]},
        )

    if op == "exists":
        return f"{col} != ''", {}

    raise UnsupportedOperatorError(op)
