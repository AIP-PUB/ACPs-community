"""tests/unit/test_access_config_validation.py — Access 模块配置属性单键值域测试。

TDD A-2（单键部分）：先写测试（红）→ 实现 config.py 新增 property（绿）。
跨键约束（archive_retention >= raw_retention 等）留到步骤 C-2 的 runtime.validate_access_config。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from app.core.config import Settings


def _make_settings(access_overrides: dict) -> Settings:
    """构造携带自定义 [access] 节的 Settings 实例（绕过真实 TOML 文件加载）。"""
    from app.core.config import Settings

    s = Settings.__new__(Settings)
    object.__setattr__(s, "_toml", {"access": access_overrides})
    return s


class TestAccessConfigDefaults:
    """各 access_* property 默认值与类型检查。"""

    def test_topic_default(self) -> None:
        from app.core.config import settings

        assert settings.access_topic == "amp.access"

    def test_dlq_topic_default(self) -> None:
        from app.core.config import settings

        assert settings.access_dlq_topic == "amp.access.dlq"

    def test_consumer_group_default(self) -> None:
        from app.core.config import settings

        assert settings.access_consumer_group == "monitor-server.access.writer.v1"

    def test_writer_poll_timeout_ms_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_writer_poll_timeout_ms > 0

    def test_insert_batch_interval_seconds_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_insert_batch_interval_seconds > 0

    def test_insert_batch_max_rows_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_insert_batch_max_rows > 0

    def test_dedup_window_hours_default(self) -> None:
        from app.core.config import settings

        assert settings.access_dedup_window_hours >= 1

    def test_raw_retention_days_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_raw_retention_days > 0

    def test_archive_retention_days_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_archive_retention_days > 0

    def test_topology_retention_days_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_topology_retention_days > 0

    def test_lagging_threshold_ms_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_lagging_threshold_ms > 0

    def test_lagging_response_mode_valid(self) -> None:
        from app.core.config import settings

        assert settings.access_lagging_response_mode in {"503", "partial"}

    def test_query_timeout_seconds_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_query_timeout_seconds > 0

    def test_trace_max_spans_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_trace_max_spans > 0

    def test_trace_max_duration_hours_default(self) -> None:
        from app.core.config import settings

        assert settings.access_trace_max_duration_hours >= 1

    def test_slow_top_max_n_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_slow_top_max_n > 0

    def test_error_attribution_max_n_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_error_attribution_max_n > 0

    def test_error_status_threshold_in_range(self) -> None:
        from app.core.config import settings

        assert 400 <= settings.access_error_status_threshold <= 599

    def test_redacted_header_allowlist_default(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.access_redacted_header_allowlist, str)
        assert len(settings.access_redacted_header_allowlist) > 0

    def test_raw_log_enabled_default_false(self) -> None:
        from app.core.config import settings

        assert settings.access_raw_log_enabled is False

    def test_trace_seen_hint_enabled_default_false(self) -> None:
        from app.core.config import settings

        assert settings.access_trace_seen_hint_enabled is False

    def test_archive_enabled_default_false(self) -> None:
        from app.core.config import settings

        assert settings.access_archive_enabled is False

    def test_archive_interval_seconds_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_archive_interval_seconds > 0

    def test_analytics_enabled_default_true(self) -> None:
        from app.core.config import settings

        assert settings.access_analytics_enabled is True

    def test_apm_enabled_default_true(self) -> None:
        from app.core.config import settings

        assert settings.access_apm_enabled is True

    def test_metrics_log_interval_seconds_positive(self) -> None:
        from app.core.config import settings

        assert settings.access_metrics_log_interval_seconds > 0


class TestAccessConfigSingleKeyValidation:
    """单键值域校验：非法值应抛出 ValueError。"""

    def test_error_status_threshold_below_400_raises(self) -> None:
        s = _make_settings({"error_status_threshold": 399})
        with pytest.raises(ValueError, match="error_status_threshold"):
            _ = s.access_error_status_threshold

    def test_error_status_threshold_above_599_raises(self) -> None:
        s = _make_settings({"error_status_threshold": 600})
        with pytest.raises(ValueError, match="error_status_threshold"):
            _ = s.access_error_status_threshold

    def test_error_status_threshold_400_valid(self) -> None:
        s = _make_settings({"error_status_threshold": 400})
        assert s.access_error_status_threshold == 400

    def test_error_status_threshold_599_valid(self) -> None:
        s = _make_settings({"error_status_threshold": 599})
        assert s.access_error_status_threshold == 599

    def test_dedup_window_hours_zero_raises(self) -> None:
        s = _make_settings({"dedup_window_hours": 0})
        with pytest.raises(ValueError, match="dedup_window_hours"):
            _ = s.access_dedup_window_hours

    def test_dedup_window_hours_one_valid(self) -> None:
        s = _make_settings({"dedup_window_hours": 1})
        assert s.access_dedup_window_hours == 1

    def test_trace_max_duration_hours_zero_raises(self) -> None:
        s = _make_settings({"trace_max_duration_hours": 0})
        with pytest.raises(ValueError, match="trace_max_duration_hours"):
            _ = s.access_trace_max_duration_hours

    def test_trace_max_duration_hours_one_valid(self) -> None:
        s = _make_settings({"trace_max_duration_hours": 1})
        assert s.access_trace_max_duration_hours == 1

    def test_lagging_response_mode_invalid_raises(self) -> None:
        s = _make_settings({"lagging_response_mode": "invalid"})
        with pytest.raises(ValueError, match="lagging_response_mode"):
            _ = s.access_lagging_response_mode

    def test_lagging_response_mode_503_valid(self) -> None:
        s = _make_settings({"lagging_response_mode": "503"})
        assert s.access_lagging_response_mode == "503"

    def test_lagging_response_mode_partial_valid(self) -> None:
        s = _make_settings({"lagging_response_mode": "partial"})
        assert s.access_lagging_response_mode == "partial"

    def test_query_timeout_zero_raises(self) -> None:
        s = _make_settings({"query_timeout_seconds": 0})
        with pytest.raises(ValueError, match="query_timeout_seconds"):
            _ = s.access_query_timeout_seconds

    def test_raw_retention_days_zero_raises(self) -> None:
        s = _make_settings({"raw_retention_days": 0})
        with pytest.raises(ValueError, match="raw_retention_days"):
            _ = s.access_raw_retention_days

    def test_insert_batch_interval_seconds_zero_raises(self) -> None:
        s = _make_settings({"insert_batch_interval_seconds": 0})
        with pytest.raises(ValueError, match="insert_batch_interval_seconds"):
            _ = s.access_insert_batch_interval_seconds

    def test_insert_batch_max_rows_zero_raises(self) -> None:
        s = _make_settings({"insert_batch_max_rows": 0})
        with pytest.raises(ValueError, match="insert_batch_max_rows"):
            _ = s.access_insert_batch_max_rows


class TestClickhouseConfigDefaults:
    """clickhouse_* 环境变量 property 默认值测试。"""

    def test_clickhouse_host_default(self) -> None:
        from app.core.config import settings

        assert settings.clickhouse_host == "localhost"

    def test_clickhouse_port_default(self) -> None:
        from app.core.config import settings

        assert settings.clickhouse_port == 8123

    def test_clickhouse_user_default(self) -> None:
        from app.core.config import settings

        assert settings.clickhouse_user == "default"

    def test_clickhouse_password_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.clickhouse_password, str)

    def test_clickhouse_database_default(self) -> None:
        from app.core.config import settings

        # 字段默认值为 "amp"，但测试环境 conftest.py 通过 CLICKHOUSE_DATABASE 覆盖为 "amp_test"；
        # 此处只验证字段值为非空字符串，不与具体默认值耦合。
        assert isinstance(settings.clickhouse_database, str)
        assert settings.clickhouse_database
