"""tests/unit/test_system_config.py — System 模块配置属性测试。"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.core.config import Settings


def _make_settings(**overrides: str) -> Settings:
    """创建测试用 Settings 实例，APP_ENV=testing 使用测试 TOML 覆盖。"""
    env = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "APP_ENV": "testing",
        **overrides,
    }
    with patch.dict(os.environ, env, clear=False):
        return Settings()


class TestSystemBehaviorConfig:
    """设计 §7.1 的 9 个 system 行为配置键测试。"""

    def test_bulk_index_batch_interval_seconds_default(self) -> None:
        s = _make_settings()
        assert s.system_bulk_index_batch_interval_seconds >= 1  # testing.toml 覆盖为 1

    def test_bulk_index_batch_interval_zero_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s),
                "_toml",
                new_callable=lambda: property(lambda self: {"system": {"bulk_index_batch_interval_seconds": 0}}),
            ),
            pytest.raises(ValueError, match="must be > 0"),
        ):
            _ = s.system_bulk_index_batch_interval_seconds

    def test_bulk_index_batch_max_docs_default(self) -> None:
        s = _make_settings()
        assert s.system_bulk_index_batch_max_docs == 5000

    def test_event_hot_retention_days_positive(self) -> None:
        s = _make_settings()
        assert s.system_event_hot_retention_days > 0

    def test_event_hot_retention_zero_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s),
                "_toml",
                new_callable=lambda: property(lambda self: {"system": {"event_hot_retention_days": 0}}),
            ),
            pytest.raises(ValueError, match="must be > 0"),
        ):
            _ = s.system_event_hot_retention_days

    def test_event_warm_retention_days_positive(self) -> None:
        s = _make_settings()
        assert s.system_event_warm_retention_days > 0

    def test_archive_retention_days_positive(self) -> None:
        s = _make_settings()
        assert s.system_archive_retention_days > 0

    def test_lagging_threshold_ms_default(self) -> None:
        """testing.toml 覆盖为 5000。"""
        s = _make_settings()
        assert s.system_lagging_threshold_ms == 5000

    def test_lagging_threshold_zero_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s), "_toml", new_callable=lambda: property(lambda self: {"system": {"lagging_threshold_ms": 0}})
            ),
            pytest.raises(ValueError, match="must be > 0"),
        ):
            _ = s.system_lagging_threshold_ms

    def test_query_timeout_seconds_positive(self) -> None:
        s = _make_settings()
        assert s.system_query_timeout_seconds > 0

    def test_keyword_min_length_positive(self) -> None:
        s = _make_settings()
        assert s.system_keyword_min_length > 0

    def test_keyword_min_length_zero_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s), "_toml", new_callable=lambda: property(lambda self: {"system": {"keyword_min_length": 0}})
            ),
            pytest.raises(ValueError, match="must be > 0"),
        ):
            _ = s.system_keyword_min_length

    def test_search_text_max_length_positive(self) -> None:
        s = _make_settings()
        assert s.system_search_text_max_length > 0

    def test_search_text_max_length_zero_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s), "_toml", new_callable=lambda: property(lambda self: {"system": {"search_text_max_length": 0}})
            ),
            pytest.raises(ValueError, match="must be > 0"),
        ):
            _ = s.system_search_text_max_length


class TestSystemOperationalConfig:
    """设计 §7.2 的运营键测试。"""

    def test_topic_default(self) -> None:
        s = _make_settings()
        assert s.system_topic == "amp.system"

    def test_dlq_topic_default(self) -> None:
        s = _make_settings()
        assert s.system_dlq_topic == "amp.system.dlq"

    def test_consumer_group_default(self) -> None:
        s = _make_settings()
        assert s.system_consumer_group == "monitor-server.system.writer.v1"

    def test_lagging_response_mode_503_in_testing(self) -> None:
        """testing.toml 覆盖为 "503"。"""
        s = _make_settings()
        assert s.system_lagging_response_mode == "503"

    def test_lagging_response_mode_invalid_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s),
                "_toml",
                new_callable=lambda: property(lambda self: {"system": {"lagging_response_mode": "bad"}}),
            ),
            pytest.raises(ValueError, match=r"503.*partial"),
        ):
            _ = s.system_lagging_response_mode

    def test_freshness_reorder_margin_zero_in_testing(self) -> None:
        """testing.toml 覆盖为 0（便于断言水位精确值）。"""
        s = _make_settings()
        assert s.system_freshness_reorder_margin_ms == 0

    def test_freshness_reorder_margin_negative_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s),
                "_toml",
                new_callable=lambda: property(lambda self: {"system": {"freshness_reorder_margin_ms": -1}}),
            ),
            pytest.raises(ValueError, match=">= 0"),
        ):
            _ = s.system_freshness_reorder_margin_ms

    def test_keyword_only_max_window_seconds_positive(self) -> None:
        s = _make_settings()
        assert s.system_keyword_only_max_window_seconds > 0

    def test_pit_keep_alive_default(self) -> None:
        s = _make_settings()
        assert s.system_pit_keep_alive == "5m"

    def test_pit_keep_alive_empty_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s), "_toml", new_callable=lambda: property(lambda self: {"system": {"pit_keep_alive": "  "}})
            ),
            pytest.raises(ValueError, match="must not be empty"),
        ):
            _ = s.system_pit_keep_alive

    def test_index_number_of_shards_positive(self) -> None:
        s = _make_settings()
        assert s.system_index_number_of_shards > 0

    def test_index_number_of_replicas_zero_in_testing(self) -> None:
        """testing.toml 覆盖为 0（单节点 OpenSearch 避免 yellow）。"""
        s = _make_settings()
        assert s.system_index_number_of_replicas == 0

    def test_index_number_of_replicas_negative_raises(self) -> None:
        s = _make_settings()
        with (
            patch.object(
                type(s),
                "_toml",
                new_callable=lambda: property(lambda self: {"system": {"index_number_of_replicas": -1}}),
            ),
            pytest.raises(ValueError, match=">= 0"),
        ):
            _ = s.system_index_number_of_replicas

    def test_writer_enabled_default_true(self) -> None:
        s = _make_settings()
        assert s.system_writer_enabled is True

    def test_query_enabled_default_true(self) -> None:
        s = _make_settings()
        assert s.system_query_enabled is True

    def test_archive_enabled_default_false(self) -> None:
        s = _make_settings()
        assert s.system_archive_enabled is False

    def test_archive_interval_seconds_positive(self) -> None:
        s = _make_settings()
        assert s.system_archive_interval_seconds > 0

    def test_metrics_log_interval_seconds_positive(self) -> None:
        s = _make_settings()
        assert s.system_metrics_log_interval_seconds > 0


class TestOpenSearchConfig:
    """设计 §7.3 的 OpenSearch 连接键测试。"""

    def test_opensearch_hosts_default(self) -> None:
        s = _make_settings()
        assert "9200" in s.opensearch_hosts

    def test_opensearch_hosts_env_override(self) -> None:
        s = _make_settings(OPENSEARCH_HOSTS="https://os-cluster:9200,https://os-cluster2:9200")
        assert "os-cluster" in s.opensearch_hosts

    def test_opensearch_user_default_empty(self) -> None:
        s = _make_settings()
        assert s.opensearch_user == ""

    def test_opensearch_password_default_empty(self) -> None:
        s = _make_settings()
        assert s.opensearch_password == ""

    def test_opensearch_verify_certs_default_false(self) -> None:
        s = _make_settings()
        assert s.opensearch_verify_certs is False

    def test_opensearch_verify_certs_env_override(self) -> None:
        s = _make_settings(OPENSEARCH_VERIFY_CERTS="true")
        assert s.opensearch_verify_certs is True
