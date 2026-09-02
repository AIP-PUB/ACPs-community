"""tests/unit/test_access_planner.py — 查询规划器测试。

TDD B-6：先写测试（红）→ 实现 planner.py（绿）。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest


def _make_time_range(start: str, end: str) -> Any:
    from app.core.amp_api_schema import AMPTimeRange

    return AMPTimeRange(start_at=start, end_at=end)


def _make_page(limit: int = 50, cursor: str | None = None) -> Any:
    from app.core.amp_api_schema import AMPPaginationRequest

    return AMPPaginationRequest(limit=limit, cursor=cursor)


class TestRequireTimeRange:
    def test_none_raises(self) -> None:
        from app.access.exception import InvalidTimeRangeError
        from app.access.planner import require_time_range

        with pytest.raises(InvalidTimeRangeError):
            require_time_range(None)

    def test_valid_returns_tuple(self) -> None:
        from app.access.planner import require_time_range

        tr = _make_time_range("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        from_ms, to_ms = require_time_range(tr)
        assert from_ms < to_ms
        assert isinstance(from_ms, int)
        assert isinstance(to_ms, int)

    def test_start_equal_end_raises(self) -> None:
        from app.access.exception import InvalidTimeRangeError
        from app.access.planner import require_time_range

        tr = _make_time_range("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            require_time_range(tr)

    def test_start_after_end_raises(self) -> None:
        from app.access.exception import InvalidTimeRangeError
        from app.access.planner import require_time_range

        tr = _make_time_range("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            require_time_range(tr)


class TestAssertWithinRetention:
    def _now_ms(self) -> int:
        from datetime import datetime

        return int(datetime.now(UTC).timestamp() * 1000)

    def test_recent_from_passes(self) -> None:
        from app.access.planner import assert_within_retention

        now_ms = self._now_ms()
        from_ms = now_ms - 86_400_000  # 1 day ago
        assert_within_retention(from_ms, retention_days=30, now_ms=now_ms)

    def test_too_old_raises(self) -> None:
        from app.access.exception import OutOfRetentionError
        from app.access.planner import assert_within_retention

        now_ms = self._now_ms()
        from_ms = now_ms - 40 * 86_400_000  # 40 days ago
        with pytest.raises(OutOfRetentionError):
            assert_within_retention(from_ms, retention_days=30, now_ms=now_ms)

    def test_exactly_at_boundary_passes(self) -> None:
        from app.access.planner import assert_within_retention

        now_ms = self._now_ms()
        from_ms = now_ms - 30 * 86_400_000  # exact boundary
        # boundary is inclusive (gte oldest ms) — should pass
        assert_within_retention(from_ms, retention_days=30, now_ms=now_ms)


class TestAlignTopologyBuckets:
    def test_floor_start_ceil_end(self) -> None:
        # 2026-01-01 00:03:00 UTC → floor to 00:00
        # 2026-01-01 00:07:00 UTC → ceil to 00:10
        from datetime import datetime

        from app.access.planner import align_topology_buckets

        start = int(datetime(2026, 1, 1, 0, 3, 0, tzinfo=UTC).timestamp() * 1000)
        end = int(datetime(2026, 1, 1, 0, 7, 0, tzinfo=UTC).timestamp() * 1000)
        fb, tb = align_topology_buckets(start, end)
        five_min_ms = 5 * 60 * 1000
        assert fb % five_min_ms == 0, "from_bucket should align to 5min boundary"
        assert tb % five_min_ms == 0, "to_bucket should align to 5min boundary"
        assert fb <= start
        assert tb >= end

    def test_already_aligned_unchanged(self) -> None:
        # 2026-01-01 00:00 / 00:05 already aligned
        from datetime import datetime

        from app.access.planner import align_topology_buckets

        start = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
        end = int(datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC).timestamp() * 1000)
        fb, tb = align_topology_buckets(start, end)
        assert fb == start
        assert tb == end


class TestTracePartitionExpandHours:
    def test_minimum_one(self) -> None:
        from app.access.planner import trace_partition_expand_hours

        assert trace_partition_expand_hours(configured=0) >= 1

    def test_configured_value_returned(self) -> None:
        from app.access.planner import trace_partition_expand_hours

        assert trace_partition_expand_hours(configured=6) == 6

    def test_negative_returns_one(self) -> None:
        from app.access.planner import trace_partition_expand_hours

        assert trace_partition_expand_hours(configured=-1) >= 1


class TestClampTopN:
    def test_none_returns_default(self) -> None:
        from app.access.planner import clamp_top_n

        assert clamp_top_n(None, default=10, hard_max=100) == 10

    def test_requested_within_max(self) -> None:
        from app.access.planner import clamp_top_n

        assert clamp_top_n(20, default=10, hard_max=100) == 20

    def test_requested_exceeds_max_clamped(self) -> None:
        from app.access.planner import clamp_top_n

        assert clamp_top_n(200, default=10, hard_max=100) == 100

    def test_zero_returns_default(self) -> None:
        from app.access.planner import clamp_top_n

        assert clamp_top_n(0, default=10, hard_max=100) == 10


class TestResolvePageLimit:
    def test_default_limit(self) -> None:
        from app.access.planner import resolve_page_limit

        assert resolve_page_limit(None) == 50

    def test_custom_limit(self) -> None:
        from app.access.planner import resolve_page_limit

        page = _make_page(limit=100)
        assert resolve_page_limit(page) == 100


class TestValidateTopologyGroupBy:
    def test_aic_valid(self) -> None:
        from app.access.planner import validate_topology_group_by

        assert validate_topology_group_by("aic") == "aic"

    def test_service_valid(self) -> None:
        from app.access.planner import validate_topology_group_by

        assert validate_topology_group_by("service") == "service"

    def test_none_defaults_to_aic(self) -> None:
        from app.access.planner import validate_topology_group_by

        assert validate_topology_group_by(None) == "aic"

    def test_invalid_raises(self) -> None:
        from app.access.exception import TopologyGroupByInvalidError
        from app.access.planner import validate_topology_group_by

        with pytest.raises(TopologyGroupByInvalidError):
            validate_topology_group_by("region")


class TestValidateOperationsGroupBy:
    def test_valid_subset(self) -> None:
        from app.access.planner import validate_operations_group_by

        result = validate_operations_group_by(["aic", "service"])
        assert "aic" in result
        assert "service" in result

    def test_none_returns_empty(self) -> None:
        from app.access.planner import validate_operations_group_by

        assert validate_operations_group_by(None) == []

    def test_invalid_field_raises(self) -> None:
        from app.access.exception import InvalidFilterError
        from app.access.planner import validate_operations_group_by

        with pytest.raises(InvalidFilterError):
            validate_operations_group_by(["unknownDim"])


class TestValidateAttributionGroupBy:
    def test_error_code_valid(self) -> None:
        from app.access.planner import validate_attribution_group_by

        result = validate_attribution_group_by(["errorCode"])
        assert "errorCode" in result

    def test_invalid_raises(self) -> None:
        from app.access.exception import TopologyGroupByInvalidError
        from app.access.planner import validate_attribution_group_by

        with pytest.raises((TopologyGroupByInvalidError, Exception)):
            validate_attribution_group_by(["unknownField"])


class TestParseBucketSize:
    def test_five_min(self) -> None:
        from app.access.planner import parse_bucket_size

        result = parse_bucket_size("5m", collapse=False)
        assert result is not None
        assert "5" in result or "300" in result or "MINUTE" in result.upper() or "Minute" in result

    def test_collapse_returns_none(self) -> None:
        from app.access.planner import parse_bucket_size

        assert parse_bucket_size("5m", collapse=True) is None

    def test_none_returns_none(self) -> None:
        from app.access.planner import parse_bucket_size

        assert parse_bucket_size(None, collapse=False) is None

    def test_invalid_size_raises(self) -> None:
        from app.access.exception import InvalidFilterError
        from app.access.planner import parse_bucket_size

        with pytest.raises(InvalidFilterError):
            parse_bucket_size("99x", collapse=False)
