"""tests: Settings.ALIVE_SYNC_* 字段默认值 + TOML 映射 + URL 校验（Step 6）。"""

import pytest

from app.core.config import Settings, _flatten_toml_settings


class TestAliveSyncDefaults:
    def test_defaults(self) -> None:
        s = Settings()
        assert s.ALIVE_SYNC_ENABLED is False
        assert s.ALIVE_SYNC_AUTO_START is True
        assert s.ALIVE_SYNC_PROVIDER_BASE_URL == ""
        assert s.ALIVE_SYNC_HTTP_TIMEOUT == 30.0
        assert s.ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS == ""
        assert s.ALIVE_SYNC_KAFKA_GROUP_ID == "discovery-server.alive-sync.v1"
        assert s.ALIVE_SYNC_KAFKA_TOPIC == ""
        assert s.ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS == 300
        assert s.ALIVE_SYNC_BOOTSTRAP_MAX_LOOKBACK_SECONDS == 86400
        assert s.ALIVE_SYNC_RESYNC_BACKOFF_SECONDS == 10
        assert s.ALIVE_SYNC_RETRY_INTERVAL_SECONDS == 30


class TestAliveSyncTomlFlattening:
    def test_flatten_alive_sync_enabled(self) -> None:
        config = {"alive_sync": {"enabled": True, "provider_base_url": "http://localhost:9009/acps-amp-v1/heartbeat"}}
        flat = _flatten_toml_settings(config)
        assert flat.get("ALIVE_SYNC_ENABLED") is True
        assert flat.get("ALIVE_SYNC_PROVIDER_BASE_URL") == "http://localhost:9009/acps-amp-v1/heartbeat"

    def test_flatten_alive_sync_kafka_fields(self) -> None:
        config = {
            "alive_sync": {
                "kafka_bootstrap_servers": "localhost:19092",
                "kafka_group_id": "discovery-server.alive-sync.v1",
                "bootstrap_lookback_seconds": 600,
            }
        }
        flat = _flatten_toml_settings(config)
        assert flat.get("ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS") == "localhost:19092"
        assert flat.get("ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS") == 600

    def test_flatten_absent_alive_sync_not_in_flat(self) -> None:
        flat = _flatten_toml_settings({})
        # 未配置时不应出现在 flat 中
        assert "ALIVE_SYNC_ENABLED" not in flat


class TestAliveSyncUrlValidation:
    def test_valid_provider_url_accepted(self) -> None:
        s = Settings(ALIVE_SYNC_PROVIDER_BASE_URL="http://localhost:9009/acps-amp-v1/heartbeat")
        assert s.ALIVE_SYNC_PROVIDER_BASE_URL.startswith("http")

    def test_invalid_provider_url_raises(self) -> None:
        with pytest.raises(Exception, match="absolute http"):
            Settings(ALIVE_SYNC_PROVIDER_BASE_URL="not-a-url")

    def test_empty_provider_url_not_validated(self) -> None:
        s = Settings(ALIVE_SYNC_PROVIDER_BASE_URL="")
        assert s.ALIVE_SYNC_PROVIDER_BASE_URL == ""
