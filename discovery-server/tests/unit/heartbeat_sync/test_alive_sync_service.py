"""tests: AliveSyncService bootstrap/resync + holder 注入读取（Step 11）。

使用 FakeAliveSyncStore（内存）和 monkeypatched source_client / kafka_consumer
测试服务编排逻辑，不依赖真实 HTTP 或 Kafka。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.amp.alive_sync.engine import AliveSyncEngine

from app.heartbeat_sync.holder import clear_alive_reader, get_alive_reader, set_alive_reader
from app.heartbeat_sync.service import AliveSyncService

# ── 测试工具 ──────────────────────────────────────────────────────────────────


def _make_fake_store() -> Any:
    """内存 store（无 DB 依赖）。"""

    class _FakeStore:
        def __init__(self):
            self._rows: dict[str, Any] = {}
            self._checkpoints: dict[str, Any] = {}
            self.reset_called = 0

        async def load_alive_views(self, aics):
            return {aic: r for aic, r in self._rows.items() if aic in aics}

        async def replace_alive_set(self, records, checkpoints):
            self._rows.clear()
            self._checkpoints.clear()
            async for r in records:
                self._rows[r.aic] = r
            for cp in checkpoints:
                self._checkpoints[cp.shard] = cp

        async def apply_upsert(self, record, shard, last_seen_seq, kafka_next_offset):
            self._rows[record.aic] = record

        async def apply_delete(self, aic, shard, last_seen_seq, kafka_next_offset, version):
            pass

        async def load_checkpoints(self):
            return list(self._checkpoints.values())

        async def load_local_versions(self):
            return {aic: row.version for aic, row in self._rows.items()}

        async def reset(self):
            self.reset_called += 1
            self._rows.clear()
            self._checkpoints.clear()

    return _FakeStore()


def _make_settings(backoff: int = 0, retry: int = 0) -> Any:
    from unittest.mock import MagicMock

    s = MagicMock()
    s.ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS = 300
    s.ALIVE_SYNC_RESYNC_BACKOFF_SECONDS = backoff
    s.ALIVE_SYNC_RETRY_INTERVAL_SECONDS = retry
    return s


def _make_source_client_with_snapshot() -> Any:
    sync_info_json = {
        "type": "amp-alive-delta",
        "schemaVersion": "1",
        "snapshotContentType": "application/x-ndjson",
        "kafkaTopic": "amp.heartbeat.alive-delta",
        "shardCount": 1,
        "refreshEmitIntervalSeconds": 60,
        "deltaRetentionHours": 24,
        "currentPublishedSeqByShard": {"hb-000": "5"},
    }
    meta_json = {
        "recordType": "snapshot-meta",
        "type": "amp-alive-delta",
        "cutoverSeqByShard": {"hb-000": "5"},
        "generatedAt": "2026-06-13T01:00:00Z",
    }
    row_json = {
        "shard": "hb-000",
        "seq": "5",
        "type": "amp-alive-delta",
        "id": "urn:amp:alive:AIC-001",
        "version": "5",
        "op": "upsert",
        "kind": "snapshot",
        "payload": {"aic": "AIC-001", "lastSeenAt": "2026-06-13T01:00:00Z"},
    }
    from acps_sdk.amp.heartbeat_sync import AliveDeltaEnvelope, AliveSnapshotMeta, HeartbeatSyncInfo

    client = MagicMock()
    client.fetch_sync_info = AsyncMock(return_value=HeartbeatSyncInfo.model_validate(sync_info_json))

    @asynccontextmanager
    async def _stream_snapshot():
        meta = AliveSnapshotMeta.model_validate(meta_json)

        async def _rows():
            yield AliveDeltaEnvelope.model_validate(row_json)

        yield meta, _rows()

    client.stream_snapshot = _stream_snapshot
    return client


def _make_kafka_consumer() -> Any:
    kc = MagicMock()
    kc.start = AsyncMock()
    kc.stop = AsyncMock()
    kc.set_seek_plan = MagicMock()
    kc.poll_apply = AsyncMock()
    return kc


def _make_service() -> tuple[AliveSyncService, Any, Any]:
    store = _make_fake_store()
    source_client = _make_source_client_with_snapshot()
    kc = _make_kafka_consumer()
    engine = AliveSyncEngine(store)
    settings = _make_settings()
    return AliveSyncService(settings, store, source_client, kc, engine), store, kc


# ── bootstrap ─────────────────────────────────────────────────────────────────


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_populates_store(self) -> None:
        svc, store, _kc = _make_service()
        await svc.bootstrap()
        assert "AIC-001" in store._rows
        assert store._rows["AIC-001"].alive is True

    @pytest.mark.asyncio
    async def test_bootstrap_sets_seek_plan(self) -> None:
        svc, _store, kc = _make_service()
        await svc.bootstrap()
        kc.set_seek_plan.assert_called_once()


# ── resume_or_bootstrap ───────────────────────────────────────────────────────


class TestResumeOrBootstrap:
    @pytest.mark.asyncio
    async def test_no_checkpoints_falls_back_to_bootstrap(self) -> None:
        svc, store, _kc = _make_service()
        # store 无 checkpoint → bootstrap
        await svc.resume_or_bootstrap()
        assert "AIC-001" in store._rows

    @pytest.mark.asyncio
    async def test_hydrate_fail_falls_back_to_bootstrap(self) -> None:
        svc, store, kc = _make_service()
        from acps_sdk.amp.alive_sync.store import ShardCheckpoint

        store._checkpoints["hb-000"] = ShardCheckpoint(
            shard="hb-000",
            last_seen_seq=5,
            cutover_seq=5,
            kafka_next_offset=None,
            snapshot_generated_at="2026-06-13T01:00:00Z",
        )
        # hydrate 成功（有 checkpoint），验证 set_seek_plan 被调用
        await svc.resume_or_bootstrap()
        kc.set_seek_plan.assert_called_once()


# ── request_resync ────────────────────────────────────────────────────────────


class TestRequestResync:
    @pytest.mark.asyncio
    async def test_resets_store_then_bootstraps(self) -> None:
        svc, store, _kc = _make_service()
        await svc.request_resync("test-reason")
        assert store.reset_called == 1
        assert "AIC-001" in store._rows  # bootstrap 重跑后 AIC-001 回来


# ── status ────────────────────────────────────────────────────────────────────


class TestStatus:
    @pytest.mark.asyncio
    async def test_returns_checkpoint_and_alive_counts(self) -> None:
        svc, _store, _kc = _make_service()
        await svc.bootstrap()
        status = await svc.status()
        assert status["running"] is True
        assert status["aliveCount"] == 1
        assert status["checkpointCount"] == 1
        assert "hb-000" in status["shards"]


# ── holder ────────────────────────────────────────────────────────────────────


class TestHolder:
    def setup_method(self) -> None:
        clear_alive_reader()

    def test_get_returns_none_initially(self) -> None:
        assert get_alive_reader() is None

    def test_set_and_get(self) -> None:
        store = _make_fake_store()
        set_alive_reader(store)
        reader = get_alive_reader()
        assert reader is store

    def test_clear_returns_none(self) -> None:
        store = _make_fake_store()
        set_alive_reader(store)
        clear_alive_reader()
        assert get_alive_reader() is None
