"""tests/unit/system/test_planner.py — planner.py 护栏函数单元测试。"""

from __future__ import annotations

import pytest

from app.core.amp_api_schema import AMPPaginationRequest, AMPSortSpec, AMPTimeRange
from app.system.exception import (
    InvalidTimeRangeError,
    OutOfRetentionError,
    SystemKeywordTooBroadError,
    UnsupportedFieldError,
)
from app.system.planner import (
    ResolvedSort,
    assert_within_retention,
    inject_scope_filter,
    require_time_range,
    resolve_page_limit,
    validate_keyword,
    validate_sort,
)


class TestRequireTimeRange:
    def test_none_raises(self) -> None:
        with pytest.raises(InvalidTimeRangeError):
            require_time_range(None)

    def test_valid_range_parses(self) -> None:
        tr = AMPTimeRange(start_at="2024-01-01T00:00:00Z", end_at="2024-01-02T00:00:00Z")
        from_ms, to_ms = require_time_range(tr)
        assert from_ms < to_ms

    def test_start_equals_end_raises(self) -> None:
        tr = AMPTimeRange(start_at="2024-01-01T00:00:00Z", end_at="2024-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            require_time_range(tr)

    def test_start_after_end_raises(self) -> None:
        tr = AMPTimeRange(start_at="2024-01-02T00:00:00Z", end_at="2024-01-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError):
            require_time_range(tr)

    def test_ms_values_correct(self) -> None:
        tr = AMPTimeRange(start_at="2024-01-01T00:00:00Z", end_at="2024-01-01T01:00:00Z")
        from_ms, to_ms = require_time_range(tr)
        assert to_ms - from_ms == 3600 * 1000


class TestAssertWithinRetention:
    def _now_ms(self) -> int:
        import time

        return int(time.time() * 1000)

    def test_recent_from_ms_passes(self) -> None:
        now = self._now_ms()
        from_ms = now - 3 * 86400 * 1000  # 3 days ago
        assert_within_retention(from_ms, archive_retention_days=90, now_ms=now)

    def test_exactly_at_cutoff_passes(self) -> None:
        now = self._now_ms()
        from_ms = now - 90 * 86400 * 1000
        assert_within_retention(from_ms, archive_retention_days=90, now_ms=now)

    def test_before_cutoff_raises_out_of_retention(self) -> None:
        now = self._now_ms()
        from_ms = now - 91 * 86400 * 1000  # 91 days ago > 90-day retention
        with pytest.raises(OutOfRetentionError):
            assert_within_retention(from_ms, archive_retention_days=90, now_ms=now)


class TestValidateKeyword:
    def test_none_keyword_passes(self) -> None:
        validate_keyword(
            None,
            min_length=2,
            has_other_filter=False,
            window_seconds=3600,
            keyword_only_max_window_seconds=1800,
        )

    def test_keyword_meets_min_length_passes(self) -> None:
        validate_keyword(
            "er",
            min_length=2,
            has_other_filter=True,
            window_seconds=3600,
            keyword_only_max_window_seconds=3600,
        )

    def test_keyword_too_short_raises(self) -> None:
        with pytest.raises(SystemKeywordTooBroadError):
            validate_keyword(
                "e",
                min_length=2,
                has_other_filter=False,
                window_seconds=100,
                keyword_only_max_window_seconds=3600,
            )

    def test_keyword_only_too_wide_window_raises(self) -> None:
        with pytest.raises(SystemKeywordTooBroadError):
            validate_keyword(
                "error",
                min_length=2,
                has_other_filter=False,
                window_seconds=7200,
                keyword_only_max_window_seconds=3600,
            )

    def test_keyword_with_other_filter_wide_window_passes(self) -> None:
        """有其他过滤条件时，宽时间窗口不触发护栏。"""
        validate_keyword(
            "error",
            min_length=2,
            has_other_filter=True,
            window_seconds=7200,
            keyword_only_max_window_seconds=3600,
        )

    def test_whitespace_only_keyword_too_short(self) -> None:
        with pytest.raises(SystemKeywordTooBroadError):
            validate_keyword(
                "  ",
                min_length=2,
                has_other_filter=False,
                window_seconds=100,
                keyword_only_max_window_seconds=3600,
            )


class TestValidateSort:
    def test_none_returns_default_desc(self) -> None:
        result = validate_sort(None)
        assert len(result) == 1
        assert result[0].field == "timestamp"
        assert result[0].order == "desc"

    def test_empty_list_returns_default(self) -> None:
        result = validate_sort([])
        assert result[0].field == "timestamp"

    def test_valid_timestamp_sort(self) -> None:
        spec = AMPSortSpec(field="timestamp", order="asc")
        result = validate_sort([spec])
        assert len(result) == 1
        assert result[0].doc_field == "timestamp"
        assert result[0].order == "asc"

    def test_severity_number_whitelisted(self) -> None:
        spec = AMPSortSpec(field="severityNumber", order="desc")
        result = validate_sort([spec])
        assert result[0].doc_field == "severity_number"

    def test_unsupported_field_raises(self) -> None:
        spec = AMPSortSpec(field="message", order="asc")
        with pytest.raises(UnsupportedFieldError):
            validate_sort([spec])

    def test_result_is_resolved_sort(self) -> None:
        spec = AMPSortSpec(field="timestamp", order="desc")
        result = validate_sort([spec])
        assert isinstance(result[0], ResolvedSort)


class TestResolvePageLimit:
    def test_none_returns_50(self) -> None:
        assert resolve_page_limit(None) == 50

    def test_custom_limit(self) -> None:
        page = AMPPaginationRequest(limit=100)
        assert resolve_page_limit(page) == 100


class TestInjectScopeFilter:
    def test_no_auth_returns_empty(self) -> None:
        """当前无鉴权 → 返回空列表（不拒绝请求）。"""
        result = inject_scope_filter()
        assert result == []
