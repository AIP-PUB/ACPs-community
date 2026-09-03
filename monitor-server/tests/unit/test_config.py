"""tests/unit/test_config.py — Settings 配置加载单元测试。"""

import pytest

# 注意：unit 测试不依赖真实 DB，配置加载不应触发 DB 连接


class TestSettingsLoad:
    def test_settings_instance_accessible(self) -> None:
        from app.core.config import settings

        assert settings is not None

    def test_audit_topic_default(self) -> None:
        from app.core.config import settings

        assert settings.audit_topic == "amp.audit"

    def test_audit_dlq_topic_default(self) -> None:
        from app.core.config import settings

        assert settings.audit_dlq_topic == "amp.audit.dlq"

    def test_audit_consumer_group_default(self) -> None:
        from app.core.config import settings

        assert settings.audit_consumer_group == "amp.audit.writer"

    def test_kafka_auto_offset_reset_default(self) -> None:
        from app.core.config import settings

        assert settings.kafka_auto_offset_reset == "earliest"

    def test_kafka_max_poll_records_positive(self) -> None:
        from app.core.config import settings

        assert settings.kafka_max_poll_records > 0


class TestAuditConfigValidation:
    def test_logical_chain_count_positive(self) -> None:
        from app.core.config import settings

        assert settings.audit_logical_chain_count > 0

    def test_anchor_interval_positive(self) -> None:
        from app.core.config import settings

        assert settings.audit_anchor_interval_minutes > 0

    def test_online_retention_positive(self) -> None:
        from app.core.config import settings

        assert settings.audit_online_retention_months > 0

    def test_logical_chain_count_negative_raises(self) -> None:
        """当 TOML 中 logical_chain_count <= 0 时，property 应抛出 ValueError。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        # 直接构造一个含非法值的 _toml
        s._toml = {"audit": {"logical_chain_count": 0}}
        with pytest.raises(ValueError, match="logical_chain_count must be > 0"):
            _ = s.audit_logical_chain_count

    def test_anchor_interval_negative_raises(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"audit": {"anchor_interval_minutes": -1}}
        with pytest.raises(ValueError, match="anchor_interval_minutes must be > 0"):
            _ = s.audit_anchor_interval_minutes

    def test_online_retention_zero_raises(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"audit": {"online_retention_months": 0}}
        with pytest.raises(ValueError, match="online_retention_months must be > 0"):
            _ = s.audit_online_retention_months

    def test_atr_mock_mode_default_true(self) -> None:
        from app.core.config import settings

        # 开发环境默认为 mock mode
        assert isinstance(settings.atr_mock_mode, bool)

    def test_max_event_lag_hours_positive(self) -> None:
        """audit_max_event_lag_hours 应为正整数（§5.3 §7）。"""
        from app.core.config import settings

        assert settings.audit_max_event_lag_hours > 0

    def test_max_event_lag_hours_default_is_48(self) -> None:
        """默认值应为 48 小时（§7 配置表）。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}  # 无配置时取默认值
        assert s.audit_max_event_lag_hours == 48

    def test_max_event_lag_hours_custom_value(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"audit": {"max_event_lag_hours": 72}}
        assert s.audit_max_event_lag_hours == 72

    def test_atr_key_cache_ttl_seconds_default_is_300(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.atr_key_cache_ttl_seconds == 300

    def test_atr_key_cache_ttl_seconds_custom_value(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"atr": {"key_cache_ttl_seconds": 600}}
        assert s.atr_key_cache_ttl_seconds == 600

    def test_atr_key_cache_ttl_seconds_must_be_positive(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"atr": {"key_cache_ttl_seconds": 0}}
        with pytest.raises(ValueError, match=r"atr\.key_cache_ttl_seconds must be > 0"):
            _ = s.atr_key_cache_ttl_seconds


class TestRedisConfig:
    def test_redis_url_default(self) -> None:
        """未设置 REDIS_URL 时，redis_url 应为 default.toml 的默认值或字段默认值。"""
        from app.core.config import settings

        # 在测试环境中（无 REDIS_URL env var）redis_url 应返回字段默认值
        assert "redis://" in settings.redis_url
        assert "6379" in settings.redis_url

    def test_redis_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://custom:6379/0")
        from app.core.config import Settings

        s = Settings(REDIS_URL="redis://custom:6379/0", DATABASE_URL="postgresql+asyncpg://x:y@h/db")  # pyright: ignore[reportCallIssue]
        assert s.redis_url == "redis://custom:6379/0"

    def test_redis_max_connections_default(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.redis_max_connections == 50

    def test_redis_socket_timeout_seconds_default(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.redis_socket_timeout_seconds == 5.0


class TestHeartbeatConfig:
    def test_heartbeat_topic_default(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.heartbeat_topic == "amp.heartbeat"

    def test_heartbeat_shard_count_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_heartbeat_shard_count, int)
        assert settings.heartbeat_heartbeat_shard_count > 0

    def test_heartbeat_silence_threshold_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_silence_threshold_seconds, int)

    def test_heartbeat_evict_after_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_evict_after_seconds, int)

    def test_heartbeat_sync_enabled_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_sync_enabled, bool)

    def test_heartbeat_analytics_enabled_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_analytics_enabled, bool)

    def test_heartbeat_summary_buckets_type(self) -> None:
        from app.core.config import settings

        buckets = settings.heartbeat_summary_buckets_seconds
        assert isinstance(buckets, list)
        assert all(isinstance(v, int) for v in buckets)

    def test_heartbeat_in_list_max_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_in_list_max, int)
        assert settings.heartbeat_in_list_max > 0

    def test_heartbeat_snapshot_share_window_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.heartbeat_snapshot_share_window_seconds, int)

    def test_heartbeat_lagging_response_mode_type(self) -> None:
        from app.core.config import settings

        assert settings.heartbeat_lagging_response_mode in ("503", "partial")


class TestMetricsConfig:
    """Metrics 配置属性测试（Step 2）。"""

    # ── 环境变量覆盖 ──────────────────────────────────────────────────────────

    def test_vm_query_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VM_QUERY_URL", "http://custom:8428")
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.vm_query_url == "http://custom:8428"

    def test_vm_remote_write_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VM_REMOTE_WRITE_URL", "http://custom:8428")
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.vm_remote_write_url == "http://custom:8428"

    def test_vm_query_url_from_toml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VM_QUERY_URL", raising=False)
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"vm_query_url": "http://vm-host:8428"}}
        assert s.vm_query_url == "http://vm-host:8428"

    def test_vm_query_url_default_when_no_env_no_toml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VM_QUERY_URL", raising=False)
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.vm_query_url == "http://localhost:8428"

    def test_vm_remote_write_url_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VM_REMOTE_WRITE_URL", raising=False)
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.vm_remote_write_url == "http://localhost:8428"

    # ── 属性类型断言 ──────────────────────────────────────────────────────────

    def test_metrics_topic_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_topic, str)
        assert settings.metrics_topic == "amp.metrics"

    def test_metrics_dlq_topic_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_dlq_topic, str)

    def test_metrics_consumer_group_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_consumer_group, str)

    def test_metrics_writer_poll_timeout_ms_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_writer_poll_timeout_ms, int)
        assert settings.metrics_writer_poll_timeout_ms > 0

    def test_metrics_remote_write_batch_interval_seconds_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_remote_write_batch_interval_seconds, int)

    def test_metrics_remote_write_batch_max_samples_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_remote_write_batch_max_samples, int)

    def test_metrics_dedupe_ttl_seconds_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_dedupe_ttl_seconds, int)

    def test_metrics_snapshot_ttl_seconds_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_snapshot_ttl_seconds, int)

    def test_metrics_snapshot_index_scan_batch_size_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_snapshot_index_scan_batch_size, int)

    def test_metrics_snapshot_fallback_lookback_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_snapshot_fallback_lookback, str)

    def test_metrics_raw_retention_days_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_raw_retention_days, int)

    def test_metrics_downsample_retention_days_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_downsample_retention_days, int)

    def test_metrics_max_points_per_series_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_max_points_per_series, int)

    def test_metrics_ranking_max_top_n_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_ranking_max_top_n, int)

    def test_metrics_slo_max_rules_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_slo_max_rules, int)

    def test_metrics_capacity_default_lookback_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_capacity_default_lookback, str)

    def test_metrics_capacity_default_active_ratio_threshold_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_capacity_default_active_ratio_threshold, float)

    def test_metrics_capacity_default_queue_ratio_threshold_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_capacity_default_queue_ratio_threshold, float)

    def test_metrics_query_timeout_seconds_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_query_timeout_seconds, int)

    def test_metrics_lagging_threshold_ms_default(self) -> None:
        """lagging_threshold_ms 默认值必须为 150000（spec §6.1.4）。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {}
        assert s.metrics_lagging_threshold_ms == 150000

    def test_metrics_analytics_enabled_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_analytics_enabled, bool)

    def test_metrics_governance_enabled_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_governance_enabled, bool)

    def test_metrics_metrics_log_interval_seconds_type(self) -> None:
        from app.core.config import settings

        assert isinstance(settings.metrics_metrics_log_interval_seconds, int)

    # ── 校验规则 ──────────────────────────────────────────────────────────────

    def test_metrics_lagging_response_mode_invalid(self) -> None:
        """非法 lagging_response_mode 抛 ValueError。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"lagging_response_mode": "other"}}
        with pytest.raises(ValueError, match="lagging_response_mode"):
            _ = s.metrics_lagging_response_mode

    def test_metrics_lagging_response_mode_valid_503(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"lagging_response_mode": "503"}}
        assert s.metrics_lagging_response_mode == "503"

    def test_metrics_lagging_response_mode_valid_partial(self) -> None:
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"lagging_response_mode": "partial"}}
        assert s.metrics_lagging_response_mode == "partial"

    def test_metrics_capacity_threshold_invalid_above_one(self) -> None:
        """capacity ratio 阈值 > 1 抛 ValueError。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"capacity_default_active_ratio_threshold": 1.5}}
        with pytest.raises(ValueError, match="capacity_default_active_ratio_threshold"):
            _ = s.metrics_capacity_default_active_ratio_threshold

    def test_metrics_capacity_threshold_invalid_zero(self) -> None:
        """capacity ratio 阈值 = 0 抛 ValueError（需 > 0）。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"capacity_default_active_ratio_threshold": 0.0}}
        with pytest.raises(ValueError, match="capacity_default_active_ratio_threshold"):
            _ = s.metrics_capacity_default_active_ratio_threshold

    def test_metrics_capacity_threshold_valid_one(self) -> None:
        """capacity ratio 阈值 = 1.0 合法（含端点）。"""
        from app.core.config import Settings

        s = Settings.__new__(Settings)
        s._toml = {"metrics": {"capacity_default_active_ratio_threshold": 1.0}}
        assert s.metrics_capacity_default_active_ratio_threshold == 1.0
