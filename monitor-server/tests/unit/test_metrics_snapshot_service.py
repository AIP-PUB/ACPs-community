"""tests/unit/test_metrics_snapshot_service.py — snapshot_service.py 单元测试。

覆盖范围：
- _ensure_snapshot_sort
- _match_label_matchers
- _trim_windows
- _cached_to_view
- _snap_value_getter
- query_snapshots（mock Redis + TSDB）
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acps_sdk.amp.models import LoadMetrics, WindowMetrics

from app.metrics.exception import UnsupportedFieldError
from app.metrics.filters import LabelMatcher
from app.metrics.schema import MetricsSnapshotView
from app.metrics.snapshot_cache import CachedSnapshot
from app.metrics.snapshot_service import (
    _cached_to_view,
    _ensure_snapshot_sort,
    _match_label_matchers,
    _snap_value_getter,
    _trim_windows,
)

# ── _ensure_snapshot_sort ────────────────────────────────────────────────────


class TestEnsureSnapshotSort:
    def test_none_is_ok(self) -> None:
        _ensure_snapshot_sort(None)  # should not raise

    def test_empty_list_is_ok(self) -> None:
        _ensure_snapshot_sort([])  # should not raise

    def test_observed_at_desc_ok(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        _ensure_snapshot_sort([AMPSortSpec(field="observedAt", order="desc")])

    def test_aic_asc_ok(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        _ensure_snapshot_sort([AMPSortSpec(field="aic", order="asc")])

    def test_invalid_field_raises(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        with pytest.raises(UnsupportedFieldError):
            _ensure_snapshot_sort([AMPSortSpec(field="aic", order="desc")])

    def test_invalid_sort_field_raises(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        with pytest.raises(UnsupportedFieldError):
            _ensure_snapshot_sort([AMPSortSpec(field="loadMetrics.activeTasks", order="desc")])


# ── _match_label_matchers ──────────────────────────────────────────────────────


class TestMatchLabelMatchers:
    def _make_snap(
        self,
        aic: str = "aic-1",
        service_name: str | None = "svc-a",
        service_namespace: str | None = "ns-a",
        deployment_env: str | None = "prod",
    ) -> CachedSnapshot:
        return CachedSnapshot(
            aic=aic,
            observed_at_ms=1000,
            uptime_seconds=100.0,
            load_metrics=None,
            window_metrics=None,
            service_name=service_name,
            service_namespace=service_namespace,
            deployment_env=deployment_env,
        )

    def test_no_matchers_always_true(self) -> None:
        snap = self._make_snap()
        assert _match_label_matchers(snap, []) is True

    def test_eq_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="service_name", op="=", value="svc-a")]
        assert _match_label_matchers(snap, matchers) is True

    def test_eq_no_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="service_name", op="=", value="svc-b")]
        assert _match_label_matchers(snap, matchers) is False

    def test_neq_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="service_name", op="!=", value="svc-b")]
        assert _match_label_matchers(snap, matchers) is True

    def test_in_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="aic", op="in", value=["aic-1", "aic-2"])]
        assert _match_label_matchers(snap, matchers) is True

    def test_in_no_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="aic", op="in", value=["aic-2", "aic-3"])]
        assert _match_label_matchers(snap, matchers) is False

    def test_regex_match(self) -> None:
        snap = self._make_snap()
        matchers = [LabelMatcher(label="service_name", op="=~", value="svc-.*")]
        assert _match_label_matchers(snap, matchers) is True

    def test_missing_label_eq_empty_string(self) -> None:
        snap = self._make_snap(service_name=None)
        matchers = [LabelMatcher(label="service_name", op="=", value="")]
        assert _match_label_matchers(snap, matchers) is True

    def test_missing_label_neq_nonempty(self) -> None:
        snap = self._make_snap(service_name=None)
        matchers = [LabelMatcher(label="service_name", op="!=", value="svc-a")]
        assert _match_label_matchers(snap, matchers) is True


# ── _trim_windows ─────────────────────────────────────────────────────────────


class TestTrimWindows:
    def _make_wm(self, window: str) -> WindowMetrics:
        return WindowMetrics(window=window, success_rate=99.0)

    def test_none_windows_no_trim(self) -> None:
        wms = [self._make_wm("PT5M"), self._make_wm("PT1H")]
        result = _trim_windows(wms, None)
        assert result == wms

    def test_requested_windows_trims(self) -> None:
        wms = [self._make_wm("PT5M"), self._make_wm("PT1H")]
        result = _trim_windows(wms, {"PT5M"})
        assert result is not None
        assert len(result) == 1
        assert result[0].window == "PT5M"

    def test_no_match_returns_none(self) -> None:
        wms = [self._make_wm("PT5M")]
        result = _trim_windows(wms, {"PT1H"})
        assert result is None

    def test_none_wm_returns_none(self) -> None:
        result = _trim_windows(None, {"PT5M"})
        assert result is None


# ── _cached_to_view ───────────────────────────────────────────────────────────


class TestCachedToView:
    def test_basic_conversion(self) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        snap = CachedSnapshot(
            aic="aic-1",
            observed_at_ms=now_ms,
            uptime_seconds=100.0,
            load_metrics=None,
            window_metrics=None,
            service_name="svc",
            service_namespace=None,
            deployment_env=None,
        )
        view = _cached_to_view(snap, None)
        assert view.aic == "aic-1"
        assert view.uptime_seconds == 100.0
        assert view.load_metrics is None

    def test_window_trim_applied(self) -> None:
        wms = [
            WindowMetrics(window="PT5M", success_rate=99.0),
            WindowMetrics(window="PT1H", success_rate=98.0),
        ]
        snap = CachedSnapshot(
            aic="aic-1",
            observed_at_ms=1000,
            uptime_seconds=None,
            load_metrics=None,
            window_metrics=wms,
            service_name=None,
            service_namespace=None,
            deployment_env=None,
        )
        view = _cached_to_view(snap, {"PT5M"})
        assert view.window_metrics is not None
        assert len(view.window_metrics) == 1
        assert view.window_metrics[0].window == "PT5M"


# ── _snap_value_getter ────────────────────────────────────────────────────────


class TestSnapValueGetter:
    def _make_view(self) -> MetricsSnapshotView:
        now_iso = datetime.now(UTC).isoformat()
        load = LoadMetrics(active_tasks=10, queued_tasks=5, max_active_tasks=20, max_queued_tasks=15)
        wms = [WindowMetrics(window="PT5M", success_rate=99.0, p95_latency_ms=50.0)]
        return MetricsSnapshotView(
            aic="aic-1",
            observed_at=now_iso,
            load_metrics=load,
            window_metrics=wms,
        )

    def test_load_metrics_field(self) -> None:
        view = self._make_view()
        assert _snap_value_getter(view, "loadMetrics.activeTasks") == 10.0

    def test_window_metrics_field(self) -> None:
        view = self._make_view()
        v = _snap_value_getter(view, "windowMetrics.successRate")
        assert v == 99.0

    def test_unknown_path_returns_none(self) -> None:
        view = self._make_view()
        assert _snap_value_getter(view, "unknown.field") is None

    def test_none_load_metrics_returns_none(self) -> None:
        from app.metrics.schema import MetricsSnapshotView

        view = MetricsSnapshotView(aic="a", observed_at="2025-01-01T00:00:00+00:00")
        assert _snap_value_getter(view, "loadMetrics.activeTasks") is None


# ── query_snapshots (mock Redis + TSDB) ──────────────────────────────────────


class TestQuerySnapshots:
    def _make_cached_snap(self, aic: str, ts_ms: int) -> CachedSnapshot:
        return CachedSnapshot(
            aic=aic,
            observed_at_ms=ts_ms,
            uptime_seconds=100.0,
            load_metrics=None,
            window_metrics=None,
            service_name="svc",
            service_namespace="ns",
            deployment_env="prod",
        )

    @pytest.mark.asyncio
    async def test_basic_snapshot_query(self) -> None:
        from app.metrics.schema import MetricsSnapshotQueryRequest

        req = MetricsSnapshotQueryRequest()

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        snap = self._make_cached_snap("aic-1", now_ms)

        from app.core.amp_api_schema import AMPResponseMeta

        mock_meta = AMPResponseMeta(
            data_freshness_at=datetime.now(UTC).isoformat(),
            ingestion_lag_ms=1000,
        )
        mock_freshness = MagicMock()
        mock_freshness.data_freshness_at_ms = now_ms
        mock_freshness.ingestion_lag_ms = 1000
        mock_freshness.lagging = False

        redis = AsyncMock()

        with (
            patch("app.metrics.snapshot_service.mget_snapshots", new=AsyncMock(return_value=[snap])),
            patch("app.metrics.snapshot_service.scan_index_desc", new=AsyncMock(return_value=[("aic-1", now_ms)])),
            patch("app.metrics.snapshot_service.evaluate_freshness", new=AsyncMock(return_value=mock_freshness)),
            patch("app.metrics.snapshot_service.build_meta", return_value=mock_meta),
            patch("app.metrics.snapshot_service.apply_degrade_policy", return_value=False),
            patch("app.metrics.snapshot_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.metrics_snapshot_fallback_lookback = "PT10M"
            mock_settings.return_value.metrics_snapshot_index_scan_batch_size = 500

            from app.metrics.snapshot_service import query_snapshots

            items, meta = await query_snapshots(redis, req)

        assert len(items) >= 1
        assert items[0].aic == "aic-1"
        assert meta is mock_meta

    @pytest.mark.asyncio
    async def test_static_aics_query(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.metrics.schema import MetricsSnapshotQueryRequest

        req = MetricsSnapshotQueryRequest(
            filter=AMPFilter(conditions=[AMPFilterCondition(field="aic", op="eq", value="aic-1")])
        )

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        snap = self._make_cached_snap("aic-1", now_ms)

        from app.core.amp_api_schema import AMPResponseMeta

        mock_meta = AMPResponseMeta(
            data_freshness_at=datetime.now(UTC).isoformat(),
        )
        mock_freshness = MagicMock()
        mock_freshness.data_freshness_at_ms = now_ms
        mock_freshness.ingestion_lag_ms = None
        mock_freshness.lagging = False

        redis = AsyncMock()

        with (
            patch("app.metrics.snapshot_service.mget_snapshots", new=AsyncMock(return_value=[snap])),
            patch("app.metrics.snapshot_service.evaluate_freshness", new=AsyncMock(return_value=mock_freshness)),
            patch("app.metrics.snapshot_service.build_meta", return_value=mock_meta),
            patch("app.metrics.snapshot_service.apply_degrade_policy", return_value=False),
            patch("app.metrics.snapshot_service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.metrics_snapshot_fallback_lookback = "PT10M"
            mock_settings.return_value.metrics_snapshot_index_scan_batch_size = 500

            from app.metrics.snapshot_service import query_snapshots

            items, _meta = await query_snapshots(redis, req)

        assert len(items) == 1
        assert items[0].aic == "aic-1"

    @pytest.mark.asyncio
    async def test_invalid_sort_raises(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec
        from app.metrics.schema import MetricsSnapshotQueryRequest

        req = MetricsSnapshotQueryRequest(sort=[AMPSortSpec(field="activeTasks", order="desc")])

        redis = AsyncMock()
        with patch("app.metrics.snapshot_service.get_settings") as mock_settings:
            mock_settings.return_value.metrics_snapshot_fallback_lookback = "PT10M"
            mock_settings.return_value.metrics_snapshot_index_scan_batch_size = 500

            from app.metrics.snapshot_service import query_snapshots

            with pytest.raises(UnsupportedFieldError):
                await query_snapshots(redis, req)
