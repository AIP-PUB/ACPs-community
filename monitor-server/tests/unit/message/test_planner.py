"""单元测试：B-5 planner.py — 查询规划与协议护栏。"""

from __future__ import annotations

import pytest

from app.core.amp_api_schema import AMPFilter, AMPFilterCondition, AMPPaginationRequest, AMPTimeRange
from app.message.exception import (
    LifecycleKeyRequiredError,
    MessageDestinationRequiredError,
    MessageGroupByInvalidError,
    MessageStepInvalidError,
    OutOfRetentionError,
)
from app.message.planner import (
    align_to_bucket,
    assert_within_retention,
    clamp_deadletter_n,
    compute_rebuild_from,
    parse_throughput_step,
    require_lifecycle_selectivity,
    require_throughput_destination,
    require_time_range,
    resolve_page_limit,
    validate_destination_group_by,
)

# ── 辅助工厂 ─────────────────────────────────────────────────────────────────


def _tr(start: str = "2026-06-01T00:00:00Z", end: str = "2026-06-02T00:00:00Z") -> AMPTimeRange:
    return AMPTimeRange(start_at=start, end_at=end)


def _filter_with(*conditions: dict) -> AMPFilter:
    conds = [AMPFilterCondition(**c) for c in conditions]
    return AMPFilter(conditions=conds, logic="and")


# ── require_time_range ────────────────────────────────────────────────────────

NOW_MS = 1_800_000_000_000  # 一个足够大的值，不影响保留校验


class TestRequireTimeRange:
    def test_valid_time_range(self) -> None:
        from_ms, to_ms = require_time_range(_tr("2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"))
        assert from_ms < to_ms
        assert isinstance(from_ms, int)
        assert isinstance(to_ms, int)

    def test_none_raises(self) -> None:
        from app.message.exception import InvalidTimeRangeError

        with pytest.raises(InvalidTimeRangeError):
            require_time_range(None)

    def test_inverted_range_raises(self) -> None:
        from app.message.exception import InvalidTimeRangeError

        with pytest.raises(InvalidTimeRangeError):
            require_time_range(_tr("2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z"))

    def test_equal_range_raises(self) -> None:
        from app.message.exception import InvalidTimeRangeError

        with pytest.raises(InvalidTimeRangeError):
            require_time_range(_tr("2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z"))

    def test_returns_milliseconds(self) -> None:
        from_ms, to_ms = require_time_range(_tr("2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"))
        assert to_ms - from_ms == 3_600_000

    def test_utc_timezone_handling(self) -> None:
        from_ms, to_ms = require_time_range(_tr("2026-06-01T00:00:00+00:00", "2026-06-01T01:00:00+00:00"))
        assert to_ms - from_ms == 3_600_000


# ── assert_within_retention ───────────────────────────────────────────────────


class TestAssertWithinRetention:
    def _now_ms(self) -> int:
        return NOW_MS

    def test_within_retention_no_error(self) -> None:
        recent_from = NOW_MS - 3 * 86_400_000  # 3 天前
        assert_within_retention(recent_from, retention_days=7, now_ms=NOW_MS)

    def test_exactly_at_boundary_no_error(self) -> None:
        boundary = NOW_MS - 7 * 86_400_000
        assert_within_retention(boundary, retention_days=7, now_ms=NOW_MS)

    def test_out_of_retention_raises(self) -> None:
        too_old = NOW_MS - 8 * 86_400_000
        with pytest.raises(OutOfRetentionError):
            assert_within_retention(too_old, retention_days=7, now_ms=NOW_MS)

    def test_different_retention_days(self) -> None:
        from_ms = NOW_MS - 30 * 86_400_000  # 30天前
        with pytest.raises(OutOfRetentionError):
            assert_within_retention(from_ms, retention_days=7, now_ms=NOW_MS)

    def test_one_day_retention_recent(self) -> None:
        from_ms = NOW_MS - 12 * 3600 * 1000
        assert_within_retention(from_ms, retention_days=1, now_ms=NOW_MS)

    def test_one_day_retention_old(self) -> None:
        from_ms = NOW_MS - 2 * 86_400_000
        with pytest.raises(OutOfRetentionError):
            assert_within_retention(from_ms, retention_days=1, now_ms=NOW_MS)


# ── require_lifecycle_selectivity ─────────────────────────────────────────────


class TestRequireLifecycleSelectivity:
    def test_no_filter_no_time_range_raises(self) -> None:
        with pytest.raises(LifecycleKeyRequiredError):
            require_lifecycle_selectivity(filter_=None, time_range=None)

    def test_empty_filter_raises(self) -> None:
        f = AMPFilter(conditions=[], logic="and")
        with pytest.raises(LifecycleKeyRequiredError):
            require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_message_id_satisfies(self) -> None:
        f = _filter_with({"field": "messageId", "op": "eq", "value": "abc"})
        require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_lifecycle_key_satisfies(self) -> None:
        f = _filter_with({"field": "lifecycleKey", "op": "eq", "value": "mid:abc"})
        require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_correlation_id_satisfies(self) -> None:
        f = _filter_with({"field": "correlationId", "op": "eq", "value": "corr-123"})
        require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_trace_id_satisfies(self) -> None:
        f = _filter_with({"field": "traceId", "op": "eq", "value": "trace-123"})
        require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_system_plus_dest_plus_time_range_satisfies(self) -> None:
        f = _filter_with(
            {"field": "system", "op": "eq", "value": "kafka"},
            {"field": "destination.name", "op": "eq", "value": "my-topic"},
        )
        require_lifecycle_selectivity(filter_=f, time_range=_tr())

    def test_system_only_raises(self) -> None:
        f = _filter_with({"field": "system", "op": "eq", "value": "kafka"})
        with pytest.raises(LifecycleKeyRequiredError):
            require_lifecycle_selectivity(filter_=f, time_range=None)

    def test_system_dest_no_time_raises(self) -> None:
        f = _filter_with(
            {"field": "system", "op": "eq", "value": "kafka"},
            {"field": "destination.name", "op": "eq", "value": "my-topic"},
        )
        with pytest.raises(LifecycleKeyRequiredError):
            require_lifecycle_selectivity(filter_=f, time_range=None)


# ── validate_destination_group_by ─────────────────────────────────────────────


class TestValidateDestinationGroupBy:
    def test_none_returns_empty(self) -> None:
        result = validate_destination_group_by(None)
        assert result == []

    def test_valid_group_by(self) -> None:
        result = validate_destination_group_by(["system", "destination.name"])
        assert result == ["system", "destination.name"]

    def test_invalid_field_raises(self) -> None:
        with pytest.raises(MessageGroupByInvalidError):
            validate_destination_group_by(["system", "bogus_field"])

    def test_all_valid_fields(self) -> None:
        result = validate_destination_group_by(
            ["system", "destination.name", "destination.kind", "destination.virtualHost"]
        )
        assert len(result) == 4


# ── require_throughput_destination ───────────────────────────────────────────


class TestRequireThroughputDestination:
    def test_both_present(self) -> None:
        sys, dest = require_throughput_destination(system="kafka", destination_name="my-topic")
        assert sys == "kafka"
        assert dest == "my-topic"

    def test_missing_system_raises(self) -> None:
        with pytest.raises(MessageDestinationRequiredError):
            require_throughput_destination(system=None, destination_name="my-topic")

    def test_missing_dest_name_raises(self) -> None:
        with pytest.raises(MessageDestinationRequiredError):
            require_throughput_destination(system="kafka", destination_name=None)

    def test_both_missing_raises(self) -> None:
        with pytest.raises(MessageDestinationRequiredError):
            require_throughput_destination(system=None, destination_name=None)


# ── parse_throughput_step ─────────────────────────────────────────────────────


class TestParseThroughputStep:
    def test_none_short_range_defaults_5m(self) -> None:
        # < 6h → 5m
        from_ms = 0
        to_ms = 2 * 3600 * 1000  # 2h
        result = parse_throughput_step(None, from_ms=from_ms, to_ms=to_ms)
        assert result == 300

    def test_none_medium_range_defaults_15m(self) -> None:
        # > 6h, <= 1d → 15m
        from_ms = 0
        to_ms = 12 * 3600 * 1000  # 12h
        result = parse_throughput_step(None, from_ms=from_ms, to_ms=to_ms)
        assert result == 900

    def test_none_long_range_defaults_1h(self) -> None:
        # > 1d → 1h
        from_ms = 0
        to_ms = 2 * 86_400_000  # 2d
        result = parse_throughput_step(None, from_ms=from_ms, to_ms=to_ms)
        assert result == 3600

    def test_valid_iso_duration_pt5m(self) -> None:
        result = parse_throughput_step("PT5M", from_ms=0, to_ms=3600_000)
        assert result == 300

    def test_valid_iso_duration_pt15m(self) -> None:
        result = parse_throughput_step("PT15M", from_ms=0, to_ms=3600_000)
        assert result == 900

    def test_valid_iso_duration_pt1h(self) -> None:
        result = parse_throughput_step("PT1H", from_ms=0, to_ms=3600_000)
        assert result == 3600

    def test_step_less_than_5m_rounds_up(self) -> None:
        # 2min < 5min → 5min
        result = parse_throughput_step("PT2M", from_ms=0, to_ms=3600_000)
        assert result == 300

    def test_non_300_multiple_rounds_up(self) -> None:
        # 7min → next 300s multiple = 10min = 600s
        result = parse_throughput_step("PT7M", from_ms=0, to_ms=3600_000)
        assert result == 600

    def test_invalid_duration_raises(self) -> None:
        with pytest.raises(MessageStepInvalidError):
            parse_throughput_step("PT3600X", from_ms=0, to_ms=3600_000)


# ── compute_rebuild_from ──────────────────────────────────────────────────────


class TestComputeRebuildFrom:
    def test_with_watermark(self) -> None:
        wm = 1_000_000_000
        result = compute_rebuild_from(last_watermark_ms=wm, overlap_seconds=300)
        assert result == wm - 300_000

    def test_none_watermark_returns_zero(self) -> None:
        result = compute_rebuild_from(last_watermark_ms=None, overlap_seconds=300)
        assert result == 0

    def test_overlap_zero(self) -> None:
        wm = 5_000_000
        result = compute_rebuild_from(last_watermark_ms=wm, overlap_seconds=0)
        assert result == wm

    def test_large_overlap(self) -> None:
        wm = 3_600_000
        result = compute_rebuild_from(last_watermark_ms=wm, overlap_seconds=3600)
        assert result == 0

    def test_negative_clamp_to_zero(self) -> None:
        result = compute_rebuild_from(last_watermark_ms=1000, overlap_seconds=3600)
        assert result == 0


# ── resolve_page_limit ────────────────────────────────────────────────────────


class TestResolvePageLimit:
    def test_none_returns_default(self) -> None:
        result = resolve_page_limit(None)
        assert result == 50

    def test_explicit_limit(self) -> None:
        page = AMPPaginationRequest(limit=100)
        result = resolve_page_limit(page)
        assert result == 100

    def test_limit_1(self) -> None:
        page = AMPPaginationRequest(limit=1)
        result = resolve_page_limit(page)
        assert result == 1


# ── clamp_deadletter_n ────────────────────────────────────────────────────────


class TestClampDeadletterN:
    def test_within_max(self) -> None:
        assert clamp_deadletter_n(50, hard_max=200) == 50

    def test_exceeds_max_clamped(self) -> None:
        assert clamp_deadletter_n(500, hard_max=200) == 200

    def test_exactly_max(self) -> None:
        assert clamp_deadletter_n(200, hard_max=200) == 200


# ── align_to_bucket ───────────────────────────────────────────────────────────


class TestAlignToBucket:
    def test_already_aligned(self) -> None:
        ts = 300_000  # 5min in ms
        result = align_to_bucket(ts, bucket_seconds=300)
        assert result == 300_000

    def test_rounds_down(self) -> None:
        ts = 450_000  # 7.5 min
        result = align_to_bucket(ts, bucket_seconds=300)
        assert result == 300_000

    def test_zero(self) -> None:
        assert align_to_bucket(0, bucket_seconds=300) == 0

    def test_just_before_boundary(self) -> None:
        ts = 299_999
        result = align_to_bucket(ts, bucket_seconds=300)
        assert result == 0
