"""tests: AliveSyncEngine — hydrate / apply_snapshot / apply_delta 有状态路径。"""
from __future__ import annotations

import json
from typing import AsyncIterable

import pytest
import pytest_asyncio

from acps_sdk.amp.alive_sync.engine import AliveSyncEngine, DeltaDecision
from acps_sdk.amp.alive_sync.errors import GapDetectedError, ResyncRequired
from acps_sdk.amp.alive_sync.store import AliveRecord, ShardCheckpoint
from acps_sdk.amp.heartbeat_sync import (
    AliveDeltaEnvelope,
    AliveSetEntry,
    AliveSnapshotMeta,
)

# ── 工具函数 ─────────────────────────────────────────────────────────────────


def _make_envelope(
    seq: int,
    op: str = "upsert",
    kind: str = "enter_alive",
    aic: str = "AIC-001",
    shard: str = "hb-000",
) -> AliveDeltaEnvelope:
    return AliveDeltaEnvelope(
        shard=shard,
        seq=str(seq),
        type="amp-alive-delta",
        id=f"urn:amp:alive:{aic}",
        version=str(seq),
        op=op,
        kind=kind,
        payload=AliveSetEntry(aic=aic, lastSeenAt="2026-06-13T01:20:00Z"),
    )


def _make_snapshot_env(
    seq: int,
    aic: str = "AIC-001",
    shard: str = "hb-000",
) -> AliveDeltaEnvelope:
    return AliveDeltaEnvelope(
        shard=shard,
        seq=str(seq),
        type="amp-alive-delta",
        id=f"urn:amp:alive:{aic}",
        version=str(seq),
        op="upsert",
        kind="snapshot",
        payload=AliveSetEntry(aic=aic, lastSeenAt="2026-06-13T01:00:00Z"),
    )


async def _agen(*items: AliveDeltaEnvelope) -> AsyncIterable[AliveDeltaEnvelope]:
    for item in items:
        yield item


# ── hydrate ──────────────────────────────────────────────────────────────────


class TestHydrate:
    @pytest.mark.asyncio
    async def test_no_checkpoints_raises_resync(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        with pytest.raises(ResyncRequired):
            await engine.hydrate()

    @pytest.mark.asyncio
    async def test_hydrate_restores_memory(self, fake_store) -> None:
        fake_store._checkpoints["hb-000"] = ShardCheckpoint(
            shard="hb-000",
            last_seen_seq=10,
            cutover_seq=5,
            kafka_next_offset=100,
            snapshot_generated_at="2026-06-13T00:00:00Z",
        )
        fake_store._rows["AIC-001"] = AliveRecord(
            aic="AIC-001",
            alive=True,
            last_seen_at="2026-06-13T00:00:00Z",
            version=10,
            shard="hb-000",
        )
        engine = AliveSyncEngine(fake_store)
        await engine.hydrate()
        assert engine._last_seen_seq == {"hb-000": 10}
        assert engine._local_version == {"AIC-001": 10}


# ── apply_snapshot ──────────────────────────────────────────────────────────


class TestApplySnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_replaces_store_and_updates_memory(self, fake_store) -> None:
        meta = AliveSnapshotMeta(
            recordType="snapshot-meta",
            type="amp-alive-delta",
            cutoverSeqByShard={"hb-000": "20"},
            generatedAt="2026-06-13T01:00:00Z",
        )
        rows = _agen(_make_snapshot_env(seq=20, aic="AIC-001"))
        engine = AliveSyncEngine(fake_store)
        await engine.apply_snapshot(meta, rows)

        # store 中应有 AIC-001
        assert "AIC-001" in fake_store._rows
        assert fake_store._rows["AIC-001"].alive is True
        assert fake_store._rows["AIC-001"].version == 20
        # checkpoint 应更新
        assert fake_store._checkpoints["hb-000"].last_seen_seq == 20
        assert fake_store._checkpoints["hb-000"].cutover_seq == 20
        # 内存水位
        assert engine._last_seen_seq["hb-000"] == 20
        assert engine._local_version["AIC-001"] == 20

    @pytest.mark.asyncio
    async def test_snapshot_memory_not_updated_on_store_error(self, fake_store) -> None:
        """store 异常时内存水位不应被更新（安全保证）。"""
        async def _failing_replace(records, checkpoints):
            # 先消耗 records（模拟开始流式写）再抛异常
            async for _ in records:
                pass
            raise RuntimeError("db error")

        fake_store.replace_alive_set = _failing_replace

        meta = AliveSnapshotMeta(
            recordType="snapshot-meta",
            type="amp-alive-delta",
            cutoverSeqByShard={"hb-000": "30"},
            generatedAt="2026-06-13T02:00:00Z",
        )
        rows = _agen(_make_snapshot_env(seq=30, aic="AIC-002"))
        engine = AliveSyncEngine(fake_store)
        # 初始内存为空
        with pytest.raises(RuntimeError):
            await engine.apply_snapshot(meta, rows)
        # 内存水位不应被推进
        assert engine._last_seen_seq == {}


# ── apply_delta ──────────────────────────────────────────────────────────────


class TestApplyDelta:
    @pytest.mark.asyncio
    async def test_apply_upsert_enter_alive(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 0

        env = _make_envelope(seq=1, op="upsert", kind="enter_alive")
        decision = await engine.apply_delta(env, kafka_next_offset=101)

        assert decision is DeltaDecision.APPLY_UPSERT
        assert engine._last_seen_seq["hb-000"] == 1
        assert engine._local_version["AIC-001"] == 1
        assert fake_store._rows["AIC-001"].alive is True

    @pytest.mark.asyncio
    async def test_apply_delete_leave_alive(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 5
        engine._local_version["AIC-001"] = 5
        # 预置存活行
        fake_store._rows["AIC-001"] = AliveRecord(
            aic="AIC-001",
            alive=True,
            last_seen_at="2026-06-13T01:00:00Z",
            version=5,
            shard="hb-000",
        )

        env = _make_envelope(seq=6, op="delete", kind="leave_alive")
        decision = await engine.apply_delta(env)

        assert decision is DeltaDecision.APPLY_DELETE
        assert fake_store._rows["AIC-001"].alive is False
        assert engine._local_version["AIC-001"] == 6  # 保留+更新 version

    @pytest.mark.asyncio
    async def test_gap_raises_gap_detected(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 4

        env = _make_envelope(seq=10)
        with pytest.raises(GapDetectedError) as exc_info:
            await engine.apply_delta(env)
        assert exc_info.value.expected_seq == 5
        assert exc_info.value.got_seq == 10

    @pytest.mark.asyncio
    async def test_skip_seq_gate_duplicate(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 10

        env = _make_envelope(seq=5)
        decision = await engine.apply_delta(env)
        assert decision is DeltaDecision.SKIP_SEQ_GATE
        # 重复事件不写库
        assert "AIC-001" not in fake_store._rows

    @pytest.mark.asyncio
    async def test_skip_version_stale(self, fake_store) -> None:
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 4
        engine._local_version["AIC-001"] = 10

        env = _make_envelope(seq=5, op="upsert")
        decision = await engine.apply_delta(env)
        assert decision is DeltaDecision.SKIP_VERSION
        assert "AIC-001" not in fake_store._rows

    @pytest.mark.asyncio
    async def test_leave_alive_then_enter_alive_reaccepts(self, fake_store) -> None:
        """leave 后旧重复被 seq 闸门拦截，新 enter 正常接受（§5.4 保留+自然有界）。"""
        engine = AliveSyncEngine(fake_store)
        engine._last_seen_seq["hb-000"] = 5
        engine._local_version["AIC-001"] = 5
        fake_store._rows["AIC-001"] = AliveRecord(
            aic="AIC-001", alive=True,
            last_seen_at="2026-06-13T01:00:00Z", version=5, shard="hb-000",
        )

        # leave_alive seq=6
        leave = _make_envelope(seq=6, op="delete", kind="leave_alive")
        await engine.apply_delta(leave)
        assert fake_store._rows["AIC-001"].alive is False

        # 旧重复 seq=4（≤ last_seen_seq=6）被 gate 拦截
        old_dup = _make_envelope(seq=4, op="upsert", kind="enter_alive")
        d2 = await engine.apply_delta(old_dup)
        assert d2 is DeltaDecision.SKIP_SEQ_GATE

        # 新 enter_alive seq=7 → 正常接受
        new_enter = _make_envelope(seq=7, op="upsert", kind="enter_alive")
        d3 = await engine.apply_delta(new_enter)
        assert d3 is DeltaDecision.APPLY_UPSERT
        assert fake_store._rows["AIC-001"].alive is True
