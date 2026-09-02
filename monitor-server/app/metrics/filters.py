"""app/metrics/filters.py — 标签过滤 / 分组参数的校验与归一化（纯函数）。

实现设计 §3.5「labelFilter / groupBy 白名单校验」。
调用方（planner.py / service 层）在构造 PromQL 前先经此模块归一化与校验。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from acps_sdk.amp.metrics_catalog import KNOWN_QUANTILES, KNOWN_WINDOWS, PublicMetricMeta

from app.core.amp_api_schema import AMPFilter
from app.metrics.exception import (
    InvalidFilterError,
    InvalidTimeRangeError,
    MetricUnsupportedError,
    UnsupportedFieldError,
)
from app.metrics.labels import ALLOWED_LABELS

# ── 可用于外部 groupBy 参数的合法标签子集（§3.5） ──────────────────────────────
# window / quantile 是后端展开产生的，不允许外部 groupBy 传入
_GROUP_BY_WHITELIST: frozenset[str] = frozenset({"aic", "service_name", "service_namespace", "deployment_env"})

# 外部 labelFilter 可携带的合法标签（含语义标签）
_LABEL_FILTER_WHITELIST: frozenset[str] = frozenset(ALLOWED_LABELS)


# ── LabelMatcher（供 promql.py 用） ───────────────────────────────────────────


class LabelOp(StrEnum):
    EQ = "="
    NEQ = "!="
    REGEX = "=~"
    NREGEX = "!~"
    IN = "in"


@dataclass(frozen=True)
class LabelMatcher:
    """单个 PromQL 标签 matcher，附带 PromQL 渲染能力。"""

    label: str
    op: str
    value: Any
    """EQ/NEQ/REGEX/NREGEX：str；IN：list[str]（渲染为 =~ 多值正则）。"""

    def render(self) -> str:
        """渲染为 PromQL 标签条件字符串（不含外层花括号）。

        IN 被展开为 ``label=~"v1|v2|v3"``（正则转义）。
        """
        import re

        if (self.op == LabelOp.IN or self.op == "in") and isinstance(self.value, list):
            escaped = "|".join(re.escape(v) for v in self.value)
            return f'{self.label}=~"{escaped}"'
        return f'{self.label}{self.op}"{self.value}"'


# ── 公共校验入口 ──────────────────────────────────────────────────────────────


def validate_label_filter(
    label: str,
    op: str,
    value: str | list[str],
) -> LabelMatcher:
    """校验并构造单个标签 matcher。

    Args:
        label: 标签名（必须在 ALLOWED_LABELS 白名单内）。
        op: 操作符（=、!=、=~、!~、in）。
        value: 匹配值；in 时为 list[str]。

    Returns:
        LabelMatcher

    Raises:
        UnsupportedFieldError: label 不在白名单、op 不合法，或 window/quantile 值不在已知集合内。
    """
    if label not in _LABEL_FILTER_WHITELIST:
        raise UnsupportedFieldError(label)

    # 操作符白名单
    valid_ops = {"=", "!=", "=~", "!~", "in"}
    if op not in valid_ops:
        raise UnsupportedFieldError(label)

    # window/quantile 附加值校验（C-METRIC-QUERY-4）
    if label == "window" and op in ("=", "in"):
        candidate_values = [value] if isinstance(value, str) else value
        for v in candidate_values:
            if v not in KNOWN_WINDOWS:
                raise UnsupportedFieldError(f"window={v}")
    if label == "quantile" and op in ("=", "in"):
        candidate_values = [value] if isinstance(value, str) else value
        for v in candidate_values:
            if v not in KNOWN_QUANTILES:
                raise UnsupportedFieldError(f"quantile={v}")

    return LabelMatcher(label=label, op=op, value=value)


def validate_group_by(group_by: list[str] | None) -> list[str]:
    """校验外部 groupBy 参数，返回归一化列表。

    Args:
        group_by: 外部请求传入的分组标签列表（可为 None）。

    Returns:
        list[str]: 已归一化的 groupBy 列表（空列表表示无分组）。

    Raises:
        UnsupportedFieldError: 包含不在白名单的标签。
    """
    if not group_by:
        return []
    invalid = [g for g in group_by if g not in _GROUP_BY_WHITELIST]
    if invalid:
        raise UnsupportedFieldError(invalid[0])
    return list(group_by)


# ── 时间范围归一化（§3.0） ─────────────────────────────────────────────────────


def validate_time_range_ms(start_ms: int, end_ms: int, max_range_ms: int | None = None) -> None:
    """校验时间范围（start < end，不超最大跨度）。

    Args:
        start_ms: 开始时间戳（毫秒）。
        end_ms: 结束时间戳（毫秒）。
        max_range_ms: 最大允许跨度（毫秒），None 表示不限。

    Raises:
        InvalidTimeRangeError: start >= end 或超出最大跨度。
    """
    if start_ms >= end_ms:
        raise InvalidTimeRangeError()
    if max_range_ms is not None and (end_ms - start_ms) > max_range_ms:
        raise InvalidTimeRangeError()


def build_aic_matcher(aics: list[str]) -> LabelMatcher:
    """构造 aic 的 IN matcher（供 planner/service 层复用）。

    Args:
        aics: AIC 列表（非空）。

    Returns:
        LabelMatcher

    Raises:
        MetricUnsupportedError: aics 为空时（无意义的查询）。
    """
    if not aics:
        raise MetricUnsupportedError("(empty aic list)")
    if len(aics) == 1:
        return LabelMatcher(label="aic", op="=", value=aics[0])
    return LabelMatcher(label="aic", op="in", value=aics)


def build_window_matcher(window: str) -> LabelMatcher:
    """构造 window 标签精确匹配器（不做重复校验，调用方应先 validate_label_filter）。

    Args:
        window: ISO 8601 Duration（如 "PT5M"）。

    Returns:
        LabelMatcher
    """
    return LabelMatcher(label="window", op="=", value=window)


def build_quantile_matcher(quantile: str) -> LabelMatcher:
    """构造 quantile 标签精确匹配器。

    Args:
        quantile: 分位数字符串（如 "p95"）。

    Returns:
        LabelMatcher
    """
    return LabelMatcher(label="quantile", op="=", value=quantile)


# ── 各 API 的标签字段白名单（§6.5 spec §6.3.2） ───────────────────────────────────

LABEL_FIELDS: frozenset[str] = frozenset(
    {"aic", "service_name", "service_namespace", "deployment_env", "window", "quantile"}
)
"""series / rankings：全部允许标签。"""

RESOURCE_LABEL_FIELDS: frozenset[str] = frozenset({"aic", "service_name", "service_namespace", "deployment_env"})
"""snapshots / capacity：无 window / quantile。"""

SLO_LABEL_FIELDS: frozenset[str] = frozenset({"aic", "service_name", "service_namespace", "deployment_env", "window"})
"""SLO：含 window，不含 quantile（spec §6.3.2）。"""

SNAPSHOT_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "loadMetrics.activeTasks",
        "loadMetrics.queuedTasks",
        "loadMetrics.maxActiveTasks",
        "loadMetrics.maxQueuedTasks",
        "loadMetrics.cpuUsage",
        "loadMetrics.memoryUsage",
        "windowMetrics.successRate",
        "windowMetrics.p95LatencyMs",
    }
)
"""snapshots 允许的数值后置过滤字段。"""

CAPACITY_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        "loadMetrics.maxActiveTasks",
        "loadMetrics.maxQueuedTasks",
    }
)
"""capacity 允许的数值后置过滤字段（仅 max*）。"""


# ── NumericPostFilter / CompiledFilter ────────────────────────────────────────


@dataclass(frozen=True)
class NumericPostFilter:
    """数值后置过滤条件（应用层过滤，不下推 TSDB）。"""

    path: str
    """字段路径（如 "loadMetrics.activeTasks"）。"""

    op: str
    """操作符：gt / gte / lt / lte / eq / between。"""

    value: float | list[float]


@dataclass(frozen=True)
class CompiledFilter:
    """AMPFilter 编译结果：标签 matcher + 数值后置过滤 + 静态 AIC 集。"""

    label_matchers: list[LabelMatcher]
    post_filters: list[NumericPostFilter]
    static_aics: list[str] | None
    """能静态归约出的有限 AIC 集（aic eq/in 时）；None 表示无限制。"""


# ── AMPFilter 编译 ────────────────────────────────────────────────────────────


def _compile_filter_inner(
    filter_: AMPFilter | None,
    allowed_label_fields: frozenset[str],
    allowed_numeric_fields: frozenset[str],
) -> tuple[list[LabelMatcher], list[NumericPostFilter]]:
    """内部：将 AMPFilter 编译为 label_matchers + post_filters。

    只支持单层 logic="and" 的 conditions；嵌套 groups 或 logic in {"or","not"} → UnsupportedFieldError(422)。
    """

    if filter_ is None:
        return [], []

    if filter_.logic != "and":
        raise UnsupportedFieldError(f"filter.logic='{filter_.logic}' is not supported (only 'and')")

    if filter_.groups:
        raise UnsupportedFieldError("Nested filter groups are not supported")

    label_matchers: list[LabelMatcher] = []
    post_filters: list[NumericPostFilter] = []

    for cond in filter_.conditions or []:
        field = cond.field
        op = cond.op
        value = cond.value

        if field in allowed_label_fields:
            # 操作符归一化
            op_map = {"eq": "=", "ne": "!=", "in": "in", "nin": "!~"}
            pql_op = op_map.get(op, op)
            if pql_op not in {"=", "!=", "=~", "!~", "in"}:
                raise UnsupportedFieldError(f"operator '{op}' is not supported for field '{field}'")
            in_value: str | list[str] = (
                (list(value) if not isinstance(value, list) else value) if pql_op == "in" else str(value)
            )
            lm = validate_label_filter(field, pql_op, in_value)
            label_matchers.append(lm)

        elif field in allowed_numeric_fields:
            post_filters.append(NumericPostFilter(path=field, op=op, value=value))

        else:
            raise UnsupportedFieldError(field)

    return label_matchers, post_filters


def compile_label_filter(
    filter_: AMPFilter | None,
    *,
    allowed_fields: frozenset[str],
) -> list[LabelMatcher]:
    """编译 AMPFilter 为纯标签 matcher 列表（无数值后置过滤）。

    只允许 allowed_fields 中的字段作为标签过滤；数值字段 → UnsupportedFieldError(422)。
    """
    label_matchers, _ = _compile_filter_inner(filter_, allowed_fields, frozenset())
    return label_matchers


def compile_snapshot_filter(filter_: AMPFilter | None) -> CompiledFilter:
    """编译 snapshots/query 的 AMPFilter。

    标签字段限 RESOURCE_LABEL_FIELDS（无 window/quantile）；
    SNAPSHOT_NUMERIC_FIELDS → post_filters；
    计算 static_aics（aic eq/in 时）。
    """
    label_matchers, post_filters = _compile_filter_inner(filter_, RESOURCE_LABEL_FIELDS, SNAPSHOT_NUMERIC_FIELDS)
    static_aics = _extract_static_aics(label_matchers)
    return CompiledFilter(
        label_matchers=label_matchers,
        post_filters=post_filters,
        static_aics=static_aics,
    )


def compile_capacity_filter(
    filter_: AMPFilter | None,
) -> tuple[list[LabelMatcher], list[NumericPostFilter]]:
    """编译 capacity/saturation 的 AMPFilter。

    标签字段限 RESOURCE_LABEL_FIELDS；CAPACITY_NUMERIC_FIELDS → post_filters。
    """
    return _compile_filter_inner(filter_, RESOURCE_LABEL_FIELDS, CAPACITY_NUMERIC_FIELDS)


def _extract_static_aics(label_matchers: list[LabelMatcher]) -> list[str] | None:
    """从 matchers 中提取静态有限 AIC 集（aic = 或 aic in）。"""
    for m in label_matchers:
        if m.label == "aic":
            if m.op == "=" and isinstance(m.value, str):
                return [m.value]
            if m.op == "in" and isinstance(m.value, list):
                return list(m.value)
    return None


def inject_fixed_quantile(matchers: list[LabelMatcher], fixed_quantile: str | None) -> list[LabelMatcher]:
    """p*LatencyMs 自动注入 quantile 标签（§4.1.1 规则）。

    若调用方已传 quantile 且与 fixed_quantile 冲突 → UnsupportedFieldError(422)。
    """
    if fixed_quantile is None:
        return matchers
    for m in matchers:
        if m.label == "quantile":
            if m.value != fixed_quantile:
                raise UnsupportedFieldError(
                    f"Metric has fixed quantile '{fixed_quantile}', but filter has quantile='{m.value}'"
                )
            return matchers
    return [*matchers, build_quantile_matcher(fixed_quantile)]


def inject_window(matchers: list[LabelMatcher], window: str | None) -> list[LabelMatcher]:
    """rankings 顶层 window 等价 filter.window；二者都给且冲突 → InvalidFilterError(400)。"""
    if window is None:
        return matchers
    for m in matchers:
        if m.label == "window":
            if m.value != window:
                raise InvalidFilterError(f"Conflicting window: request window='{window}' but filter window='{m.value}'")
            return matchers
    return [*matchers, build_window_matcher(window)]


def require_window_if_needed(
    meta: PublicMetricMeta,
    matchers: list[LabelMatcher],
) -> None:
    """needs_window=True 且 matchers 中无 window → InvalidFilterError(400)。"""

    if not meta.needs_window:
        return
    for m in matchers:
        if m.label == "window":
            return
    raise InvalidFilterError(
        f"Metric '{meta.public_name}' requires a 'window' filter (needs_window=True). "
        "Please add window='PT5M' or similar to your filter."
    )


def apply_numeric_post_filters(
    items: list[Any],
    post_filters: list[NumericPostFilter],
    value_getter: Callable[[Any, str], float | None],
) -> list[Any]:
    """应用层数值后置过滤（snapshots / capacity 共用）。

    Args:
        items: 待过滤条目列表。
        post_filters: NumericPostFilter 列表。
        value_getter: 从 item 取指定路径数值的函数；返回 None 表示字段缺失（跳过此过滤）。

    Returns:
        list: 通过全部过滤条件的 items。
    """

    if not post_filters:
        return items

    def _passes(item: Any) -> bool:
        for pf in post_filters:
            v = value_getter(item, pf.path)
            if v is None:
                continue  # 字段缺失时不过滤
            # scalar 比较操作：value 为 float
            if isinstance(pf.value, (int, float)):
                scalar = float(pf.value)
                if pf.op == "gt" and not v > scalar:
                    return False
                if pf.op == "gte" and not v >= scalar:
                    return False
                if pf.op == "lt" and not v < scalar:
                    return False
                if pf.op == "lte" and not v <= scalar:
                    return False
                if pf.op == "eq" and v != scalar:
                    return False
            if (
                pf.op == "between"
                and isinstance(pf.value, list)
                and len(pf.value) == 2
                and not (pf.value[0] <= v <= pf.value[1])
            ):
                return False
        return True

    return [item for item in items if _passes(item)]


__all__ = [
    "CAPACITY_NUMERIC_FIELDS",
    "LABEL_FIELDS",
    "RESOURCE_LABEL_FIELDS",
    "SLO_LABEL_FIELDS",
    "SNAPSHOT_NUMERIC_FIELDS",
    "CompiledFilter",
    "LabelMatcher",
    "LabelOp",
    "NumericPostFilter",
    "apply_numeric_post_filters",
    "build_aic_matcher",
    "build_quantile_matcher",
    "build_window_matcher",
    "compile_capacity_filter",
    "compile_label_filter",
    "compile_snapshot_filter",
    "inject_fixed_quantile",
    "inject_window",
    "require_window_if_needed",
    "validate_group_by",
    "validate_label_filter",
    "validate_time_range_ms",
]
