"""Unit tests for app/heartbeat/sync_service.py — 全部 mock（TDD red phase）。

运行：just test unit -k heartbeat_sync_service
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── ensure_sync_enabled ───────────────────────────────────────────────────────


class TestEnsureSyncEnabled:
    def test_raises_sync_disabled_error_when_disabled(self) -> None:
        """sync_enabled=False 时 ensure_sync_enabled() 抛出 SyncDisabledError（404）。"""
        from app.heartbeat.exception import SyncDisabledError
        from app.heartbeat.sync_service import ensure_sync_enabled

        with patch("app.heartbeat.sync_service.settings") as mock_settings:
            mock_settings.heartbeat_sync_enabled = False
            with pytest.raises(SyncDisabledError):
                ensure_sync_enabled()

    def test_no_error_when_enabled(self) -> None:
        """sync_enabled=True 时不抛出异常。"""
        from app.heartbeat.sync_service import ensure_sync_enabled

        with patch("app.heartbeat.sync_service.settings") as mock_settings:
            mock_settings.heartbeat_sync_enabled = True
            ensure_sync_enabled()  # should not raise


# ── get_sync_info ─────────────────────────────────────────────────────────────


class TestGetSyncInfo:
    async def test_returns_correct_heartbeat_sync_info(self) -> None:
        """get_sync_info 返回正确的 HeartbeatSyncInfo，包含 currentPublishedSeqByShard。"""
        from acps_sdk.amp.heartbeat_sync import HeartbeatSyncInfo

        from app.heartbeat.sync_service import get_sync_info

        mock_redis = AsyncMock()

        with (
            patch(
                "app.heartbeat.sync_service.store.read_all_published_seq",
                AsyncMock(return_value={"hb-000": 42, "hb-001": 7}),
            ),
            patch("app.heartbeat.sync_service.settings") as mock_settings,
        ):
            mock_settings.heartbeat_delta_topic = "amp.heartbeat.alive-delta"
            mock_settings.heartbeat_heartbeat_shard_count = 2
            mock_settings.heartbeat_refresh_emit_interval_seconds = 30
            mock_settings.heartbeat_delta_retention_hours = 168

            result = await get_sync_info(mock_redis)

        assert isinstance(result, HeartbeatSyncInfo)
        assert result.kafka_topic == "amp.heartbeat.alive-delta"
        assert result.shard_count == 2
        assert result.current_published_seq_by_shard == {"hb-000": "42", "hb-001": "7"}
        assert result.refresh_emit_interval_seconds == 30

    async def test_raises_snapshot_unavailable_on_redis_connection_error(self) -> None:
        """8-8: Redis ConnectionError 被捕获 → SnapshotUnavailableError。"""
        from redis.exceptions import ConnectionError as RedisConnectionError

        from app.heartbeat.exception import SnapshotUnavailableError
        from app.heartbeat.sync_service import get_sync_info

        mock_redis = AsyncMock()

        with (
            patch(
                "app.heartbeat.sync_service.store.read_all_published_seq",
                AsyncMock(side_effect=RedisConnectionError("connection refused")),
            ),
            pytest.raises(SnapshotUnavailableError),
        ):
            await get_sync_info(mock_redis)

    async def test_raises_snapshot_unavailable_on_redis_timeout(self) -> None:
        """8-8: Redis TimeoutError 被捕获 → SnapshotUnavailableError。"""
        from redis.exceptions import TimeoutError as RedisTimeoutError

        from app.heartbeat.exception import SnapshotUnavailableError
        from app.heartbeat.sync_service import get_sync_info

        mock_redis = AsyncMock()

        with (
            patch(
                "app.heartbeat.sync_service.store.read_all_published_seq",
                AsyncMock(side_effect=RedisTimeoutError("timeout")),
            ),
            pytest.raises(SnapshotUnavailableError),
        ):
            await get_sync_info(mock_redis)


# ── _ensure_delta_log_healthy ─────────────────────────────────────────────────


class TestEnsureDeltaLogHealthy:
    async def test_raises_delta_log_unhealthy_when_lag_exceeds_threshold(
        self,
    ) -> None:
        """8-7: outbox_publish_lag_ms 超限 → DeltaLogUnhealthyError。"""
        from app.heartbeat.exception import DeltaLogUnhealthyError
        from app.heartbeat.sync_service import _ensure_delta_log_healthy

        mock_redis = AsyncMock()

        with (
            patch("app.heartbeat.sync_service.all_shard_ids", return_value=["hb-000"]),
            patch(
                "app.heartbeat.sync_service.store.outbox_publish_lag_ms",
                AsyncMock(return_value=99_000),  # 99s lag
            ),
            patch("app.heartbeat.sync_service.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30

            with pytest.raises(DeltaLogUnhealthyError):
                await _ensure_delta_log_healthy(mock_redis)

    async def test_no_error_when_lag_within_threshold(self) -> None:
        """lag < threshold 时不抛出异常。"""
        from app.heartbeat.sync_service import _ensure_delta_log_healthy

        mock_redis = AsyncMock()

        with (
            patch("app.heartbeat.sync_service.all_shard_ids", return_value=["hb-000"]),
            patch(
                "app.heartbeat.sync_service.store.outbox_publish_lag_ms",
                AsyncMock(return_value=1_000),  # 1s lag
            ),
            patch("app.heartbeat.sync_service.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30

            await _ensure_delta_log_healthy(mock_redis)  # should not raise

    async def test_no_error_when_lag_is_none(self) -> None:
        """outbox 为空（lag=None）时不抛出异常。"""
        from app.heartbeat.sync_service import _ensure_delta_log_healthy

        mock_redis = AsyncMock()

        with (
            patch("app.heartbeat.sync_service.all_shard_ids", return_value=["hb-000"]),
            patch(
                "app.heartbeat.sync_service.store.outbox_publish_lag_ms",
                AsyncMock(return_value=None),
            ),
            patch("app.heartbeat.sync_service.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30

            await _ensure_delta_log_healthy(mock_redis)  # should not raise

    async def test_uses_outbox_publish_lag_ms_not_pel_only(self) -> None:
        """8-7: 使用 outbox_publish_lag_ms（非 PEL-only 函数，P1-2）。"""
        from app.heartbeat.sync_service import _ensure_delta_log_healthy

        mock_redis = AsyncMock()
        mock_lag = AsyncMock(return_value=None)

        with (
            patch("app.heartbeat.sync_service.all_shard_ids", return_value=["hb-000"]),
            patch("app.heartbeat.sync_service.store.outbox_publish_lag_ms", mock_lag),
            patch("app.heartbeat.sync_service.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30
            await _ensure_delta_log_healthy(mock_redis)

        mock_lag.assert_called_once_with(mock_redis, "hb-000")


# ── stream_snapshot ───────────────────────────────────────────────────────────


class TestStreamSnapshot:
    async def test_raises_delta_log_unhealthy_before_streaming(self) -> None:
        """lag 超限时在开始 stream 之前抛出 DeltaLogUnhealthyError。"""
        from app.heartbeat.exception import DeltaLogUnhealthyError
        from app.heartbeat.sync_service import stream_snapshot

        mock_redis = AsyncMock()
        mock_exporter = MagicMock()

        with (
            patch(
                "app.heartbeat.sync_service._ensure_delta_log_healthy",
                AsyncMock(side_effect=DeltaLogUnhealthyError()),
            ),
            pytest.raises(DeltaLogUnhealthyError),
        ):
            async for _ in stream_snapshot(mock_redis, mock_exporter):
                pass

    async def test_yields_lines_from_exporter_when_healthy(self) -> None:
        """健康状态下 stream_snapshot 产出 exporter.stream() 的字节。"""
        from app.heartbeat.sync_service import stream_snapshot

        mock_redis = AsyncMock()

        async def fake_stream(redis: object) -> object:
            yield b'{"recordType":"snapshot-meta"}\n'
            yield b'{"id":"urn:amp:alive:aic-001"}\n'

        mock_exporter = MagicMock()
        mock_exporter.stream.side_effect = fake_stream

        chunks: list[bytes] = []

        with patch(
            "app.heartbeat.sync_service._ensure_delta_log_healthy",
            AsyncMock(),
        ):
            async for chunk in stream_snapshot(mock_redis, mock_exporter):
                chunks.append(chunk)

        assert chunks[0] == b'{"recordType":"snapshot-meta"}\n'
        assert chunks[1] == b'{"id":"urn:amp:alive:aic-001"}\n'

    async def test_raises_snapshot_unavailable_on_stream_redis_error(self) -> None:
        """8-8: exporter.stream() 中的 Redis 异常被捕获 → SnapshotUnavailableError。"""
        from redis.exceptions import ConnectionError as RedisConnectionError

        from app.heartbeat.exception import SnapshotUnavailableError
        from app.heartbeat.sync_service import stream_snapshot

        mock_redis = AsyncMock()

        async def fake_erroring_stream(redis: object) -> object:
            raise RedisConnectionError("lost connection")
            yield  # make it a generator

        mock_exporter = MagicMock()
        mock_exporter.stream.side_effect = fake_erroring_stream

        with (
            patch(
                "app.heartbeat.sync_service._ensure_delta_log_healthy",
                AsyncMock(),
            ),
            pytest.raises(SnapshotUnavailableError),
        ):
            async for _ in stream_snapshot(mock_redis, mock_exporter):
                pass
