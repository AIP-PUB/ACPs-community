"""tests/unit/test_metrics_service.py — service.py 单元测试。

覆盖范围：
- _parse_iso_duration_ms
- _parse_step_ms
- _parse_time_range / _require_time_range
- _build_group_labels
- _resolve_capacity_thresholds
- _union_candidates
- _slo_meets
- query_series / query_rankings / evaluate_slo / query_capacity（mock TSDB + freshness）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from app.metrics.schema import MetricsCapacityRequest
    from app.metrics.tsdb import InstantSample

from app.metrics.exception import (
    InvalidFilterError,
    InvalidTimeRangeError,
    ReadModelLaggingError,
    SLORuleInvalidError,
)
from app.metrics.service import (
    _build_group_labels,
    _parse_iso_duration_ms,
    _parse_step_ms,
    _parse_time_range,
    _require_time_range,
    _resolve_capacity_thresholds,
    _slo_meets,
    _union_candidates,
    iso_duration_to_promql_range,
    promql_timestamp_to_ms,
)

# ── _parse_iso_duration_ms ────────────────────────────────────────────────────


class TestParseIsoDurationMs:
    def test_pt5m(self) -> None:
        assert _parse_iso_duration_ms("PT5M") == 5 * 60 * 1000

    def test_p1d(self) -> None:
        assert _parse_iso_duration_ms("P1D") == 24 * 3600 * 1000

    def test_pt10m30s(self) -> None:
        assert _parse_iso_duration_ms("PT10M30S") == 10 * 60 * 1000 + 30 * 1000

    def test_pt1h(self) -> None:
        assert _parse_iso_duration_ms("PT1H") == 3600 * 1000

    def test_p1w(self) -> None:
        assert _parse_iso_duration_ms("P1W") == 7 * 24 * 3600 * 1000

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid ISO 8601"):
            _parse_iso_duration_ms("5m")

    def test_zero_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _parse_iso_duration_ms("PT0S")


class TestIsoDurationToPromqlRange:
    def test_pt10m(self) -> None:
        assert iso_duration_to_promql_range("PT10M") == "10m"

    def test_pt1h(self) -> None:
        assert iso_duration_to_promql_range("PT1H") == "1h"

    def test_p1d(self) -> None:
        assert iso_duration_to_promql_range("P1D") == "1d"


class TestPromqlTimestampToMs:
    def test_seconds(self) -> None:
        assert promql_timestamp_to_ms(1_700_000_000.0) == 1_700_000_000_000

    def test_milliseconds(self) -> None:
        assert promql_timestamp_to_ms(1_700_000_000_000.0) == 1_700_000_000_000


# ── _parse_step_ms ────────────────────────────────────────────────────────────


class TestParseStepMs:
    def test_none_returns_none(self) -> None:
        assert _parse_step_ms(None) is None

    def test_iso_duration(self) -> None:
        assert _parse_step_ms("PT1M") == 60 * 1000

    def test_seconds_string(self) -> None:
        assert _parse_step_ms("60") == 60 * 1000

    def test_invalid_raises(self) -> None:
        with pytest.raises(InvalidFilterError):
            _parse_step_ms("not_a_duration")


# ── _require_time_range ───────────────────────────────────────────────────────


class TestRequireTimeRange:
    def test_none_raises(self) -> None:
        with pytest.raises(InvalidTimeRangeError):
            _require_time_range(None)

    def test_valid_returns_same(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange

        tr = AMPTimeRange(start_at="2025-01-01T00:00:00Z", end_at="2025-01-01T01:00:00Z")
        assert _require_time_range(tr) is tr


# ── _parse_time_range ─────────────────────────────────────────────────────────


class TestParseTimeRange:
    def test_valid_range(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange

        tr = AMPTimeRange(start_at="2025-01-01T00:00:00Z", end_at="2025-01-01T01:00:00Z")
        _start_dt, _end_dt, start_ms, end_ms = _parse_time_range(tr)
        assert start_ms < end_ms
        assert end_ms - start_ms == 3600 * 1000

    def test_start_after_end_raises(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange

        tr = AMPTimeRange(start_at="2025-01-01T01:00:00Z", end_at="2025-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            _parse_time_range(tr)

    def test_start_equal_end_raises(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange

        tr = AMPTimeRange(start_at="2025-01-01T00:00:00Z", end_at="2025-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            _parse_time_range(tr)


# ── _build_group_labels ───────────────────────────────────────────────────────


class TestBuildGroupLabels:
    def test_default_includes_aic(self) -> None:
        result = _build_group_labels(None, None)
        assert result == ["aic"]

    def test_group_by_aic_false_no_aic(self) -> None:
        result = _build_group_labels(False, None)
        assert result is None or "aic" not in (result or [])

    def test_group_by_labels_added(self) -> None:
        result = _build_group_labels(True, ["service_name"])
        assert result is not None
        assert "aic" in result
        assert "service_name" in result

    def test_invalid_label_raises(self) -> None:
        from app.metrics.exception import UnsupportedFieldError

        with pytest.raises(UnsupportedFieldError):
            _build_group_labels(True, ["log_id"])

    def test_no_duplicate_aic(self) -> None:
        result = _build_group_labels(True, ["service_name"])
        assert result is not None
        assert result.count("aic") == 1


# ── _resolve_capacity_thresholds ─────────────────────────────────────────────


class TestResolveCapacityThresholds:
    def _make_req(
        self,
        active: float | None = None,
        queue: float | None = None,
    ) -> MetricsCapacityRequest:
        from app.metrics.schema import MetricsCapacityRequest

        return MetricsCapacityRequest(
            active_ratio_threshold=active,
            queue_ratio_threshold=queue,
        )

    def test_both_none_returns_defaults(self) -> None:
        req = self._make_req()
        with patch("app.metrics.service.get_settings") as mock_s:
            mock_s.return_value.metrics_capacity_default_active_ratio_threshold = 0.8
            mock_s.return_value.metrics_capacity_default_queue_ratio_threshold = 0.8
            active, queue = _resolve_capacity_thresholds(req)
        assert active == 0.8
        assert queue == 0.8

    def test_only_active_given(self) -> None:
        req = self._make_req(active=0.9)
        with patch("app.metrics.service.get_settings") as mock_s:
            mock_s.return_value.metrics_capacity_default_active_ratio_threshold = 0.8
            mock_s.return_value.metrics_capacity_default_queue_ratio_threshold = 0.8
            active, queue = _resolve_capacity_thresholds(req)
        assert active == 0.9
        assert queue is None

    def test_invalid_threshold_raises(self) -> None:
        req = self._make_req(active=1.5)
        with patch("app.metrics.service.get_settings") as mock_s:
            mock_s.return_value.metrics_capacity_default_active_ratio_threshold = 0.8
            mock_s.return_value.metrics_capacity_default_queue_ratio_threshold = 0.8
            with pytest.raises(InvalidFilterError):
                _resolve_capacity_thresholds(req)


# ── _union_candidates ─────────────────────────────────────────────────────────


class TestUnionCandidates:
    def _make_sample(self, aic: str, value: float) -> InstantSample:
        from app.metrics.tsdb import InstantSample

        return InstantSample(labels={"aic": aic}, value=value, timestamp_ms=0)

    def test_union_both_sides(self) -> None:
        active = [self._make_sample("a1", 0.9), self._make_sample("a2", 0.3)]
        queue = [self._make_sample("b1", 0.8), self._make_sample("a1", 0.9)]
        result = _union_candidates(active, queue, 0.8, 0.8)
        assert "a1" in result
        assert "b1" in result
        assert "a2" not in result  # 0.3 < 0.8

    def test_only_active_side(self) -> None:
        active = [self._make_sample("a1", 0.9)]
        result = _union_candidates(active, [], 0.8, None)
        assert result == ["a1"]

    def test_empty_returns_empty(self) -> None:
        result = _union_candidates([], [], 0.8, 0.8)
        assert result == []


# ── _slo_meets ────────────────────────────────────────────────────────────────


class TestSloMeets:
    def test_success_rate_meets(self) -> None:
        assert _slo_meets("success_rate", actual=99.0, target=95.0) is True

    def test_success_rate_breach(self) -> None:
        assert _slo_meets("success_rate", actual=90.0, target=95.0) is False

    def test_latency_meets(self) -> None:
        assert _slo_meets("p95_latency_ms", actual=100.0, target=200.0) is True

    def test_latency_breach(self) -> None:
        assert _slo_meets("p95_latency_ms", actual=300.0, target=200.0) is False


# ── query_series (integration-style mock) ────────────────────────────────────


class TestQuerySeries:
    @pytest.mark.asyncio
    async def test_returns_series_and_meta(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange
        from app.metrics.schema import MetricsSeriesQueryRequest
        from app.metrics.tsdb import RangeSeries

        # 使用近期时间避免超出 retention
        now_iso = datetime.now(UTC).isoformat()
        one_hour_ago_iso = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 3600, tz=UTC).isoformat()

        req = MetricsSeriesQueryRequest(
            metric="activeTasks",
            time_range=AMPTimeRange(
                start_at=one_hour_ago_iso,
                end_at=now_iso,
            ),
        )

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        mock_series = RangeSeries(
            labels={"aic": "aic-1", "__name__": "amp_load_active_tasks"},
            points=[(now_ms - 60000, 42.0), (now_ms, 43.0)],
        )

        from app.core.amp_api_schema import AMPResponseMeta

        mock_meta = AMPResponseMeta(
            data_freshness_at=now_iso,
            ingestion_lag_ms=5000,
        )

        mock_freshness = MagicMock()
        mock_freshness.data_freshness_at_ms = now_ms
        mock_freshness.ingestion_lag_ms = 5000
        mock_freshness.lagging = False

        with (
            patch("app.metrics.service.tsdb.range_query", new=AsyncMock(return_value=[mock_series])),
            patch("app.metrics.service.evaluate_freshness", new=AsyncMock(return_value=mock_freshness)),
            patch("app.metrics.service.build_meta", return_value=mock_meta),
            patch("app.metrics.service.apply_degrade_policy", return_value=False),
            patch("app.metrics.service.get_redis", return_value=MagicMock()),
            patch("app.metrics.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.metrics_raw_retention_days = 30
            mock_settings.return_value.metrics_downsample_retention_days = 90
            mock_settings.return_value.metrics_max_points_per_series = 10000

            from app.metrics.service import query_series

            items, meta = await query_series(req)

        assert len(items) == 1
        assert items[0].metric == "activeTasks"
        assert len(items[0].points) == 2
        assert meta is mock_meta

    @pytest.mark.asyncio
    async def test_tsdb_error_raises_read_model_lagging(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange
        from app.metrics.schema import MetricsSeriesQueryRequest

        now_iso = datetime.now(UTC).isoformat()
        one_hour_ago_iso = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 3600, tz=UTC).isoformat()

        req = MetricsSeriesQueryRequest(
            metric="activeTasks",
            time_range=AMPTimeRange(
                start_at=one_hour_ago_iso,
                end_at=now_iso,
            ),
        )

        with (
            patch("app.metrics.service.tsdb.range_query", new=AsyncMock(side_effect=Exception("TSDB down"))),
            patch("app.metrics.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.metrics_raw_retention_days = 30
            mock_settings.return_value.metrics_downsample_retention_days = 90
            mock_settings.return_value.metrics_max_points_per_series = 10000

            from app.metrics.service import query_series

            with pytest.raises(ReadModelLaggingError):
                await query_series(req)


# ── evaluate_slo (integration-style mock) ────────────────────────────────────


class TestEvaluateSlo:
    @pytest.mark.asyncio
    async def test_basic_slo_evaluation(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange
        from app.metrics.schema import MetricsSLOEvaluateRequest, MetricsSLORule
        from app.metrics.tsdb import InstantSample

        req = MetricsSLOEvaluateRequest(
            time_range=AMPTimeRange(
                start_at="2025-01-01T00:00:00Z",
                end_at="2025-01-01T01:00:00Z",
            ),
            rules=[MetricsSLORule(sli="success_rate", window="PT5M", target=95.0)],
            include_failed_details=True,
        )

        # actual=90 (breach); ts=time in ms
        actual_sample = InstantSample(
            labels={"aic": "aic-1", "window": "PT5M"},
            value=90.0,
            timestamp_ms=1735689600000,
        )
        ts_sample = InstantSample(
            labels={"aic": "aic-1", "window": "PT5M"},
            value=float(1735689600000),  # ts as epoch ms
            timestamp_ms=1735689600000,
        )

        mock_freshness = MagicMock()
        mock_freshness.data_freshness_at_ms = 1735689600000
        mock_freshness.ingestion_lag_ms = 1000
        mock_freshness.lagging = False

        from app.core.amp_api_schema import AMPResponseMeta

        mock_meta = AMPResponseMeta(
            data_freshness_at="2025-01-01T00:00:00+00:00",
            ingestion_lag_ms=1000,
        )

        with (
            patch(
                "app.metrics.service.tsdb.instant_many",
                new=AsyncMock(
                    side_effect=[
                        {0: [actual_sample]},  # actual_results
                        {0: [ts_sample]},  # ts_results
                    ]
                ),
            ),
            patch("app.metrics.service.evaluate_freshness", new=AsyncMock(return_value=mock_freshness)),
            patch("app.metrics.service.build_meta", return_value=mock_meta),
            patch("app.metrics.service.apply_degrade_policy", return_value=False),
            patch("app.metrics.service.get_redis", return_value=MagicMock()),
            patch("app.metrics.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.metrics_slo_max_rules = 20

            from app.metrics.service import evaluate_slo

            resp = await evaluate_slo(req)

        assert resp.summary.total == 1
        assert resp.summary.breach_count == 1
        assert resp.summary.meets_count == 0
        assert len(resp.items) == 1
        assert resp.items[0].aic == "aic-1"
        assert resp.items[0].meets is False

    @pytest.mark.asyncio
    async def test_too_many_rules_raises(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange
        from app.metrics.schema import MetricsSLOEvaluateRequest, MetricsSLORule

        rules = [MetricsSLORule(sli="success_rate", window="PT5M", target=95.0) for _ in range(25)]
        req = MetricsSLOEvaluateRequest(
            time_range=AMPTimeRange(
                start_at="2025-01-01T00:00:00Z",
                end_at="2025-01-01T01:00:00Z",
            ),
            rules=rules,
        )

        with patch("app.metrics.service.get_settings") as mock_settings:
            mock_settings.return_value.metrics_slo_max_rules = 20
            with pytest.raises(SLORuleInvalidError):
                from app.metrics.service import evaluate_slo

                await evaluate_slo(req)
