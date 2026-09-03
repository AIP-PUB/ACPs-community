"""tests: seek_timestamp_ms / next_lookback_seconds。"""
import pytest

from acps_sdk.amp.alive_sync.bootstrap import next_lookback_seconds, seek_timestamp_ms


class TestSeekTimestampMs:
    def test_basic_subtraction(self) -> None:
        # 2026-06-13T01:20:00Z epoch ms = 1750000000000 ± 允差
        # 只验证 lookback 正确减去
        ts1 = seek_timestamp_ms("2026-06-13T01:20:00Z", lookback_seconds=0)
        ts2 = seek_timestamp_ms("2026-06-13T01:20:00Z", lookback_seconds=300)
        assert ts1 - ts2 == 300_000

    def test_result_non_negative(self) -> None:
        # 非常大的 lookback 也不得为负
        ts = seek_timestamp_ms("2026-06-13T01:20:00Z", lookback_seconds=999_999_999)
        assert ts >= 0

    def test_z_suffix_parsed(self) -> None:
        # 带 Z 后缀不应抛异常
        ts = seek_timestamp_ms("2026-06-13T01:20:00Z", lookback_seconds=60)
        assert ts > 0

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="格式无效"):
            seek_timestamp_ms("not-a-date", lookback_seconds=60)


class TestNextLookbackSeconds:
    def test_doubles_by_default(self) -> None:
        assert next_lookback_seconds(300, max_seconds=86400) == 600

    def test_clamps_at_max(self) -> None:
        assert next_lookback_seconds(50000, max_seconds=86400) == 86400

    def test_already_at_max_stays(self) -> None:
        assert next_lookback_seconds(86400, max_seconds=86400) == 86400

    def test_custom_factor(self) -> None:
        assert next_lookback_seconds(100, factor=3, max_seconds=10000) == 300
