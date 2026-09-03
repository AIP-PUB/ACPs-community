"""tests/unit/test_heartbeat_runtime_config.py — validate_heartbeat_config 单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.heartbeat.exception import HeartbeatConfigError
from app.heartbeat.runtime import validate_heartbeat_config


def _make_valid_settings(**overrides):  # type: ignore[no-untyped-def]
    """返回一个携带合法默认值的 mock settings 对象。"""
    s = MagicMock()
    defaults = {
        "heartbeat_refresh_emit_interval_seconds": 30,
        "heartbeat_silence_threshold_seconds": 90,
        "heartbeat_evict_after_seconds": 3600,
        "heartbeat_heartbeat_shard_count": 1,
        "heartbeat_silent_scan_interval_seconds": 10,
        "heartbeat_evict_scan_interval_seconds": 60,
        "heartbeat_scan_batch_size": 200,
        "heartbeat_scan_lock_ttl_seconds": 30,
        "heartbeat_in_list_max": 50,
        "heartbeat_silence_top_default_n": 10,
        "heartbeat_silence_top_max_n": 50,
        "heartbeat_silence_top_shard_fetch_size": 100,
        "heartbeat_input_partition_count": 1,
        "heartbeat_outbox_max_len": 10000,
        "heartbeat_relay_published_seq_batch_size": 200,
        "heartbeat_snapshot_chunk_size": 500,
        "heartbeat_snapshot_max_enumeration_seconds": 300,
        "heartbeat_metrics_log_interval_seconds": 60,
        "heartbeat_relay_max_publish_lag_seconds": 0,
        "heartbeat_snapshot_max_alive_rows_per_s": 0,
        "heartbeat_summary_buckets_seconds": [30, 60, 120, 300, 600, 3600],
        "heartbeat_writer_watermark_flush_interval_ms": 5000,
        "heartbeat_writer_watermark_stale_after_ms": 30000,
    }
    for k, v in {**defaults, **overrides}.items():
        setattr(s, k, v)
    return s


class TestValidateHeartbeatConfigValid:
    def test_valid_defaults_no_error(self) -> None:
        s = _make_valid_settings()
        with patch("app.heartbeat.runtime.settings", s):
            validate_heartbeat_config()  # should not raise

    def test_valid_zero_lag_allowed(self) -> None:
        s = _make_valid_settings(heartbeat_relay_max_publish_lag_seconds=0)
        with patch("app.heartbeat.runtime.settings", s):
            validate_heartbeat_config()


class TestValidateHeartbeatConfigTimeOrder:
    def test_refresh_greater_than_silence_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_refresh_emit_interval_seconds=100,
            heartbeat_silence_threshold_seconds=90,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match=r"refresh_emit_interval_seconds.*silence_threshold_seconds"),
        ):
            validate_heartbeat_config()

    def test_refresh_equal_to_silence_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_refresh_emit_interval_seconds=90,
            heartbeat_silence_threshold_seconds=90,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="refresh_emit_interval_seconds"),
        ):
            validate_heartbeat_config()

    def test_silence_greater_than_evict_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_silence_threshold_seconds=7200,
            heartbeat_evict_after_seconds=3600,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match=r"silence_threshold_seconds.*evict_after_seconds"),
        ):
            validate_heartbeat_config()


class TestValidateHeartbeatConfigPositiveConstraints:
    @pytest.mark.parametrize(
        "field",
        [
            "heartbeat_heartbeat_shard_count",
            "heartbeat_silence_threshold_seconds",
            "heartbeat_evict_after_seconds",
            "heartbeat_refresh_emit_interval_seconds",
            "heartbeat_silent_scan_interval_seconds",
            "heartbeat_evict_scan_interval_seconds",
            "heartbeat_scan_batch_size",
            "heartbeat_scan_lock_ttl_seconds",
            "heartbeat_in_list_max",
            "heartbeat_silence_top_default_n",
            "heartbeat_silence_top_max_n",
            "heartbeat_silence_top_shard_fetch_size",
            "heartbeat_input_partition_count",
            "heartbeat_outbox_max_len",
            "heartbeat_relay_published_seq_batch_size",
            "heartbeat_snapshot_chunk_size",
            "heartbeat_snapshot_max_enumeration_seconds",
            "heartbeat_metrics_log_interval_seconds",
        ],
    )
    def test_zero_value_raises(self, field: str) -> None:
        s = _make_valid_settings(**{field: 0})
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="must be > 0"),
        ):
            validate_heartbeat_config()

    @pytest.mark.parametrize(
        "field",
        [
            "heartbeat_relay_max_publish_lag_seconds",
            "heartbeat_snapshot_max_alive_rows_per_s",
        ],
    )
    def test_negative_value_raises(self, field: str) -> None:
        s = _make_valid_settings(**{field: -1})
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="must be >= 0"),
        ):
            validate_heartbeat_config()


class TestValidateHeartbeatConfigSummaryBuckets:
    def test_empty_buckets_raises(self) -> None:
        s = _make_valid_settings(heartbeat_summary_buckets_seconds=[])
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="must not be empty"),
        ):
            validate_heartbeat_config()

    def test_non_strictly_increasing_raises(self) -> None:
        s = _make_valid_settings(heartbeat_summary_buckets_seconds=[30, 30, 60])
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="strictly increasing"),
        ):
            validate_heartbeat_config()

    def test_decreasing_raises(self) -> None:
        s = _make_valid_settings(heartbeat_summary_buckets_seconds=[60, 30])
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="strictly increasing"),
        ):
            validate_heartbeat_config()

    def test_single_bucket_valid(self) -> None:
        s = _make_valid_settings(heartbeat_summary_buckets_seconds=[30])
        with patch("app.heartbeat.runtime.settings", s):
            validate_heartbeat_config()  # should not raise


class TestValidateHeartbeatConfigSilenceTopSanity:
    def test_fetch_size_less_than_max_n_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_silence_top_shard_fetch_size=20,
            heartbeat_silence_top_max_n=50,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match=r"silence_top_shard_fetch_size.*>=.*silence_top_max_n"),
        ):
            validate_heartbeat_config()

    def test_fetch_size_equal_to_max_n_valid(self) -> None:
        s = _make_valid_settings(
            heartbeat_silence_top_shard_fetch_size=50,
            heartbeat_silence_top_max_n=50,
        )
        with patch("app.heartbeat.runtime.settings", s):
            validate_heartbeat_config()  # should not raise


class TestValidateHeartbeatConfigWatermark:
    def test_stale_le_flush_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_writer_watermark_flush_interval_ms=10000,
            heartbeat_writer_watermark_stale_after_ms=5000,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(
                HeartbeatConfigError, match=r"writer_watermark_stale_after_ms.*>.*writer_watermark_flush_interval_ms"
            ),
        ):
            validate_heartbeat_config()

    def test_stale_equal_to_flush_raises(self) -> None:
        s = _make_valid_settings(
            heartbeat_writer_watermark_flush_interval_ms=5000,
            heartbeat_writer_watermark_stale_after_ms=5000,
        )
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="stale_after_ms"),
        ):
            validate_heartbeat_config()

    def test_zero_flush_raises(self) -> None:
        s = _make_valid_settings(heartbeat_writer_watermark_flush_interval_ms=0)
        with (
            patch("app.heartbeat.runtime.settings", s),
            pytest.raises(HeartbeatConfigError, match="writer_watermark_flush_interval_ms must be > 0"),
        ):
            validate_heartbeat_config()

    def test_multiple_errors_collected(self) -> None:
        """validate_heartbeat_config 遇到多个错误应收集全部后统一抛出。"""
        s = _make_valid_settings(
            heartbeat_refresh_emit_interval_seconds=100,
            heartbeat_silence_threshold_seconds=90,
            heartbeat_summary_buckets_seconds=[],
        )
        with patch("app.heartbeat.runtime.settings", s), pytest.raises(HeartbeatConfigError) as exc_info:
            validate_heartbeat_config()
        msg = str(exc_info.value)
        assert "refresh_emit_interval_seconds" in msg
        assert "must not be empty" in msg
