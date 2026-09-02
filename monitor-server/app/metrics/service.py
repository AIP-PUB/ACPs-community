"""app/metrics/service.py — 查询面 service：series / rankings / SLO / capacity。

四个直查 TSDB 的端点（C-METRIC-QUERY-1：除 snapshots 外不读 Redis 作真相源）。

TSDB 异常统一处理原则：所有公开函数顶层捕获 tsdb 抛出的查询/超时异常，
统一转 ReadModelLaggingError(503) 或按超时记 query_timeout_total，
不向调用方暴露 httpx / 解析底层异常。
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Final

import structlog

from app.core.amp_api_schema import AMPResponseMeta, AMPTimeRange
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.metrics import tsdb
from app.metrics.exception import (
    InvalidFilterError,
    InvalidTimeRangeError,
    OutOfRetentionError,
    ReadModelLaggingError,
    SLORuleInvalidError,
    UnsupportedFieldError,
)
from app.metrics.filters import (
    LABEL_FIELDS,
    SLO_LABEL_FIELDS,
    apply_numeric_post_filters,
    compile_capacity_filter,
    compile_label_filter,
    inject_fixed_quantile,
    inject_window,
    require_window_if_needed,
)
from app.metrics.freshness import apply_degrade_policy, build_meta, evaluate_freshness
from app.metrics.metrics import metrics as _metrics
from app.metrics.planner import (
    QueryPlan,
    ensure_uptime_not_folded,
    fold_reducer,
    plan_capacity_step,
    plan_source_and_step,
    validate_ranking_aggregation,
    validate_series_aggregation,
)
from app.metrics.promql import (
    apply_series_reducer,
    build_capacity_candidate_expr,
    build_capacity_detail_selectors,
    build_rank_sampled_at,
    build_ranking_expr,
    build_ranking_score,
    build_selector,
    build_series_rollup,
    build_slo_actual_expr,
    build_slo_ts_expr,
)
from app.metrics.schema import (
    MetricsCapacityRequest,
    MetricsCapacitySaturationItem,
    MetricSeriesPoint,
    MetricsRankingItem,
    MetricsRankingQueryRequest,
    MetricsSeries,
    MetricsSeriesQueryRequest,
    MetricsSLOEvaluateRequest,
    MetricsSLOEvaluateResponse,
    MetricsSLOEvaluation,
    MetricsSLORule,
    MetricsSLOSummary,
)
from app.metrics.series import (
    AMP_LOAD_ACTIVE_TASKS,
    AMP_LOAD_MAX_ACTIVE_TASKS,
    AMP_LOAD_MAX_QUEUED_TASKS,
    AMP_LOAD_QUEUED_TASKS,
    MetricSourceResolver,
    resolve_public_metric,
)

logger = structlog.get_logger(__name__)

_source_resolver = MetricSourceResolver()

# ISO 8601 Duration 正则（简化版，覆盖 PTnMnS / PnD 等常用形式）
_ISO_DURATION_RE: Final = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


# ── 辅助：解析 ISO 8601 Duration → 毫秒 ──────────────────────────────────────


def _parse_iso_duration_ms(duration: str) -> int:
    """将 ISO 8601 Duration 字符串转换为毫秒（近似，月/年按 30/365 天计）。

    Args:
        duration: 如 "PT5M", "P1D", "PT10M30S"。

    Returns:
        毫秒整数。

    Raises:
        ValueError: 格式无效或结果 <= 0。
    """
    m = _ISO_DURATION_RE.match(duration)
    if not m:
        raise ValueError(f"Invalid ISO 8601 duration: {duration!r}")
    years, months, weeks, days, hours, minutes, seconds_str = m.groups()
    total_ms = 0
    if years:
        total_ms += int(years) * 365 * 24 * 3600 * 1000
    if months:
        total_ms += int(months) * 30 * 24 * 3600 * 1000
    if weeks:
        total_ms += int(weeks) * 7 * 24 * 3600 * 1000
    if days:
        total_ms += int(days) * 24 * 3600 * 1000
    if hours:
        total_ms += int(hours) * 3600 * 1000
    if minutes:
        total_ms += int(minutes) * 60 * 1000
    if seconds_str:
        total_ms += int(float(seconds_str) * 1000)
    if total_ms <= 0:
        raise ValueError(f"Duration must be positive, got: {duration!r}")
    return total_ms


def iso_duration_to_promql_range(duration: str) -> str:
    """将 ISO 8601 Duration 转为 PromQL range 选择器（如 PT10M → 10m）。

    配置层使用 ISO 8601（§7.1）；PromQL/VictoriaMetrics 的 `[lookback]` 需 m/h/d/s/ms。
    """
    ms = _parse_iso_duration_ms(duration)
    day_ms = 24 * 3600 * 1000
    hour_ms = 3600 * 1000
    minute_ms = 60 * 1000
    if ms % day_ms == 0 and ms >= day_ms:
        return f"{ms // day_ms}d"
    if ms % hour_ms == 0 and ms >= hour_ms:
        return f"{ms // hour_ms}h"
    if ms % minute_ms == 0 and ms >= minute_ms:
        return f"{ms // minute_ms}m"
    if ms % 1000 == 0 and ms >= 1000:
        return f"{ms // 1000}s"
    return f"{ms}ms"


def promql_timestamp_to_ms(value: float) -> int:
    """将 PromQL 时间函数返回值（秒或毫秒 Unix 时间戳）规范化为毫秒。"""
    v = int(value)
    if v < 10_000_000_000:
        return v * 1000
    return v


def _parse_step_ms(step_str: str | None) -> int | None:
    """解析 step 字段（ISO Duration 或纯秒数字符串）→ 毫秒；None 返回 None。"""
    if step_str is None:
        return None
    if step_str.startswith("P") or step_str.startswith("p"):
        return _parse_iso_duration_ms(step_str)
    # 纯数字兼容（秒）
    try:
        return int(float(step_str) * 1000)
    except ValueError:
        raise InvalidFilterError(f"Cannot parse step: {step_str!r}") from None


# ── 辅助：时间范围工具 ───────────────────────────────────────────────────────


def _require_time_range(tr: AMPTimeRange | None) -> AMPTimeRange:
    """None → 400 INVALID_TIME_RANGE；startAt >= endAt → 同。"""
    if tr is None:
        raise InvalidTimeRangeError("timeRange is required for this query")
    return tr


def _parse_time_range(tr: AMPTimeRange) -> tuple[datetime, datetime, int, int]:
    """解析 AMPTimeRange → (start_dt, end_dt, start_ms, end_ms)。"""
    try:
        start_dt = datetime.fromisoformat(tr.start_at)
        end_dt = datetime.fromisoformat(tr.end_at)
    except ValueError as exc:
        raise InvalidTimeRangeError(f"Cannot parse timeRange: {exc}") from exc
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    if start_ms >= end_ms:
        raise InvalidTimeRangeError("startAt must be before endAt")
    return start_dt, end_dt, start_ms, end_ms


def _range_str(start_ms: int, end_ms: int) -> str:
    """将时间区间转为 PromQL range 字符串（如 '300s'）。"""
    duration_s = max(1, (end_ms - start_ms) // 1000)
    return f"{duration_s}s"


# ── 辅助：GroupBy 构建 ────────────────────────────────────────────────────────

_GROUP_BY_LABEL_WHITELIST: Final = frozenset(
    {"service_name", "service_namespace", "deployment_env", "window", "quantile"}
)


def _build_group_labels(
    group_by_aic: bool | None,
    group_by_labels: list[str] | None,
) -> list[str] | None:
    """构建 group_by 标签列表。

    groupByLabels 仅允许 {service_name, service_namespace, deployment_env, window, quantile}
    （spec §6.3.4, C-METRIC-QUERY-4）；越界 → UnsupportedFieldError(422)。
    groupByAic 默认 True（每主体独立 series）；False 时折叠 aic。
    """
    result: list[str] = []
    if group_by_aic is None or group_by_aic:
        result.append("aic")
    if group_by_labels:
        invalid = [g for g in group_by_labels if g not in _GROUP_BY_LABEL_WHITELIST]
        if invalid:
            raise UnsupportedFieldError(invalid[0])
        for g in group_by_labels:
            if g not in result:
                result.append(g)
    return result or None


# ── 辅助：snapshot value getter（capacity 数值后置过滤复用） ──────────────────


def _snapshot_value_getter(item: MetricsCapacitySaturationItem, path: str) -> float | None:
    """从 MetricsCapacitySaturationItem 获取 path 对应的 float 值。"""
    mapping: dict[str, str] = {
        "loadMetrics.maxActiveTasks": "max_active_tasks",
        "loadMetrics.maxQueuedTasks": "max_queued_tasks",
    }
    attr = mapping.get(path)
    if attr is None:
        return None
    v = getattr(item, attr, None)
    return float(v) if v is not None else None


# ── series/query ──────────────────────────────────────────────────────────────


async def query_series(
    req: MetricsSeriesQueryRequest,
) -> tuple[list[MetricsSeries], AMPResponseMeta]:
    """series/query：返回指定指标的时序数据，含 step 降级。

    设计 §6.2，C-METRIC-QUERY-1/2/3/4/6；C-METRIC-RETENTION-2。
    """
    t0 = time.monotonic()
    settings = get_settings()

    # 1. 解析 metric
    resolved = resolve_public_metric(req.metric)

    # 2. 必填 time_range
    tr = _require_time_range(req.time_range)
    start_dt, end_dt, start_ms, end_ms = _parse_time_range(tr)

    # 3. 聚合函数验证
    aggregation = req.aggregation or "latest"
    validate_series_aggregation(resolved.meta.family, aggregation)

    # 4. 标签 filter
    label_matchers = compile_label_filter(req.filter, allowed_fields=LABEL_FIELDS)
    label_matchers = inject_fixed_quantile(label_matchers, resolved.meta.fixed_quantile)
    require_window_if_needed(resolved.meta, label_matchers)

    # 5. 查询计划（step / source 选择）
    requested_step_ms = _parse_step_ms(req.step)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        query_plan: QueryPlan = plan_source_and_step(
            start_ms=start_ms,
            end_ms=end_ms,
            now_ms=now_ms,
            raw_retention_days=settings.metrics_raw_retention_days,
            downsample_retention_days=settings.metrics_downsample_retention_days,
            requested_step_ms=requested_step_ms,
            max_points=settings.metrics_max_points_per_series,
        )
    except OutOfRetentionError:
        raise

    # 6. 构建 PromQL 表达式
    metric_name = _source_resolver.resolve(resolved.series_name, query_plan.source.kind)
    selector = build_selector(metric_name, label_matchers)
    rollup = build_series_rollup(selector, aggregation, query_plan.step_ms)

    group_labels = _build_group_labels(req.group_by_aic, req.group_by_labels)
    ensure_uptime_not_folded(
        resolved.series_name,
        group_by_aic=req.group_by_aic is None or req.group_by_aic,
    )
    reducer = fold_reducer(resolved.series_name)
    expr = apply_series_reducer(reducer, rollup, group_labels)

    # 7. TSDB 查询
    try:
        matrix = await tsdb.range_query(expr, start=start_dt, end=end_dt, step_ms=query_plan.step_ms)
    except Exception as exc:
        _metrics.inc("amp_metrics_query_timeout_total")
        logger.warning("metrics_service.series.tsdb_error", exc_info=exc)
        raise ReadModelLaggingError() from exc

    # 8. 转 MetricsSeries
    result: list[MetricsSeries] = []
    total_points = 0
    for rs in matrix:
        labels_copy = dict(rs.labels)
        window = labels_copy.get("window")
        points = [
            MetricSeriesPoint(
                timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat(),
                value=v,
            )
            for ts_ms, v in rs.points
        ]
        total_points += len(points)
        result.append(
            MetricsSeries(
                metric=req.metric,
                labels=labels_copy,
                window=window,
                points=points,
                step_ms=query_plan.step_ms,
            )
        )

    _metrics.inc("amp_metrics_query_points_returned_total", total_points)

    # 9. freshness + meta
    redis = get_redis()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _metrics.observe_ms("amp_metrics_query_latency_ms", elapsed_ms)

    freshness = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(freshness)
    meta = build_meta(freshness, now_ms=now_ms, partial=partial, elapsed_ms=elapsed_ms)
    return result, meta


# ── rankings/query ────────────────────────────────────────────────────────────


async def query_rankings(
    req: MetricsRankingQueryRequest,
) -> tuple[list[MetricsRankingItem], AMPResponseMeta]:
    """rankings/query：TopN/BottomN instant 排行。

    设计 §6.3。rankings 不分页：meta.nextCursor 恒空。
    """
    t0 = time.monotonic()
    settings = get_settings()

    # 1. metric + time_range（必填）
    resolved = resolve_public_metric(req.metric)
    tr = _require_time_range(req.time_range)
    _start_dt, end_dt, start_ms, end_ms = _parse_time_range(tr)

    # 2. top_n clamp
    top_n = min(req.top_n or 20, settings.metrics_ranking_max_top_n)

    # 3. 聚合函数验证
    aggregation = req.aggregation or "avg"
    validate_ranking_aggregation(resolved.meta.family, aggregation)

    # 4. 标签 filter
    label_matchers = compile_label_filter(req.filter, allowed_fields=LABEL_FIELDS)
    label_matchers = inject_window(label_matchers, req.window)
    label_matchers = inject_fixed_quantile(label_matchers, resolved.meta.fixed_quantile)
    require_window_if_needed(resolved.meta, label_matchers)

    # 5. 查询计划（仅取 source，instant 不需要 step）
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        query_plan = plan_source_and_step(
            start_ms=start_ms,
            end_ms=end_ms,
            now_ms=now_ms,
            raw_retention_days=settings.metrics_raw_retention_days,
            downsample_retention_days=settings.metrics_downsample_retention_days,
            requested_step_ms=None,
            max_points=settings.metrics_max_points_per_series,
        )
    except OutOfRetentionError:
        raise

    metric_name = _source_resolver.resolve(resolved.series_name, query_plan.source.kind)
    range_str = _range_str(start_ms, end_ms)

    # 6. 构建 score + ranking 表达式
    selector = build_selector(metric_name, label_matchers)
    score_expr = build_ranking_score(selector, aggregation, range_str)
    rank_expr = build_ranking_expr(score_expr, top_n, req.direction or "desc")

    # 7. TSDB instant
    try:
        rows = await tsdb.instant(rank_expr, at=end_dt)
    except Exception as exc:
        _metrics.inc("amp_metrics_query_timeout_total")
        logger.warning("metrics_service.rankings.tsdb_error", exc_info=exc)
        raise ReadModelLaggingError() from exc

    # 7b. sampledAt 回填（仅 latest/min/max 聚合）
    sampled_at_map: dict[str, str] = {}
    if aggregation in {"latest", "min", "max"} and rows:
        try:
            sampled_at_expr = build_rank_sampled_at(selector, aggregation, range_str)
            sampled_at_rows = await tsdb.instant(sampled_at_expr, at=end_dt)
            for row in sampled_at_rows:
                aic = row.labels.get("aic", "")
                if aic:
                    sampled_at_map[aic] = datetime.fromtimestamp(
                        promql_timestamp_to_ms(row.value) / 1000, tz=UTC
                    ).isoformat()
        except Exception as exc:
            logger.warning("metrics_service.rankings.sampled_at_error", exc_info=exc)

    evaluated_at_iso = end_dt.isoformat()
    result: list[MetricsRankingItem] = []
    for row in rows:
        aic = row.labels.get("aic", "")
        window = row.labels.get("window")
        quantile = row.labels.get("quantile")
        result.append(
            MetricsRankingItem(
                aic=aic,
                metric=req.metric,
                window=window,
                quantile=quantile,
                value=row.value,
                evaluated_at=evaluated_at_iso,
                sampled_at=sampled_at_map.get(aic),
            )
        )

    _metrics.inc("amp_metrics_query_points_returned_total", len(result))

    # 8. freshness + meta（nextCursor 恒空）
    redis = get_redis()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _metrics.observe_ms("amp_metrics_query_latency_ms", elapsed_ms)

    freshness = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(freshness)
    meta = build_meta(freshness, now_ms=now_ms, partial=partial, elapsed_ms=elapsed_ms)
    return result, meta


# ── slo/evaluate ──────────────────────────────────────────────────────────────

_MEETS_OPS: Final = frozenset({"success_rate"})


def _slo_meets(sli: str, actual: float, target: float) -> bool:
    """success_rate → actual >= target；时延 → actual <= target。"""
    if sli in _MEETS_OPS:
        return actual >= target
    return actual <= target


def _validate_slo_rule(rule: MetricsSLORule) -> None:
    """校验单条 SLO rule 合法性。"""
    from acps_sdk.amp.metrics_catalog import KNOWN_WINDOWS

    if rule.window not in KNOWN_WINDOWS:
        raise SLORuleInvalidError(f"Unsupported window: {rule.window!r}")
    if rule.sli == "success_rate":
        if not (0 <= rule.target <= 100):
            raise SLORuleInvalidError(f"SLI success_rate target must be in [0, 100], got {rule.target}")
    else:
        if rule.target < 0:
            raise SLORuleInvalidError(f"SLI {rule.sli!r} target must be >= 0, got {rule.target}")


async def evaluate_slo(req: MetricsSLOEvaluateRequest) -> MetricsSLOEvaluateResponse:
    """slo/evaluate：批量 SLO 评估，返回每条 rule 的 meets/breach 明细。

    设计 §6.4。
    """
    t0 = time.monotonic()
    settings = get_settings()

    # 1. time_range 必填；rules 非空且 <= max_rules
    tr = _require_time_range(req.time_range)
    _start_dt, end_dt, start_ms, end_ms = _parse_time_range(tr)

    if not req.rules:
        raise SLORuleInvalidError("rules must be non-empty")
    if len(req.rules) > settings.metrics_slo_max_rules:
        raise SLORuleInvalidError(f"Too many SLO rules: {len(req.rules)} > max {settings.metrics_slo_max_rules}")
    for rule in req.rules:
        _validate_slo_rule(rule)

    # 2. 公共 filter（SLO 允许 window 标签）
    base_matchers = compile_label_filter(req.filter, allowed_fields=SLO_LABEL_FIELDS)
    range_str = _range_str(start_ms, end_ms)

    # 3. 每条 rule 生成 actual_expr + ts_expr
    actual_exprs: dict[int, str] = {}
    ts_exprs: dict[int, str] = {}
    for i, rule in enumerate(req.rules):
        matchers = inject_window(list(base_matchers), rule.window)
        actual_exprs[i] = build_slo_actual_expr(rule.sli, matchers, rule.window, range_str)
        ts_exprs[i] = build_slo_ts_expr(rule.sli, matchers, rule.window, range_str)

    # 4. 并发 instant_many
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        actual_results = await tsdb.instant_many(actual_exprs, at=end_dt)
        ts_results = await tsdb.instant_many(ts_exprs, at=end_dt)
    except Exception as exc:
        _metrics.inc("amp_metrics_query_timeout_total")
        logger.warning("metrics_service.slo.tsdb_error", exc_info=exc)
        raise ReadModelLaggingError() from exc

    # 5. 组装 items（每 rule × 每 AIC）
    include_failed = req.include_failed_details or False
    items: list[MetricsSLOEvaluation] = []
    total = 0
    meets_count = 0
    breach_count = 0

    for i, rule in enumerate(req.rules):
        actual_samples = actual_results.get(i, [])
        ts_samples = ts_results.get(i, [])
        ts_by_aic: dict[str, tsdb.InstantSample] = {
            s.labels.get("aic", ""): s for s in ts_samples if s.labels.get("aic")
        }

        for actual_sample in actual_samples:
            aic = actual_sample.labels.get("aic", "")
            if not aic:
                continue
            ts_sample = ts_by_aic.get(aic)
            if ts_sample is None:
                continue

            actual_val = actual_sample.value
            meets = _slo_meets(rule.sli, actual_val, rule.target)
            observed_at_iso = datetime.fromtimestamp(promql_timestamp_to_ms(ts_sample.value) / 1000, tz=UTC).isoformat()

            total += 1
            if meets:
                meets_count += 1
            else:
                breach_count += 1
                if include_failed:
                    items.append(
                        MetricsSLOEvaluation(
                            aic=aic,
                            window=rule.window,
                            meets=meets,
                            target=rule.target,
                            actual=actual_val,
                            sli=rule.sli,
                            observed_at=observed_at_iso,
                        )
                    )

    # 6. freshness + meta
    redis = get_redis()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _metrics.observe_ms("amp_metrics_query_latency_ms", elapsed_ms)

    freshness = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(freshness)
    meta = build_meta(freshness, now_ms=now_ms, partial=partial, elapsed_ms=elapsed_ms)

    return MetricsSLOEvaluateResponse(
        items=items,
        summary=MetricsSLOSummary(
            total=total,
            meets_count=meets_count,
            breach_count=breach_count,
        ),
        meta=meta,
    )


# ── capacity/saturation ───────────────────────────────────────────────────────


def _resolve_capacity_thresholds(
    req: MetricsCapacityRequest,
) -> tuple[float | None, float | None]:
    """§6.5 第 2 条 union + 默认值：两者都缺→都启用并套默认；只给一个→仅启用该侧。"""
    settings = get_settings()
    active_thr = req.active_ratio_threshold
    queue_thr = req.queue_ratio_threshold

    if active_thr is None and queue_thr is None:
        # 两者都缺 → 都启用默认
        return (
            settings.metrics_capacity_default_active_ratio_threshold,
            settings.metrics_capacity_default_queue_ratio_threshold,
        )

    # 校验越界
    for v, name in [(active_thr, "activeRatioThreshold"), (queue_thr, "queueRatioThreshold")]:
        if v is not None and not (0 < v <= 1):
            raise InvalidFilterError(f"{name} must be in (0, 1], got {v}")

    return active_thr, queue_thr


def _union_candidates(
    active_samples: list[tsdb.InstantSample],
    queue_samples: list[tsdb.InstantSample],
    active_thr: float | None,
    queue_thr: float | None,
) -> list[str]:
    """合并 active + queue 两侧候选 AIC（union 语义）。"""
    candidate_set: set[str] = set()
    if active_thr is not None:
        for s in active_samples:
            aic = s.labels.get("aic", "")
            if aic and s.value >= active_thr:
                candidate_set.add(aic)
    if queue_thr is not None:
        for s in queue_samples:
            aic = s.labels.get("aic", "")
            if aic and s.value >= queue_thr:
                candidate_set.add(aic)
    return sorted(candidate_set)


def _range_series_for_aic(
    matrices: dict[str, list[tsdb.RangeSeries]],
    metric_name: str,
    aic: str,
) -> list[tsdb.RangeSeries]:
    """从 range_many 结果中筛出指定 AIC 的时序。"""
    return [rs for rs in matrices.get(metric_name, []) if rs.labels.get("aic") == aic]


def _compute_capacity_peaks(
    candidate_aics: list[str],
    matrices: dict[str, list[tsdb.RangeSeries]],
    active_thr: float | None,
    queue_thr: float | None,
) -> list[MetricsCapacitySaturationItem]:
    """按时间戳对齐 numerator/denominator，逐点计 ratio，取 lookback 内峰值。

    §6.5 第 4/6/7 条：den<=0 的点丢弃；取最大 ratio 对应时刻；activeTasks 取峰值点。
    """
    items: list[MetricsCapacitySaturationItem] = []

    for aic in candidate_aics:
        act_num = _range_series_for_aic(matrices, AMP_LOAD_ACTIVE_TASKS, aic)
        act_den = _range_series_for_aic(matrices, AMP_LOAD_MAX_ACTIVE_TASKS, aic)
        q_num = _range_series_for_aic(matrices, AMP_LOAD_QUEUED_TASKS, aic)
        q_den = _range_series_for_aic(matrices, AMP_LOAD_MAX_QUEUED_TASKS, aic)

        def _align_and_peak(
            num_series: list[tsdb.RangeSeries],
            den_series: list[tsdb.RangeSeries],
        ) -> tuple[float | None, float | None, float | None, int | None]:
            """返回 (peak_ratio, peak_num, peak_den, peak_ts_ms)。"""
            if not num_series or not den_series:
                return None, None, None, None
            num_map: dict[int, float] = {}
            for rs in num_series:
                for ts_ms, v in rs.points:
                    num_map[ts_ms] = v
            den_map: dict[int, float] = {}
            for rs in den_series:
                for ts_ms, v in rs.points:
                    den_map[ts_ms] = v
            peak_ratio: float | None = None
            peak_num: float | None = None
            peak_den: float | None = None
            peak_ts_ms: int | None = None
            for ts_ms, num_val in num_map.items():
                den_val = den_map.get(ts_ms)
                if den_val is None or den_val <= 0:
                    continue
                ratio = num_val / den_val
                if peak_ratio is None or ratio > peak_ratio:
                    peak_ratio = ratio
                    peak_num = num_val
                    peak_den = den_val
                    peak_ts_ms = ts_ms
            return peak_ratio, peak_num, peak_den, peak_ts_ms

        active_ratio, act_n, act_d, act_ts = _align_and_peak(act_num, act_den)
        queue_ratio, q_n, q_d, q_ts = _align_and_peak(q_num, q_den)

        # 选代表时刻：active 优先（设计 §6.5 第 7 条）
        best_ts = act_ts or q_ts
        if best_ts is None:
            continue

        sampled_at = datetime.fromtimestamp(best_ts / 1000, tz=UTC).isoformat()

        items.append(
            MetricsCapacitySaturationItem(
                aic=aic,
                active_ratio=active_ratio,
                queue_ratio=queue_ratio,
                active_tasks=int(act_n) if act_n is not None else None,
                max_active_tasks=int(act_d) if act_d is not None else None,
                queued_tasks=int(q_n) if q_n is not None else None,
                max_queued_tasks=int(q_d) if q_d is not None else None,
                sampled_at=sampled_at,
            )
        )

    return items


async def query_capacity(
    req: MetricsCapacityRequest,
) -> tuple[list[MetricsCapacitySaturationItem], AMPResponseMeta]:
    """capacity/saturation：两阶段容量饱和度查询。

    设计 §6.5，C-METRIC-MODEL-3。
    """
    t0 = time.monotonic()
    settings = get_settings()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=UTC)

    # 1. 解析 lookback + 阈值
    lookback_str = req.lookback or settings.metrics_capacity_default_lookback
    try:
        lookback_ms = _parse_iso_duration_ms(lookback_str)
    except ValueError as exc:
        raise InvalidFilterError(f"Invalid lookback: {exc}") from exc

    raw_retention_ms = settings.metrics_raw_retention_days * 24 * 3600 * 1000
    if lookback_ms > raw_retention_ms:
        raise OutOfRetentionError(
            f"lookback={lookback_str} exceeds raw retention ({settings.metrics_raw_retention_days}d)"
        )

    active_thr, queue_thr = _resolve_capacity_thresholds(req)
    label_matchers, post_filters = compile_capacity_filter(req.filter)
    promql_lookback = iso_duration_to_promql_range(lookback_str)

    # 2. 第一阶段：候选剪枝（instant，at=now）
    try:
        active_samples: list[tsdb.InstantSample] = []
        queue_samples: list[tsdb.InstantSample] = []

        if active_thr is not None:
            active_expr = build_capacity_candidate_expr("active", label_matchers, promql_lookback)
            active_samples = await tsdb.instant(active_expr, at=now_dt)

        if queue_thr is not None:
            queue_expr = build_capacity_candidate_expr("queue", label_matchers, promql_lookback)
            queue_samples = await tsdb.instant(queue_expr, at=now_dt)

    except Exception as exc:
        _metrics.inc("amp_metrics_query_timeout_total")
        logger.warning("metrics_service.capacity.tsdb_error_phase1", exc_info=exc)
        raise ReadModelLaggingError() from exc

    candidate_aics = _union_candidates(active_samples, queue_samples, active_thr, queue_thr)
    if not candidate_aics:
        redis = get_redis()
        freshness = await evaluate_freshness(redis, now_ms=now_ms)
        meta = build_meta(freshness, now_ms=now_ms)
        return [], meta

    # 3. 第二阶段：候选明细（range_many，RAW）
    step_ms = plan_capacity_step(lookback_ms, settings.metrics_max_points_per_series)
    start_dt = datetime.fromtimestamp((now_ms - lookback_ms) / 1000, tz=UTC)
    detail_selectors = build_capacity_detail_selectors(candidate_aics, label_matchers)

    try:
        matrices = await tsdb.range_many(detail_selectors, start=start_dt, end=now_dt, step_ms=step_ms)
    except Exception as exc:
        _metrics.inc("amp_metrics_query_timeout_total")
        logger.warning("metrics_service.capacity.tsdb_error_phase2", exc_info=exc)
        raise ReadModelLaggingError() from exc

    # 4. 计算峰值
    items = _compute_capacity_peaks(candidate_aics, matrices, active_thr, queue_thr)

    # 5. 数值后置过滤 + 排序 + 截断
    items = apply_numeric_post_filters(items, post_filters, _snapshot_value_getter)

    def _sort_key(item: MetricsCapacitySaturationItem) -> tuple[float, str]:
        best = max(item.active_ratio or 0.0, item.queue_ratio or 0.0)
        return (-best, item.aic)

    items.sort(key=_sort_key)
    items = items[: settings.metrics_ranking_max_top_n]

    _metrics.inc("amp_metrics_query_points_returned_total", len(items))

    # 6. freshness + meta（nextCursor 恒空）
    redis = get_redis()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _metrics.observe_ms("amp_metrics_query_latency_ms", elapsed_ms)

    freshness = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(freshness)
    meta = build_meta(freshness, now_ms=now_ms, partial=partial, elapsed_ms=elapsed_ms)
    return items, meta


__all__ = [
    "evaluate_slo",
    "iso_duration_to_promql_range",
    "promql_timestamp_to_ms",
    "query_capacity",
    "query_rankings",
    "query_series",
]
