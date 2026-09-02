"""tests/unit/test_metrics_pure_functions.py — Step 3 纯函数层单元测试。

覆盖：labels / series / promql / planner / filters / samples / cursor / runtime.validate
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from acps_sdk.amp.models import MetricsBody

# ── TestLabels ─────────────────────────────────────────────────────────────────


class TestLabels:
    """labels.py — derive_resource_labels / base_labels / assert_label_cardinality_safe。"""

    def test_derive_resource_labels_full(self) -> None:
        from app.metrics.labels import derive_resource_labels

        resource = {
            "service.name": "demo-leader",
            "service.namespace": "acps-demo",
            "deployment.environment.name": "dev",
            "host.name": "box-1",  # 高基数字段，应被丢弃
        }
        result = derive_resource_labels(resource)
        assert result == {
            "service_name": "demo-leader",
            "service_namespace": "acps-demo",
            "deployment_env": "dev",
        }
        assert "host_name" not in result

    def test_derive_resource_labels_none(self) -> None:
        from app.metrics.labels import derive_resource_labels

        assert derive_resource_labels(None) == {}

    def test_derive_resource_labels_empty(self) -> None:
        from app.metrics.labels import derive_resource_labels

        assert derive_resource_labels({}) == {}

    def test_derive_resource_labels_partial(self) -> None:
        from app.metrics.labels import derive_resource_labels

        resource = {"service.name": "svc", "deployment.environment.name": "prod"}
        result = derive_resource_labels(resource)
        assert result == {"service_name": "svc", "deployment_env": "prod"}
        assert "service_namespace" not in result

    def test_base_labels_contains_aic(self) -> None:
        from app.metrics.labels import base_labels

        result = base_labels("aic-001", {"service_name": "svc"})
        assert result["aic"] == "aic-001"
        assert result["service_name"] == "svc"

    def test_assert_label_cardinality_safe_ok(self) -> None:
        from app.metrics.labels import assert_label_cardinality_safe

        assert_label_cardinality_safe({"aic": "a", "service_name": "s", "window": "PT5M"})

    def test_assert_label_cardinality_safe_disallowed(self) -> None:
        from app.metrics.labels import assert_label_cardinality_safe

        with pytest.raises(ValueError, match="disallowed"):
            assert_label_cardinality_safe({"aic": "a", "host_name": "box-1"})


# ── TestSeries ─────────────────────────────────────────────────────────────────


class TestSeries:
    """series.py — resolve_public_metric / MetricSourceResolver。"""

    def test_resolve_cpu_usage(self) -> None:
        from app.metrics.series import AMP_LOAD_CPU_USAGE, resolve_public_metric

        result = resolve_public_metric("cpuUsage")
        assert result.public_name == "cpuUsage"
        assert result.series_name == AMP_LOAD_CPU_USAGE

    def test_resolve_p95_latency_maps_to_amp_window_latency(self) -> None:
        from app.metrics.series import AMP_WINDOW_LATENCY_MS, resolve_public_metric

        r = resolve_public_metric("p95LatencyMs")
        assert r.series_name == AMP_WINDOW_LATENCY_MS

    def test_resolve_p99_latency_same_series_as_p95(self) -> None:
        from app.metrics.series import AMP_WINDOW_LATENCY_MS, resolve_public_metric

        r = resolve_public_metric("p99LatencyMs")
        assert r.series_name == AMP_WINDOW_LATENCY_MS

    def test_resolve_internal_name_raises(self) -> None:
        from app.metrics.exception import MetricUnsupportedError
        from app.metrics.series import resolve_public_metric

        with pytest.raises(MetricUnsupportedError):
            resolve_public_metric("amp_load_cpu_usage")

    def test_resolve_unknown_raises(self) -> None:
        from app.metrics.exception import MetricUnsupportedError
        from app.metrics.series import resolve_public_metric

        with pytest.raises(MetricUnsupportedError):
            resolve_public_metric("nonExistentMetric")

    def test_source_resolver_raw_returns_bare_name(self) -> None:
        from app.metrics.series import AMP_LOAD_CPU_USAGE, MetricSourceResolver, QuerySource

        r = MetricSourceResolver().resolve(AMP_LOAD_CPU_USAGE, QuerySource.RAW)
        assert r == AMP_LOAD_CPU_USAGE

    def test_source_resolver_ds5m(self) -> None:
        from app.metrics.series import AMP_LOAD_CPU_USAGE, MetricSourceResolver, QuerySource

        r = MetricSourceResolver().resolve(AMP_LOAD_CPU_USAGE, QuerySource.DS_5M)
        assert r == f"rollup_5m:{AMP_LOAD_CPU_USAGE}"

    def test_source_resolver_ds1h(self) -> None:
        from app.metrics.series import AMP_LOAD_CPU_USAGE, MetricSourceResolver, QuerySource

        r = MetricSourceResolver().resolve(AMP_LOAD_CPU_USAGE, QuerySource.DS_1H)
        assert r == f"rollup_1h:{AMP_LOAD_CPU_USAGE}"


# ── TestPromQL ─────────────────────────────────────────────────────────────────


class TestPromQL:
    """promql.py — build_selector / build_series_rollup / build_ranking_expr / regex_escape_join 等。"""

    def test_build_selector_no_matchers(self) -> None:
        from app.metrics.promql import build_selector

        assert build_selector("amp_load_cpu_usage", []) == "amp_load_cpu_usage"

    def test_build_selector_with_eq_matcher(self) -> None:
        from app.metrics.filters import LabelMatcher
        from app.metrics.promql import build_selector

        m = LabelMatcher(label="aic", op="=", value="agent-1")
        result = build_selector("amp_load_cpu_usage", [m])
        assert result == 'amp_load_cpu_usage{aic="agent-1"}'

    def test_build_selector_with_in_matcher(self) -> None:
        from app.metrics.filters import LabelMatcher
        from app.metrics.promql import build_selector

        m = LabelMatcher(label="aic", op="in", value=["a1", "a2"])
        result = build_selector("amp_load_cpu_usage", [m])
        assert "aic=~" in result
        assert "a1" in result
        assert "a2" in result

    def test_build_series_rollup_avg(self) -> None:
        from app.metrics.promql import build_series_rollup

        expr = build_series_rollup("sel", "avg", 60_000)
        assert expr == "avg_over_time(sel[60000ms])"

    def test_build_series_rollup_latest(self) -> None:
        from app.metrics.promql import build_series_rollup

        expr = build_series_rollup("sel", "latest", 15_000)
        assert expr == "last_over_time(sel[15000ms])"

    def test_build_series_rollup_quantile(self) -> None:
        from app.metrics.promql import build_series_rollup

        expr = build_series_rollup("sel", "p95", 300_000)
        assert "quantile_over_time(0.95" in expr

    def test_apply_series_reducer_no_group(self) -> None:
        from app.metrics.promql import apply_series_reducer

        result = apply_series_reducer("sum", "avg_over_time(sel[60000ms])", [])
        assert result == "avg_over_time(sel[60000ms])"

    def test_apply_series_reducer_with_group(self) -> None:
        from app.metrics.promql import apply_series_reducer

        result = apply_series_reducer("sum", "avg_over_time(sel[60000ms])", ["service_name"])
        assert "sum by (service_name)" in result

    def test_build_ranking_expr_topk(self) -> None:
        from app.metrics.promql import build_ranking_expr

        expr = build_ranking_expr("score_expr", 10, "desc")
        assert expr.startswith("topk(10,")

    def test_build_ranking_expr_bottomk(self) -> None:
        from app.metrics.promql import build_ranking_expr

        expr = build_ranking_expr("score_expr", 5, "asc")
        assert expr.startswith("bottomk(5,")

    def test_regex_escape_join_escapes_special_chars(self) -> None:
        from app.metrics.promql import regex_escape_join

        result = regex_escape_join(["a.1", "b+2", "c"])
        assert r"a\.1" in result
        assert r"b\+2" in result
        assert "c" in result
        assert "|" in result

    def test_build_snapshot_anchor_expr(self) -> None:
        from app.metrics.promql import build_snapshot_anchor_expr
        from app.metrics.series import AMP_SNAPSHOT_PRESENT

        expr = build_snapshot_anchor_expr(["aic-1", "aic-2"], "600000ms")
        assert AMP_SNAPSHOT_PRESENT in expr
        assert "tlast_over_time" in expr
        assert "600000ms" in expr


# ── TestPlanner ────────────────────────────────────────────────────────────────


class TestPlanner:
    """planner.py — plan_source_and_step / fold_reducer / aggregation validation。"""

    _NOW_MS = 1_750_000_000_000
    _ONE_DAY_MS = 86_400_000

    def test_plan_source_raw_short_range(self) -> None:
        from app.metrics.planner import plan_source_and_step
        from app.metrics.series import QuerySource

        # 1 小时范围，最近数据 → RAW 源，步长取阶梯最接近值
        end_ms = self._NOW_MS
        start_ms = end_ms - 3_600_000
        plan = plan_source_and_step(start_ms, end_ms, self._NOW_MS, 30, 90, None, 10_000)
        assert plan.source.kind == QuerySource.RAW
        assert plan.step_ms >= 15_000

    def test_plan_source_respects_requested_step(self) -> None:
        from app.metrics.planner import plan_source_and_step

        end_ms = self._NOW_MS
        start_ms = end_ms - 3_600_000
        plan = plan_source_and_step(start_ms, end_ms, self._NOW_MS, 30, 90, 60_000, 10_000)
        assert plan.step_ms == 60_000

    def test_plan_source_step_too_fine_raises(self) -> None:
        from app.metrics.exception import StepTooFineError
        from app.metrics.planner import plan_source_and_step

        end_ms = self._NOW_MS
        # 1 天范围要求 1 秒步长（86400 点，超 max_points=100）
        start_ms = end_ms - 86_400_000
        with pytest.raises(StepTooFineError):
            plan_source_and_step(start_ms, end_ms, self._NOW_MS, 30, 90, 1_000, 100)

    def test_plan_source_out_of_retention_raises(self) -> None:
        from app.metrics.exception import OutOfRetentionError
        from app.metrics.planner import plan_source_and_step

        end_ms = self._NOW_MS
        # 数据比所有保留窗口都老（120 天前 > 30+90=120d，边界外 1ms）
        start_ms = end_ms - 120 * 86_400_000 - 1
        with pytest.raises(OutOfRetentionError):
            plan_source_and_step(start_ms, end_ms, self._NOW_MS, 30, 90, None, 10_000)

    def test_fold_reducer_additive(self) -> None:
        from app.metrics.planner import fold_reducer
        from app.metrics.series import AMP_LOAD_ACTIVE_TASKS

        assert fold_reducer(AMP_LOAD_ACTIVE_TASKS) == "sum"

    def test_fold_reducer_peak(self) -> None:
        from app.metrics.planner import fold_reducer
        from app.metrics.series import AMP_WINDOW_PEAK_THROUGHPUT_MBPS

        assert fold_reducer(AMP_WINDOW_PEAK_THROUGHPUT_MBPS) == "max"

    def test_fold_reducer_avg_for_cpu(self) -> None:
        from app.metrics.planner import fold_reducer
        from app.metrics.series import AMP_LOAD_CPU_USAGE

        assert fold_reducer(AMP_LOAD_CPU_USAGE) == "avg"

    def test_ensure_uptime_not_folded_raises_when_fold(self) -> None:
        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.planner import ensure_uptime_not_folded
        from app.metrics.series import AMP_LOAD_UPTIME_SECONDS

        with pytest.raises(UnsupportedFieldError):
            ensure_uptime_not_folded(AMP_LOAD_UPTIME_SECONDS, group_by_aic=False)

    def test_ensure_uptime_not_folded_ok_when_grouped(self) -> None:
        from app.metrics.planner import ensure_uptime_not_folded
        from app.metrics.series import AMP_LOAD_UPTIME_SECONDS

        ensure_uptime_not_folded(AMP_LOAD_UPTIME_SECONDS, group_by_aic=True)

    def test_validate_series_aggregation_ok(self) -> None:
        from acps_sdk.amp.metrics_catalog import MetricFamily

        from app.metrics.planner import validate_series_aggregation

        validate_series_aggregation(MetricFamily.RESOURCE_USAGE_GAUGE, "avg")

    def test_validate_series_aggregation_invalid(self) -> None:
        from acps_sdk.amp.metrics_catalog import MetricFamily

        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.planner import validate_series_aggregation

        with pytest.raises(UnsupportedFieldError):
            validate_series_aggregation(MetricFamily.WINDOW_RATE_LATENCY, "sum")

    def test_validate_aggregation_monotonic_uptime_only_latest_max(self) -> None:
        from acps_sdk.amp.metrics_catalog import MetricFamily

        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.planner import validate_series_aggregation

        validate_series_aggregation(MetricFamily.MONOTONIC_UPTIME_GAUGE, "latest")
        validate_series_aggregation(MetricFamily.MONOTONIC_UPTIME_GAUGE, "max")
        with pytest.raises(UnsupportedFieldError):
            validate_series_aggregation(MetricFamily.MONOTONIC_UPTIME_GAUGE, "avg")

    def test_plan_capacity_step_minimum_15s(self) -> None:
        from app.metrics.planner import plan_capacity_step

        # 很短回看，步长应 >= 15000ms
        step = plan_capacity_step(60_000, 100)
        assert step >= 15_000


# ── TestFilters ────────────────────────────────────────────────────────────────


class TestFilters:
    """filters.py — validate_label_filter / validate_group_by / build_aic_matcher。"""

    def test_validate_label_filter_ok(self) -> None:
        from app.metrics.filters import validate_label_filter

        m = validate_label_filter("aic", "=", "agent-1")
        assert m.label == "aic"
        assert m.render() == 'aic="agent-1"'

    def test_validate_label_filter_disallowed_label(self) -> None:
        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.filters import validate_label_filter

        with pytest.raises(UnsupportedFieldError):
            validate_label_filter("host_name", "=", "box-1")

    def test_validate_label_filter_window_unknown_value(self) -> None:
        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.filters import validate_label_filter

        with pytest.raises(UnsupportedFieldError):
            validate_label_filter("window", "=", "PT99M")

    def test_validate_label_filter_window_known_value_ok(self) -> None:
        from app.metrics.filters import validate_label_filter

        m = validate_label_filter("window", "=", "PT5M")
        assert m.value == "PT5M"

    def test_validate_group_by_ok(self) -> None:
        from app.metrics.filters import validate_group_by

        result = validate_group_by(["aic", "service_name"])
        assert result == ["aic", "service_name"]

    def test_validate_group_by_disallowed(self) -> None:
        from app.metrics.exception import UnsupportedFieldError
        from app.metrics.filters import validate_group_by

        with pytest.raises(UnsupportedFieldError):
            validate_group_by(["window"])

    def test_build_aic_matcher_single(self) -> None:
        from app.metrics.filters import build_aic_matcher

        m = build_aic_matcher(["aic-1"])
        assert m.op == "="
        assert m.render() == 'aic="aic-1"'

    def test_build_aic_matcher_multi(self) -> None:
        from app.metrics.filters import build_aic_matcher

        m = build_aic_matcher(["aic-1", "aic-2"])
        assert m.op == "in"
        rendered = m.render()
        # render() 正则转义 '-' → '\-'，但两个 AIC 都在 =~ 表达式中
        assert 'aic=~"' in rendered
        assert "aic" in rendered
        assert "|" in rendered  # 多值以 | 连接

    def test_build_aic_matcher_empty_raises(self) -> None:
        from app.metrics.exception import MetricUnsupportedError
        from app.metrics.filters import build_aic_matcher

        with pytest.raises(MetricUnsupportedError):
            build_aic_matcher([])

    def test_validate_time_range_ms_ok(self) -> None:
        from app.metrics.filters import validate_time_range_ms

        validate_time_range_ms(1000, 2000)

    def test_validate_time_range_ms_start_gte_end(self) -> None:
        from app.metrics.exception import InvalidTimeRangeError
        from app.metrics.filters import validate_time_range_ms

        with pytest.raises(InvalidTimeRangeError):
            validate_time_range_ms(2000, 1000)

    def test_validate_time_range_ms_max_exceeded(self) -> None:
        from app.metrics.exception import InvalidTimeRangeError
        from app.metrics.filters import validate_time_range_ms

        with pytest.raises(InvalidTimeRangeError):
            validate_time_range_ms(0, 10_000, max_range_ms=5_000)


# ── TestSamples ────────────────────────────────────────────────────────────────


class TestSamples:
    """samples.py — expand_metrics_body。"""

    def _make_body(self) -> MetricsBody:
        from acps_sdk.amp.models import LoadMetrics, MetricsBody, WindowMetrics

        return MetricsBody(
            uptime_seconds=123.0,
            load_metrics=LoadMetrics(
                active_tasks=5,
                queued_tasks=2,
                cpu_usage=42.5,
            ),
            window_metrics=[
                WindowMetrics(
                    window="PT5M",
                    success_rate=99.0,
                    request_total=100,
                    p95_latency_ms=120.0,
                    p99_latency_ms=200.0,
                )
            ],
        )

    def test_expand_produces_samples(self) -> None:
        from app.metrics.samples import expand_metrics_body

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        assert len(samples) > 0

    def test_expand_all_same_timestamp(self) -> None:
        from app.metrics.samples import expand_metrics_body

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        assert all(s.timestamp_ms == 1_000_000 for s in samples), "C-METRIC-WRITE-2: 全部样本时间戳必须一致"

    def test_expand_aic_in_all_labels(self) -> None:
        from app.metrics.samples import expand_metrics_body

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-007", body=body, resource=None, observed_at_ms=1_000_000)
        assert all(s.labels.get("aic") == "aic-007" for s in samples)

    def test_expand_resource_labels_derived(self) -> None:
        from app.metrics.samples import expand_metrics_body

        body = self._make_body()
        resource = {"service.name": "demo", "service.namespace": "ns", "host.name": "box"}
        samples = expand_metrics_body(aic="aic-1", body=body, resource=resource, observed_at_ms=1_000_000)
        load_sample = next(s for s in samples if "cpu" in s.metric_name)
        assert load_sample.labels["service_name"] == "demo"
        assert "host_name" not in load_sample.labels  # 高基数字段过滤

    def test_expand_quantile_series_has_quantile_label(self) -> None:
        from app.metrics.samples import expand_metrics_body
        from app.metrics.series import AMP_WINDOW_LATENCY_MS

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        latency_samples = [s for s in samples if s.metric_name == AMP_WINDOW_LATENCY_MS]
        assert len(latency_samples) >= 2
        quantile_tags = {s.labels["quantile"] for s in latency_samples}
        assert "p95" in quantile_tags
        assert "p99" in quantile_tags

    def test_expand_includes_snapshot_present(self) -> None:
        from app.metrics.samples import expand_metrics_body
        from app.metrics.series import AMP_SNAPSHOT_PRESENT

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        anchor = [s for s in samples if s.metric_name == AMP_SNAPSHOT_PRESENT]
        assert len(anchor) == 1
        assert anchor[0].value == 1.0

    def test_expand_values_are_float(self) -> None:
        from app.metrics.samples import expand_metrics_body

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        assert all(isinstance(s.value, float) for s in samples), "偏异 D-2: 全部样本值必须为 float"

    def test_expand_uptime_present(self) -> None:
        from app.metrics.samples import expand_metrics_body
        from app.metrics.series import AMP_LOAD_UPTIME_SECONDS

        body = self._make_body()
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        uptime = [s for s in samples if s.metric_name == AMP_LOAD_UPTIME_SECONDS]
        assert len(uptime) == 1
        assert uptime[0].value == 123.0

    def test_expand_no_max_tasks_no_sample(self) -> None:
        """max_active_tasks / max_queued_tasks 缺省时不产样本（C-METRIC-MODEL-3）。"""
        from acps_sdk.amp.models import LoadMetrics, MetricsBody

        from app.metrics.samples import expand_metrics_body
        from app.metrics.series import AMP_LOAD_MAX_ACTIVE_TASKS

        body = MetricsBody(load_metrics=LoadMetrics(active_tasks=3, queued_tasks=1))
        samples = expand_metrics_body(aic="aic-1", body=body, resource=None, observed_at_ms=1_000_000)
        assert not any(s.metric_name == AMP_LOAD_MAX_ACTIVE_TASKS for s in samples)


# ── TestCursor ─────────────────────────────────────────────────────────────────


class TestCursor:
    """cursor.py — filter_fingerprint / encode_cursor / decode_cursor。"""

    def test_fingerprint_deterministic(self) -> None:
        from app.metrics.cursor import filter_fingerprint

        fp1 = filter_fingerprint(None, None)
        fp2 = filter_fingerprint(None, None)
        assert fp1 == fp2

    def test_fingerprint_changes_with_filter(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.metrics.cursor import filter_fingerprint

        f = AMPFilter(conditions=[AMPFilterCondition(field="aic", op="eq", value="x")])
        fp_none = filter_fingerprint(None, None)
        fp_filter = filter_fingerprint(f, None)
        assert fp_none != fp_filter

    def test_fingerprint_length_16(self) -> None:
        from app.metrics.cursor import filter_fingerprint

        fp = filter_fingerprint(None, None)
        assert len(fp) == 16

    def test_encode_decode_roundtrip(self) -> None:
        from app.metrics.cursor import SnapshotCursor, decode_cursor, encode_cursor

        cursor = SnapshotCursor(observed_at_ms=1_700_000_000_000, aic="aic-001", fingerprint="abcdef1234567890")
        encoded = encode_cursor(cursor)
        decoded = decode_cursor(encoded, "abcdef1234567890")
        assert decoded.observed_at_ms == 1_700_000_000_000
        assert decoded.aic == "aic-001"

    def test_decode_fingerprint_mismatch_raises(self) -> None:
        from app.metrics.cursor import SnapshotCursor, decode_cursor, encode_cursor
        from app.metrics.exception import CursorInvalidError

        cursor = SnapshotCursor(observed_at_ms=1_000_000, aic="a", fingerprint="fp1234567890abcd")
        encoded = encode_cursor(cursor)
        with pytest.raises(CursorInvalidError):
            decode_cursor(encoded, "different_fp____")

    def test_decode_malformed_raises(self) -> None:
        from app.metrics.cursor import decode_cursor
        from app.metrics.exception import CursorInvalidError

        with pytest.raises(CursorInvalidError):
            decode_cursor("!!!notbase64!!!", "fp1234567890abcd")

    def test_filter_fingerprint_windows_order_stable(self) -> None:
        """window 列表顺序不同但内容相同 → 同一指纹。"""
        from app.metrics.cursor import filter_fingerprint

        fp1 = filter_fingerprint(None, ["PT5M", "PT1M"])
        fp2 = filter_fingerprint(None, ["PT1M", "PT5M"])
        assert fp1 == fp2


# ── TestValidateMetricsConfig ──────────────────────────────────────────────────


class TestValidateMetricsConfig:
    """runtime.py — validate_metrics_config（testing.toml 下应通过）。"""

    def test_valid_config_passes(self) -> None:
        """testing 环境的配置应通过全量校验。"""
        from app.metrics.runtime import validate_metrics_config

        validate_metrics_config()  # 不抛出即通过

    def test_invalid_iso_duration_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.metrics.exception import MetricsConfigError
        from app.metrics.runtime import validate_metrics_config

        monkeypatch.setattr(
            "app.metrics.runtime.get_settings",
            lambda: _make_settings_with_bad_duration(),
        )
        with pytest.raises(MetricsConfigError, match="ISO 8601"):
            validate_metrics_config()


def _make_settings_with_bad_duration() -> object:
    """返回一个 snapshot_fallback_lookback 非法的 Settings 伪对象。"""

    class FakeSettings:
        metrics_writer_poll_timeout_ms = 1000
        metrics_remote_write_batch_interval_seconds = 5
        metrics_remote_write_batch_max_samples = 10_000
        metrics_snapshot_ttl_seconds = 600
        metrics_snapshot_index_scan_batch_size = 500
        metrics_dedupe_ttl_seconds = 86_400
        metrics_raw_retention_days = 30
        metrics_downsample_retention_days = 90
        metrics_lagging_threshold_ms = 150_000
        metrics_max_points_per_series = 10_000
        metrics_ranking_max_top_n = 200
        metrics_slo_max_rules = 20
        metrics_query_timeout_seconds = 30
        metrics_metrics_log_interval_seconds = 60
        metrics_capacity_default_active_ratio_threshold = 0.8
        metrics_capacity_default_queue_ratio_threshold = 0.8
        metrics_snapshot_fallback_lookback = "INVALID"  # 非法值
        metrics_capacity_default_lookback = "PT10M"
        metrics_lagging_response_mode = "503"

    return FakeSettings()
