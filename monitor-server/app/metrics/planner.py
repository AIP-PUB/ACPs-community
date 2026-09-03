"""app/metrics/planner.py — 共享查询规划（纯函数）。

实现设计 §6.0.1.C/D/E：数据源与步长规划、折叠 reducer、聚合兼容矩阵。
这里的全部函数是 TDD 的核心靶点：纯逻辑、无 I/O。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from acps_sdk.amp.metrics_catalog import MetricFamily

from app.metrics.exception import (
    OutOfRetentionError,
    StepTooFineError,
    UnsupportedFieldError,
)
from app.metrics.series import (
    AMP_LOAD_ACTIVE_TASKS,
    AMP_LOAD_MAX_ACTIVE_TASKS,
    AMP_LOAD_MAX_QUEUED_TASKS,
    AMP_LOAD_QUEUED_TASKS,
    AMP_LOAD_UPTIME_SECONDS,
    AMP_WINDOW_AVG_THROUGHPUT_MBPS,
    AMP_WINDOW_PEAK_THROUGHPUT_MBPS,
    AMP_WINDOW_REQUEST_PER_SECOND,
    AMP_WINDOW_REQUEST_TOTAL,
    QuerySource,
)

# ── 步长阶梯（毫秒） ─────────────────────────────────────────────────────────────

STEP_LADDER_MS: Final = [15_000, 30_000, 60_000, 300_000, 900_000, 3_600_000]
"""步长阶梯（§6.0.1.C）：15s / 30s / 1m / 5m / 15m / 1h。"""


# ── 数据源描述符 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
    """一个可用数据源的描述符（§6.0.1.C）。"""

    kind: QuerySource
    """数据源类型。"""

    retention_ms: int
    """该源的保留窗口（毫秒）：age_ms <= retention_ms 时该源可用。"""

    min_step_ms: int
    """该源支持的最小步长（毫秒）。"""


def build_sources(raw_retention_days: int, downsample_retention_days: int) -> list[Source]:
    """构造可用数据源列表（设计 §6.0.1.C）。

    retention_ms 动态计算，避免硬编码。

    Args:
        raw_retention_days: 原始序列保留天数（来自配置 metrics_raw_retention_days）。
        downsample_retention_days: 降采样序列保留天数（来自配置 metrics_downsample_retention_days）。

    Returns:
        list[Source]: 固定顺序 [RAW, DS_5M, DS_1H]。
    """
    raw_ms = raw_retention_days * 86_400_000
    ds_ms = (raw_retention_days + downsample_retention_days) * 86_400_000
    return [
        Source(kind=QuerySource.RAW, retention_ms=raw_ms, min_step_ms=15_000),
        Source(kind=QuerySource.DS_5M, retention_ms=ds_ms, min_step_ms=300_000),
        Source(kind=QuerySource.DS_1H, retention_ms=ds_ms, min_step_ms=3_600_000),
    ]


# ── 查询计划 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueryPlan:
    """查询规划结果：选中的数据源 + 有效步长（§6.0.1.C）。"""

    source: Source
    step_ms: int


def _round_up_to_ladder(step_ms: int) -> int:
    """向上取整到 STEP_LADDER_MS 中最近的阶梯值。"""
    for ladder_step in STEP_LADDER_MS:
        if ladder_step >= step_ms:
            return ladder_step
    return STEP_LADDER_MS[-1]


def plan_source_and_step(
    start_ms: int,
    end_ms: int,
    now_ms: int,
    raw_retention_days: int,
    downsample_retention_days: int,
    requested_step_ms: int | None,
    max_points: int,
) -> QueryPlan:
    """选择数据源和步长（§6.0.1.C 伪代码逐行实现）。

    关键：
    - ``age_ms`` 决定数据源可用性（最旧数据点年龄，C-METRIC-QUERY-7）
    - ``duration_ms`` 决定步长/点数计算
    - 二者不可互换

    Args:
        start_ms: 查询起始时间戳（毫秒）。
        end_ms: 查询结束时间戳（毫秒）。
        now_ms: 当前时间戳（毫秒）。
        raw_retention_days: 原始序列保留天数。
        downsample_retention_days: 降采样序列保留天数。
        requested_step_ms: 请求步长（毫秒）；None 表示自动推导。
        max_points: 单条 series 最大点数。

    Returns:
        QueryPlan

    Raises:
        StepTooFineError: 请求步长过细（超过 max_points 约束），或阶梯内无 source 满足 min_step。
        OutOfRetentionError: 所有数据源的保留窗口均不覆盖查询起始时间。
    """
    duration_ms = end_ms - start_ms
    age_ms = now_ms - start_ms  # 最旧数据点年龄

    # 请求步长超点数限制
    if requested_step_ms is not None:
        expected_points = math.ceil(duration_ms / requested_step_ms)
        if expected_points > max_points:
            raise StepTooFineError(
                f"Requested step would produce {expected_points} points (max {max_points}). "
                "Please use a coarser step or narrow the time range."
            )

    # 自动推导步长
    if requested_step_ms is None:
        raw_auto = math.ceil(duration_ms / max_points)
        step_ms = _round_up_to_ladder(raw_auto)
    else:
        step_ms = requested_step_ms

    # 按年龄筛选候选数据源（age_ms <= retention_ms）
    sources = build_sources(raw_retention_days, downsample_retention_days)
    candidates = [s for s in sources if age_ms <= s.retention_ms]
    if not candidates:
        raise OutOfRetentionError(
            f"Query start time is older than the maximum retention window "
            f"({(raw_retention_days + downsample_retention_days)} days). "
            "Please narrow the time range."
        )

    # 在候选中选首个 min_step_ms <= step_ms 的源
    for source in candidates:
        if step_ms >= source.min_step_ms:
            return QueryPlan(source=source, step_ms=step_ms)

    # 所有候选的 min_step 都大于 step_ms → StepTooFineError
    min_available = min(s.min_step_ms for s in candidates)
    raise StepTooFineError(
        f"The effective step ({step_ms}ms) is below the minimum supported step "
        f"({min_available}ms) for the available data sources. "
        "Please use a coarser step or a longer lookback."
    )


def plan_capacity_step(lookback_ms: int, max_points: int) -> int:
    """capacity 固定 RAW 数据源的步长规划（§6.0.1.C 补充）。

    Args:
        lookback_ms: 回看窗口（毫秒）。
        max_points: 最大点数。

    Returns:
        int: 步长毫秒（>= 15000）。
    """
    raw_auto = math.ceil(lookback_ms / max_points)
    return max(15_000, _round_up_to_ladder(raw_auto))


# ── 折叠 reducer（§6.0.1.D） ─────────────────────────────────────────────────

_ADDITIVE_SERIES: Final = frozenset(
    {
        AMP_LOAD_ACTIVE_TASKS,
        AMP_LOAD_QUEUED_TASKS,
        AMP_LOAD_MAX_ACTIVE_TASKS,
        AMP_LOAD_MAX_QUEUED_TASKS,
        AMP_WINDOW_REQUEST_TOTAL,
        AMP_WINDOW_REQUEST_PER_SECOND,
        AMP_WINDOW_AVG_THROUGHPUT_MBPS,
    }
)
"""跨 AIC 折叠时使用 sum 聚合的 series（可加性）。"""

_MAX_SERIES: Final = frozenset({AMP_WINDOW_PEAK_THROUGHPUT_MBPS})
"""跨 AIC 折叠时使用 max 聚合的 series（峰值性）。"""

# 其余（cpu/mem/disk/net/success_rate/avg_latency/latency_ms/uptime）→ avg
# uptime 调用前必须已经被 ensure_uptime_not_folded 拦截（§6.0.1.D 末句）


def fold_reducer(series_name: str) -> str:
    """返回跨 AIC 折叠时使用的聚合算子（"sum" / "max" / "avg"）。

    折叠口径的**单一真相源**（§6.0.1.D）：service 调此函数得 reducer 串，
    再传 promql.apply_series_reducer。不接收 MetricFamily 以避免同族不同口径的歧义。

    Args:
        series_name: 内部 amp_* series 名。

    Returns:
        str: "sum" / "max" / "avg"。
    """
    if series_name in _ADDITIVE_SERIES:
        return "sum"
    if series_name in _MAX_SERIES:
        return "max"
    return "avg"


def ensure_uptime_not_folded(series_name: str, group_by_aic: bool) -> None:
    """uptimeSeconds 不允许跨 AIC 折叠（§6.0.1.D 末句）。

    Args:
        series_name: 内部 amp_* series 名。
        group_by_aic: 是否按 aic 分组（True 表示不折叠，False 表示折叠）。

    Raises:
        UnsupportedFieldError: 试图对 uptime 进行跨 AIC 折叠（422）。
    """
    if series_name == AMP_LOAD_UPTIME_SECONDS and not group_by_aic:
        raise UnsupportedFieldError(
            "uptimeSeconds cannot be aggregated across AICs (it is monotonic per-agent). "
            "Please set groupByAic=true or remove uptimeSeconds from the query."
        )


# ── 指标族 × 聚合兼容矩阵（§6.0.1.E） ───────────────────────────────────────────

_FAMILY_ALLOWED_AGG: Final[dict[str, frozenset[str]]] = {
    MetricFamily.SAMPLE_COUNT_GAUGE: frozenset(
        {"latest", "avg", "min", "max", "sum", "p50", "p75", "p80", "p90", "p95", "p99"}
    ),
    MetricFamily.RESOURCE_USAGE_GAUGE: frozenset(
        {"latest", "avg", "min", "max", "p50", "p75", "p80", "p90", "p95", "p99"}
    ),
    MetricFamily.WINDOW_RATE_LATENCY: frozenset({"latest", "avg", "min", "max"}),
    MetricFamily.WINDOW_TOTAL: frozenset({"latest", "avg", "min", "max"}),
    MetricFamily.MONOTONIC_UPTIME_GAUGE: frozenset({"latest", "max"}),
}


def validate_series_aggregation(family: MetricFamily, aggregation: str) -> None:
    """校验 series/query 聚合算子与指标族的兼容性（§6.0.1.E）。

    Args:
        family: 指标族。
        aggregation: 聚合算子字符串。

    Raises:
        UnsupportedFieldError: 指标族不支持该聚合算子（422）。
    """
    allowed = _FAMILY_ALLOWED_AGG.get(family, frozenset())
    if aggregation not in allowed:
        raise UnsupportedFieldError(
            f"Aggregation '{aggregation}' is not supported for metric family '{family}'. Allowed: {sorted(allowed)}"
        )


def validate_ranking_aggregation(family: MetricFamily, aggregation: str) -> None:
    """校验 rankings/query 聚合算子与指标族的兼容性（复用 series 矩阵，§6.0.1.E）。

    Args:
        family: 指标族。
        aggregation: 聚合算子字符串。

    Raises:
        UnsupportedFieldError: 指标族不支持该聚合算子（422）。
    """
    validate_series_aggregation(family, aggregation)


__all__ = [
    "STEP_LADDER_MS",
    "QueryPlan",
    "Source",
    "build_sources",
    "ensure_uptime_not_folded",
    "fold_reducer",
    "plan_capacity_step",
    "plan_source_and_step",
    "validate_ranking_aggregation",
    "validate_series_aggregation",
]
