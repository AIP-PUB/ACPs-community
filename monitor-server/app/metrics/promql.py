"""app/metrics/promql.py — MetricsQL/PromQL 表达式构造（纯函数）。

全部函数零 I/O、纯字符串构造，是 TDD 的高价值靶点。
实现设计 §6.2~§6.5「核心查询表达式」逐条落地。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.metrics.filters import LabelMatcher

# ── 工具：aic 列表正则转义拼接 ─────────────────────────────────────────────────


def regex_escape_join(values: list[str]) -> str:
    """将字符串列表转义并拼接为 PromQL `=~` 正则值（防止元字符注入）。

    Args:
        values: 需要拼接的字符串列表。

    Returns:
        str: 如 ``a1|a2|a3`` 形式（已转义）。
    """
    return "|".join(re.escape(v) for v in values)


# ── selector 构造 ─────────────────────────────────────────────────────────────


def build_selector(metric_name: str, label_matchers: list[LabelMatcher]) -> str:
    """构造 PromQL metric selector 字符串。

    Args:
        metric_name: 内部 amp_* series 名（或物理 series 名）。
        label_matchers: 标签过滤列表。

    Returns:
        str: 如 ``amp_load_cpu_usage{aic="xxx",window="PT5M"}``；无 matcher 时返回裸 metric_name。
    """
    if not label_matchers:
        return metric_name
    parts = ",".join(m.render() for m in label_matchers)
    return f"{metric_name}{{{parts}}}"


# ── 时间滚动聚合 rollup ────────────────────────────────────────────────────────

_ROLLUP_MAP: dict[str, str] = {
    "avg": "avg_over_time",
    "min": "min_over_time",
    "max": "max_over_time",
    "sum": "sum_over_time",
    "latest": "last_over_time",
}

_QUANTILE_MAP: dict[str, float] = {
    "p50": 0.5,
    "p75": 0.75,
    "p80": 0.8,
    "p90": 0.9,
    "p95": 0.95,
    "p99": 0.99,
}


def _step_to_range(step_ms: int) -> str:
    """将步长毫秒转换为 PromQL range 字符串（如 ``60000ms``）。"""
    return f"{step_ms}ms"


def build_series_rollup(selector: str, aggregation: str, step_ms: int) -> str:
    """构造时间窗口聚合表达式（设计 §6.2）。

    Args:
        selector: 已构造的 selector 字符串（含标签）。
        aggregation: 聚合算子（avg/min/max/sum/latest/p50/p95/p99 等）。
        step_ms: 步长毫秒，用于构造 range 字符串。

    Returns:
        str: 如 ``avg_over_time(amp_load_cpu_usage{aic="x"}[60000ms])``。
    """
    range_str = _step_to_range(step_ms)
    if aggregation in _ROLLUP_MAP:
        fn = _ROLLUP_MAP[aggregation]
        return f"{fn}({selector}[{range_str}])"
    if aggregation in _QUANTILE_MAP:
        q = _QUANTILE_MAP[aggregation]
        return f"quantile_over_time({q}, {selector}[{range_str}])"
    # 兜底：按 avg 处理（调用方应先经过 planner.validate_series_aggregation）
    return f"avg_over_time({selector}[{range_str}])"


# ── 折叠 reducer（跨 AIC 折叠，§6.0.1.D） ────────────────────────────────────


def apply_series_reducer(reducer: str, rollup_expr: str, group_labels: list[str] | None) -> str:
    """按折叠 reducer 生成跨 AIC 聚合表达式（设计 §6.0.1.D）。

    关键：接收 `reducer: str`（"sum"/"avg"/"max"）而非 MetricFamily，
    因为同族内不同 series 的折叠口径不同（§6.0.1.D 末段）。

    Args:
        reducer: 折叠聚合算子字符串（"sum"/"avg"/"max"）。
        rollup_expr: 已构造的 rollup 表达式。
        group_labels: 保留的分组标签列表；None 或空列表表示不折叠。

    Returns:
        str: 折叠后的聚合表达式，或原 rollup_expr（不折叠时）。
    """
    if not group_labels:
        return rollup_expr
    labels_str = ",".join(group_labels)
    return f"{reducer} by ({labels_str}) ({rollup_expr})"


# ── rankings 表达式 ──────────────────────────────────────────────────────────

_RANKING_ROLLUP_MAP: dict[str, str] = {
    "avg": "avg_over_time",
    "min": "min_over_time",
    "max": "max_over_time",
    "latest": "last_over_time",
}

_RANKING_TS_MAP: dict[str, str] = {
    "latest": "tlast_over_time",
    "min": "tmin_over_time",
    "max": "tmax_over_time",
}


def build_ranking_score(selector: str, aggregation: str, range_str: str) -> str:
    """构造 rankings topk/bottomk 评分表达式（设计 §6.3）。

    Args:
        selector: 已构造的 selector。
        aggregation: avg/max/min/latest/p95/p99。
        range_str: range 字符串（如 "PT5M" 转为 "300000ms"，调用方负责转换）。

    Returns:
        str: 评分表达式。
    """
    if aggregation in _RANKING_ROLLUP_MAP:
        fn = _RANKING_ROLLUP_MAP[aggregation]
        return f"{fn}({selector}[{range_str}])"
    if aggregation in _QUANTILE_MAP:
        q = _QUANTILE_MAP[aggregation]
        return f"quantile_over_time({q}, {selector}[{range_str}])"
    return f"last_over_time({selector}[{range_str}])"


def build_ranking_expr(score_expr: str, top_n: int, direction: str) -> str:
    """构造 topk / bottomk 表达式（设计 §6.3）。

    Args:
        score_expr: 评分表达式。
        top_n: 取前 N 条。
        direction: "asc" → bottomk；"desc" → topk。

    Returns:
        str: topk/bottomk 表达式。
    """
    fn = "bottomk" if direction == "asc" else "topk"
    return f"{fn}({top_n}, {score_expr})"


def build_rank_sampled_at(selector: str, aggregation: str, range_str: str) -> str:
    """构造 sampledAt 时刻查询表达式（设计 §6.3 第 6 条）。

    latest/min/max 有对应时刻函数；其余（avg/p95/p99）不适用，调用方不应调用。

    Args:
        selector: 已构造的 selector。
        aggregation: latest/min/max。
        range_str: range 字符串。

    Returns:
        str: tlast/tmin/tmax_over_time 表达式。
    """
    fn = _RANKING_TS_MAP.get(aggregation, "tlast_over_time")
    return f"{fn}({selector}[{range_str}])"


# ── SLO 表达式 ────────────────────────────────────────────────────────────────


def build_slo_actual_expr(sli: str, label_matchers: list[LabelMatcher], window: str, range_str: str) -> str:
    """构造 SLO 实际值查询表达式（设计 §6.4）。

    Args:
        sli: SLI 名（success_rate / p95_latency_ms / p99_latency_ms / avg_latency_ms）。
        label_matchers: 已注入 window/quantile 的 matcher 列表。
        window: ISO 8601 Duration。
        range_str: 回顾 range 字符串。

    Returns:
        str: 实际值表达式。
    """
    from app.metrics.series import (
        AMP_WINDOW_AVG_LATENCY_MS,
        AMP_WINDOW_LATENCY_MS,
        AMP_WINDOW_SUCCESS_RATE,
    )

    if sli == "success_rate":
        sel = build_selector(AMP_WINDOW_SUCCESS_RATE, label_matchers)
        return f"min_over_time({sel}[{range_str}])"
    if sli in ("p95_latency_ms", "p99_latency_ms"):
        sel = build_selector(AMP_WINDOW_LATENCY_MS, label_matchers)
        return f"max_over_time({sel}[{range_str}])"
    # avg_latency_ms
    sel = build_selector(AMP_WINDOW_AVG_LATENCY_MS, label_matchers)
    return f"max_over_time({sel}[{range_str}])"


def build_slo_ts_expr(sli: str, label_matchers: list[LabelMatcher], window: str, range_str: str) -> str:
    """构造 SLO observedAt 时刻表达式（设计 §6.4）。"""
    from app.metrics.series import (
        AMP_WINDOW_AVG_LATENCY_MS,
        AMP_WINDOW_LATENCY_MS,
        AMP_WINDOW_SUCCESS_RATE,
    )

    if sli == "success_rate":
        sel = build_selector(AMP_WINDOW_SUCCESS_RATE, label_matchers)
        return f"tmin_over_time({sel}[{range_str}])"
    if sli in ("p95_latency_ms", "p99_latency_ms"):
        sel = build_selector(AMP_WINDOW_LATENCY_MS, label_matchers)
        return f"tmax_over_time({sel}[{range_str}])"
    sel = build_selector(AMP_WINDOW_AVG_LATENCY_MS, label_matchers)
    return f"tmax_over_time({sel}[{range_str}])"


# ── capacity 表达式 ───────────────────────────────────────────────────────────


def build_capacity_candidate_expr(side: str, label_matchers: list[LabelMatcher], lookback: str) -> str:
    """构造 capacity 候选剪枝 instant query 表达式（设计 §6.5）。

    分母 > 0 约束通过过滤确保，C-METRIC-MODEL-3：不对 0 分母求比值。

    Args:
        side: "active" 或 "queue"。
        label_matchers: resource 标签 matcher 列表（不含 window/quantile）。
        lookback: 回看 range 字符串（如 "600000ms"）。

    Returns:
        str: max over lookback 的 ratio 表达式。
    """
    from app.metrics.series import (
        AMP_LOAD_ACTIVE_TASKS,
        AMP_LOAD_MAX_ACTIVE_TASKS,
        AMP_LOAD_MAX_QUEUED_TASKS,
        AMP_LOAD_QUEUED_TASKS,
    )

    if side == "active":
        num_metric = AMP_LOAD_ACTIVE_TASKS
        den_metric = AMP_LOAD_MAX_ACTIVE_TASKS
    else:
        num_metric = AMP_LOAD_QUEUED_TASKS
        den_metric = AMP_LOAD_MAX_QUEUED_TASKS

    num_sel = build_selector(num_metric, label_matchers)
    den_sel = build_selector(den_metric, label_matchers)
    # 分母 > 0 过滤（C-METRIC-MODEL-3）
    ratio = f"({num_sel} / on(aic,service_name) {den_sel}) if on(aic,service_name) ({den_sel} > 0)"
    return f"max_over_time(({ratio})[{lookback}:])"


def build_capacity_detail_selectors(
    candidate_aics: list[str],
    label_matchers: list[LabelMatcher],
) -> dict[str, str]:
    """构造 capacity 明细 range query 的 4 个基础 series selector（设计 §6.5 第 3 条）。

    Args:
        candidate_aics: 候选剪枝阶段返回的 AIC 列表。
        label_matchers: 基础 resource 标签 matcher。

    Returns:
        dict[str, str]: series 名 → selector 字符串映射。
    """
    from app.metrics.filters import LabelMatcher
    from app.metrics.series import (
        AMP_LOAD_ACTIVE_TASKS,
        AMP_LOAD_MAX_ACTIVE_TASKS,
        AMP_LOAD_MAX_QUEUED_TASKS,
        AMP_LOAD_QUEUED_TASKS,
    )

    aic_matcher = LabelMatcher(label="aic", op="in", value=candidate_aics)
    matchers = [aic_matcher, *label_matchers]

    return {
        AMP_LOAD_ACTIVE_TASKS: build_selector(AMP_LOAD_ACTIVE_TASKS, matchers),
        AMP_LOAD_MAX_ACTIVE_TASKS: build_selector(AMP_LOAD_MAX_ACTIVE_TASKS, matchers),
        AMP_LOAD_QUEUED_TASKS: build_selector(AMP_LOAD_QUEUED_TASKS, matchers),
        AMP_LOAD_MAX_QUEUED_TASKS: build_selector(AMP_LOAD_MAX_QUEUED_TASKS, matchers),
    }


# ── snapshots/query TSDB 回退（§6.1 核心查询表达式） ─────────────────────────


def build_snapshot_anchor_expr(aics: list[str], lookback: str) -> str:
    """构造快照锚点时刻表达式（§6.1 修复回退，§4.4）。

    Args:
        aics: 待查询的 AIC 列表。
        lookback: range 字符串（如 "600000ms"）。

    Returns:
        str: ``tlast_over_time(amp_snapshot_present{aic=~"..."}[lookback])``。
    """
    from app.metrics.series import AMP_SNAPSHOT_PRESENT

    aic_re = regex_escape_join(aics)
    selector = f'{AMP_SNAPSHOT_PRESENT}{{aic=~"{aic_re}"}}'
    return f"tlast_over_time({selector}[{lookback}])"


def build_snapshot_field_value_exprs(
    aics: list[str],
    windows: list[str] | None,
    lookback: str,
) -> dict[str, str]:
    """构造快照各字段 value 表达式（§6.1 修复回退）。

    Args:
        aics: 候选 AIC 列表。
        windows: 需要查询的 window 列表（None 表示不过滤）。
        lookback: range 字符串。

    Returns:
        dict[str, str]: field_key → last_over_time 表达式。
    """
    from app.metrics.series import (
        AMP_LOAD_ACTIVE_TASKS,
        AMP_LOAD_CPU_USAGE,
        AMP_LOAD_DISK_USAGE,
        AMP_LOAD_MEMORY_USAGE,
        AMP_LOAD_NETWORK_IN_USAGE,
        AMP_LOAD_NETWORK_OUT_USAGE,
        AMP_LOAD_QUEUED_TASKS,
        AMP_LOAD_UPTIME_SECONDS,
        AMP_WINDOW_AVG_LATENCY_MS,
        AMP_WINDOW_AVG_THROUGHPUT_MBPS,
        AMP_WINDOW_LATENCY_MS,
        AMP_WINDOW_PEAK_THROUGHPUT_MBPS,
        AMP_WINDOW_REQUEST_PER_SECOND,
        AMP_WINDOW_REQUEST_TOTAL,
        AMP_WINDOW_SUCCESS_RATE,
    )

    aic_re = regex_escape_join(aics)
    base_matchers = f'aic=~"{aic_re}"'

    exprs: dict[str, str] = {}

    def _add(key: str, metric: str, extra: str = "") -> None:
        extra_part = f",{extra}" if extra else ""
        sel = f"{metric}{{{base_matchers}{extra_part}}}"
        exprs[key] = f"last_over_time({sel}[{lookback}])"

    # Load metrics
    _add("active_tasks", AMP_LOAD_ACTIVE_TASKS)
    _add("queued_tasks", AMP_LOAD_QUEUED_TASKS)
    _add("cpu_usage", AMP_LOAD_CPU_USAGE)
    _add("memory_usage", AMP_LOAD_MEMORY_USAGE)
    _add("disk_usage", AMP_LOAD_DISK_USAGE)
    _add("network_in_usage", AMP_LOAD_NETWORK_IN_USAGE)
    _add("network_out_usage", AMP_LOAD_NETWORK_OUT_USAGE)
    _add("uptime_seconds", AMP_LOAD_UPTIME_SECONDS)

    # Window metrics
    for w in windows or []:
        w_esc = re.escape(w)
        wf = f'window="{w_esc}"'
        _add(f"success_rate:{w}", AMP_WINDOW_SUCCESS_RATE, wf)
        _add(f"request_total:{w}", AMP_WINDOW_REQUEST_TOTAL, wf)
        _add(f"request_per_second:{w}", AMP_WINDOW_REQUEST_PER_SECOND, wf)
        _add(f"avg_throughput_mbps:{w}", AMP_WINDOW_AVG_THROUGHPUT_MBPS, wf)
        _add(f"peak_throughput_mbps:{w}", AMP_WINDOW_PEAK_THROUGHPUT_MBPS, wf)
        _add(f"avg_latency_ms:{w}", AMP_WINDOW_AVG_LATENCY_MS, wf)
        for q in ("p50", "p75", "p80", "p90", "p95", "p99"):
            key = f"latency_ms:{w}:{q}"
            sel = f'{AMP_WINDOW_LATENCY_MS}{{{base_matchers},window="{w_esc}",quantile="{q}"}}'
            exprs[key] = f"last_over_time({sel}[{lookback}])"

    return exprs


def build_snapshot_field_ts_exprs(
    aics: list[str],
    windows: list[str] | None,
    lookback: str,
) -> dict[str, str]:
    """构造快照各字段时刻表达式（§6.1 修复回退，§6.1 第 6 条 field_ts 校验）。

    与 build_snapshot_field_value_exprs 结构相同，但用 tlast_over_time。
    """
    value_exprs = build_snapshot_field_value_exprs(aics, windows, lookback)
    return {k: v.replace("last_over_time(", "tlast_over_time(", 1) for k, v in value_exprs.items()}


__all__ = [
    "apply_series_reducer",
    "build_capacity_candidate_expr",
    "build_capacity_detail_selectors",
    "build_rank_sampled_at",
    "build_ranking_expr",
    "build_ranking_score",
    "build_selector",
    "build_series_rollup",
    "build_slo_actual_expr",
    "build_slo_ts_expr",
    "build_snapshot_anchor_expr",
    "build_snapshot_field_ts_exprs",
    "build_snapshot_field_value_exprs",
    "regex_escape_join",
]
