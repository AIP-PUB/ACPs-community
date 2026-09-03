"""单元测试：Message 模块配置属性（A-1）。

按设计 §7.1 和 §7.2，验证每个 message_* property 的值域校验。
跨键约束（dedup_window >= kafka_retention 等）留 G-2 test_runtime。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def _settings_with(**kwargs: object) -> Settings:
    """用给定 message TOML 段值构造 Settings。"""
    # Settings 在 model_post_init 里载入 TOML；这里直接 mock _toml
    s = Settings(DATABASE_URL="postgresql+asyncpg://u:p@localhost/test")
    object.__setattr__(s, "_toml", {"message": kwargs})
    return s


# ── §7.1 (A) 设计明确的 17 个 property ────────────────────────────────────────


class TestMessageBatchConfig:
    def test_insert_batch_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_insert_batch_interval_seconds == 5

    def test_insert_batch_interval_seconds_valid(self) -> None:
        s = _settings_with(insert_batch_interval_seconds=10)
        assert s.message_insert_batch_interval_seconds == 10

    def test_insert_batch_interval_seconds_invalid(self) -> None:
        s = _settings_with(insert_batch_interval_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_insert_batch_interval_seconds

    def test_insert_batch_max_rows_default(self) -> None:
        s = _settings_with()
        assert s.message_insert_batch_max_rows == 5000

    def test_insert_batch_max_rows_invalid(self) -> None:
        s = _settings_with(insert_batch_max_rows=-1)
        with pytest.raises(ValueError):
            _ = s.message_insert_batch_max_rows


class TestMessageRetentionConfig:
    def test_kafka_retention_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_kafka_retention_seconds == 21600

    def test_kafka_retention_seconds_invalid(self) -> None:
        s = _settings_with(kafka_retention_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_kafka_retention_seconds

    def test_dedup_window_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_dedup_window_seconds == 21600

    def test_dedup_window_seconds_invalid(self) -> None:
        s = _settings_with(dedup_window_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_dedup_window_seconds

    def test_raw_retention_days_default(self) -> None:
        s = _settings_with()
        assert s.message_raw_retention_days == 7

    def test_raw_retention_days_invalid(self) -> None:
        s = _settings_with(raw_retention_days=0)
        with pytest.raises(ValueError):
            _ = s.message_raw_retention_days

    def test_lifecycle_retention_days_default(self) -> None:
        s = _settings_with()
        assert s.message_lifecycle_retention_days == 30

    def test_lifecycle_retention_days_invalid(self) -> None:
        s = _settings_with(lifecycle_retention_days=0)
        with pytest.raises(ValueError):
            _ = s.message_lifecycle_retention_days

    def test_destination_state_retention_days_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_state_retention_days == 30

    def test_destination_stats_retention_days_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_stats_retention_days == 30

    def test_raw_archive_retention_days_default(self) -> None:
        s = _settings_with()
        assert s.message_raw_archive_retention_days == 30


class TestMessageCompactionConfig:
    def test_lifecycle_compact_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_lifecycle_compact_interval_seconds == 60

    def test_lifecycle_compact_interval_seconds_invalid(self) -> None:
        s = _settings_with(lifecycle_compact_interval_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_lifecycle_compact_interval_seconds

    def test_destination_stats_compact_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_stats_compact_interval_seconds == 60

    def test_destination_stats_compact_interval_seconds_invalid(self) -> None:
        s = _settings_with(destination_stats_compact_interval_seconds=-1)
        with pytest.raises(ValueError):
            _ = s.message_destination_stats_compact_interval_seconds

    def test_compaction_overlap_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_compaction_overlap_seconds == 300

    def test_compaction_overlap_seconds_zero_is_valid(self) -> None:
        s = _settings_with(compaction_overlap_seconds=0)
        assert s.message_compaction_overlap_seconds == 0

    def test_compaction_overlap_seconds_negative_is_invalid(self) -> None:
        s = _settings_with(compaction_overlap_seconds=-1)
        with pytest.raises(ValueError):
            _ = s.message_compaction_overlap_seconds


class TestMessageQueryConfig:
    def test_state_collect_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_state_collect_interval_seconds == 60

    def test_state_collect_interval_seconds_invalid(self) -> None:
        s = _settings_with(state_collect_interval_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_state_collect_interval_seconds

    def test_lagging_threshold_ms_default(self) -> None:
        s = _settings_with()
        assert s.message_lagging_threshold_ms == 300000

    def test_lagging_threshold_ms_invalid(self) -> None:
        s = _settings_with(lagging_threshold_ms=0)
        with pytest.raises(ValueError):
            _ = s.message_lagging_threshold_ms

    def test_query_timeout_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_query_timeout_seconds == 30

    def test_query_timeout_seconds_invalid(self) -> None:
        s = _settings_with(query_timeout_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_query_timeout_seconds

    def test_destination_query_max_groups_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_query_max_groups == 200

    def test_destination_query_max_groups_invalid(self) -> None:
        s = _settings_with(destination_query_max_groups=0)
        with pytest.raises(ValueError):
            _ = s.message_destination_query_max_groups

    def test_deadletter_query_max_n_default(self) -> None:
        s = _settings_with()
        assert s.message_deadletter_query_max_n == 200

    def test_deadletter_query_max_n_invalid(self) -> None:
        s = _settings_with(deadletter_query_max_n=0)
        with pytest.raises(ValueError):
            _ = s.message_deadletter_query_max_n


# ── §7.2 (B) 运营/profile 键（15 个）────────────────────────────────────────


class TestMessageOperationalConfig:
    def test_topic_default(self) -> None:
        s = _settings_with()
        assert s.message_topic == "amp.message"

    def test_dlq_topic_default(self) -> None:
        s = _settings_with()
        assert s.message_dlq_topic == "amp.message.dlq"

    def test_consumer_group_default(self) -> None:
        s = _settings_with()
        assert s.message_consumer_group == "monitor-server.message.writer.v1"

    def test_writer_poll_timeout_ms_default(self) -> None:
        s = _settings_with()
        assert s.message_writer_poll_timeout_ms == 1000

    def test_raw_log_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_raw_log_enabled is False

    def test_raw_log_enabled_true(self) -> None:
        s = _settings_with(raw_log_enabled=True)
        assert s.message_raw_log_enabled is True

    def test_lagging_response_mode_default(self) -> None:
        s = _settings_with()
        assert s.message_lagging_response_mode == "partial"

    def test_lagging_response_mode_503(self) -> None:
        s = _settings_with(lagging_response_mode="503")
        assert s.message_lagging_response_mode == "503"

    def test_lagging_response_mode_invalid(self) -> None:
        s = _settings_with(lagging_response_mode="error")
        with pytest.raises(ValueError):
            _ = s.message_lagging_response_mode

    def test_correlation_id_stable_unique_default(self) -> None:
        s = _settings_with()
        assert s.message_correlation_id_stable_unique is False

    def test_writer_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_writer_enabled is True

    def test_reliability_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_reliability_enabled is True

    def test_destination_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_enabled is True

    def test_state_collector_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_state_collector_enabled is False

    def test_destination_source_kind_default(self) -> None:
        s = _settings_with()
        assert s.message_destination_source_kind == "null"

    def test_archive_enabled_default(self) -> None:
        s = _settings_with()
        assert s.message_archive_enabled is False

    def test_archive_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_archive_interval_seconds == 3600

    def test_archive_interval_seconds_invalid(self) -> None:
        s = _settings_with(archive_interval_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_archive_interval_seconds

    def test_metrics_log_interval_seconds_default(self) -> None:
        s = _settings_with()
        assert s.message_metrics_log_interval_seconds == 60

    def test_metrics_log_interval_seconds_invalid(self) -> None:
        s = _settings_with(metrics_log_interval_seconds=0)
        with pytest.raises(ValueError):
            _ = s.message_metrics_log_interval_seconds
