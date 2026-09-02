"""tests/integration/test_heartbeat_snapshot_integration.py — SnapshotExporter 集成测试（Step 8 / §9.3）。

需要：Redis 7+ 在 localhost:6379（testing.toml db=3）。
运行：just test integration -k heartbeat_snapshot

覆盖：
- 弱快照不变式：mark_silent 后的 AIC（left_alive）不出现在 snapshot（8-5 membership 过滤）
- snapshot 行 version 字段等于 last_delta_seq（snapshot version=last_delta_seq）
- NDJSON 首行 meta 结构（recordType=snapshot-meta，type=ALIVE_DELTA_TYPE）
- cutover 短窗共享（C-SYNC-3 / P1-3）：share_window>0 时两次连续请求返回同一对象
- 同 score 大组（> chunk_size）不漏成员（C-SYNC-2 tie-safe）
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator

import pytest
from acps_sdk.amp.heartbeat_sync import ALIVE_DELTA_TYPE, seq_to_str

from app.core.config import settings
from app.heartbeat.functions import mark_silent_one
from app.heartbeat.sharding import shard_id_for_aic
from app.heartbeat.snapshot import SnapshotExporter
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    reset_heartbeat_redis_state,
    seed_heartbeat,
)

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
async def loaded_functions(redis_client: object) -> None:
    await ensure_functions_for_tests(redis_client)  # type: ignore[arg-type]


# ── 辅助 ──────────────────────────────────────────────────────────────────────


def _fresh_exporter() -> SnapshotExporter:
    """创建全新 SnapshotExporter 实例（非全局单例，测试隔离）。"""
    return SnapshotExporter()


async def _collect_stream(exporter: SnapshotExporter, redis: object) -> list[dict]:
    """收集 exporter.stream() 输出的全部 NDJSON 行并解析为 dict 列表。"""
    lines = []
    async for raw_line in exporter.stream(redis):  # type: ignore[arg-type]
        stripped = raw_line.strip()
        if stripped:
            lines.append(json.loads(stripped))
    return lines


# ── 弱快照不变式（8-5） ────────────────────────────────────────────────────────


class TestSnapshotMembershipFilter:
    async def test_left_alive_excluded_from_snapshot(self, redis_client, loaded_functions) -> None:
        """mark_silent 转为 left_alive 的 AIC 不出现在 snapshot（弱快照不变式）。"""
        aic = "snap-integ-left-alive-001"
        shard = shard_id_for_aic(aic)
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        old_ms = int(time.time() * 1000) - silence_ms - 1_000  # 在静默窗口内

        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=old_ms)
        # 触发 mark_silent → left_alive + ZREM
        await mark_silent_one(redis_client, shard=shard, aic=aic)

        exporter = _fresh_exporter()
        now_ms = int(time.time() * 1000)
        entries = await exporter._enumerate_shard_alive(
            redis_client,
            shard,
            now_ms=now_ms,
            cutover_seq=0,
        )

        found_aics = {e.payload.aic for e in entries if e.payload}
        assert aic not in found_aics, f"left_alive AIC {aic} 不应出现在 snapshot"

    async def test_alive_aic_appears_in_snapshot(self, redis_client, loaded_functions) -> None:
        """正常 alive 状态的 AIC 出现在 snapshot 中。"""
        aic = "snap-integ-alive-001"
        shard = shard_id_for_aic(aic)
        fresh_ms = int(time.time() * 1000) - 200  # 0.2s 前，alive

        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)

        exporter = _fresh_exporter()
        now_ms = int(time.time() * 1000)
        entries = await exporter._enumerate_shard_alive(
            redis_client,
            shard,
            now_ms=now_ms,
            cutover_seq=0,
        )

        found_aics = {e.payload.aic for e in entries if e.payload}
        assert aic in found_aics, f"alive AIC {aic} 应出现在 snapshot"


# ── snapshot version = last_delta_seq ─────────────────────────────────────────


class TestSnapshotVersion:
    async def test_snapshot_version_matches_last_delta_seq(self, redis_client, loaded_functions) -> None:
        """snapshot 条目 version 字段等于该 AIC 的 last_delta_seq（snapshot version = seq）。"""
        aic = "snap-integ-version-001"
        shard = shard_id_for_aic(aic)
        fresh_ms = int(time.time() * 1000) - 200

        result = await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)
        # seed_heartbeat 调 apply_heartbeat，enter_alive 时 last_delta_seq=1
        # result.seq 即该条目的 outbox delta_seq

        exporter = _fresh_exporter()
        now_ms = int(time.time() * 1000)
        entries = await exporter._enumerate_shard_alive(
            redis_client,
            shard,
            now_ms=now_ms,
            cutover_seq=0,
        )

        found = next((e for e in entries if e.payload and e.payload.aic == aic), None)
        assert found is not None, "AIC 应出现在 snapshot"

        # version 应等于 result.seq（delta_seq），格式化为数字字符串
        # enter_alive 时 seq 必然非 None（outbox 序列号）
        assert result.seq is not None, "enter_alive 结果的 seq 应非 None"
        expected_version = seq_to_str(result.seq)
        assert found.version == expected_version, (
            f"snapshot version={found.version!r} 应等于 last_delta_seq={expected_version!r}"
        )


# ── NDJSON 首行 meta 结构（8-9） ──────────────────────────────────────────────


class TestSnapshotNdjson:
    async def test_first_line_is_snapshot_meta(self, redis_client, loaded_functions) -> None:
        """snapshot stream 首行为 recordType=snapshot-meta 的 AliveSnapshotMeta（8-9）。"""
        aic = "snap-integ-meta-001"
        fresh_ms = int(time.time() * 1000) - 200
        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)

        exporter = _fresh_exporter()
        lines = await _collect_stream(exporter, redis_client)

        assert len(lines) >= 1, "stream 应至少输出首行 meta"
        meta = lines[0]
        assert meta.get("recordType") == "snapshot-meta", "首行 recordType 应为 snapshot-meta"
        assert meta.get("type") == ALIVE_DELTA_TYPE, f"type 应为 {ALIVE_DELTA_TYPE!r}"
        assert "generatedAt" in meta, "meta 应包含 generatedAt 字段"
        assert "cutoverSeqByShard" in meta, "meta 应包含 cutoverSeqByShard 字段"

    async def test_content_lines_are_alive_delta_envelopes(self, redis_client, loaded_functions) -> None:
        """snapshot stream 数据行（非首行）应是 AliveDeltaEnvelope（kind=snapshot）。"""
        aic = "snap-integ-content-001"
        fresh_ms = int(time.time() * 1000) - 200
        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)

        exporter = _fresh_exporter()
        lines = await _collect_stream(exporter, redis_client)

        assert len(lines) >= 2, "stream 应包含 meta + 至少 1 条数据行"
        content = lines[1]
        assert content.get("kind") == "snapshot", "数据行 kind 应为 snapshot"
        assert content.get("op") == "upsert", "数据行 op 应为 upsert"
        assert "payload" in content, "数据行应含 payload"


# ── 短窗共享（C-SYNC-3 / P1-3） ───────────────────────────────────────────────


class TestSnapshotCaching:
    async def test_short_window_sharing_returns_same_object(self, redis_client, loaded_functions, monkeypatch) -> None:
        """share_window > 0 时两次连续 _get_or_materialize 返回同一 MaterializedSnapshot 对象（C-SYNC-3）。"""
        # 覆盖 testing.toml 中的 share_window=0，设为 5s
        monkeypatch.setattr(
            type(settings),
            "heartbeat_snapshot_share_window_seconds",
            property(lambda self: 5),
        )

        aic = "snap-integ-cache-001"
        fresh_ms = int(time.time() * 1000) - 200
        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)

        exporter = _fresh_exporter()  # 新实例，无历史 cache
        snap1 = await exporter._get_or_materialize(redis_client)
        snap2 = await exporter._get_or_materialize(redis_client)

        # 两次调用应返回同一对象（缓存命中，P1-3 验证）
        assert snap1 is snap2, "share_window 内第二次调用应返回缓存对象"
        assert snap1.meta is snap2.meta
        assert snap1.materialized_at_ms == snap2.materialized_at_ms

    async def test_no_caching_when_share_window_zero(self, redis_client, loaded_functions) -> None:
        """share_window=0（testing.toml 默认）时每次请求重新物化（确保测试隔离性）。"""
        aic = "snap-integ-nocache-001"
        fresh_ms = int(time.time() * 1000) - 200
        await seed_heartbeat(redis_client, aic=aic, observed_at_ms=fresh_ms)

        exporter = _fresh_exporter()
        snap1 = await exporter._get_or_materialize(redis_client)
        snap2 = await exporter._get_or_materialize(redis_client)

        # share_window=0 → 每次重新物化，两次对象不同
        assert snap1 is not snap2, "share_window=0 时每次应重新物化"


# ── 大 score 组 tie-safe（C-SYNC-2） ──────────────────────────────────────────


class TestSnapshotTieSafe:
    async def test_large_score_group_no_missing_members(self, redis_client, loaded_functions, monkeypatch) -> None:
        """同 score AIC 数量 > chunk_size 时，tie-safe 补读确保全部成员不漏（C-SYNC-2）。

        测试策略：
        - 将 chunk_size 缩小为 3（monkeypatch）
        - Seed 5 个 AIC，全部使用相同 observed_at_ms（同 score）
        - 断言 snapshot 中 5 个 AIC 均出现
        """
        # 将 chunk_size 缩小，强制 tie-safe 路径
        monkeypatch.setattr(
            type(settings),
            "heartbeat_snapshot_chunk_size",
            property(lambda self: 3),
        )

        base_ms = int(time.time() * 1000) - 200  # 0.2s 前，alive
        aics = [f"snap-tie-aic-{i:02d}" for i in range(5)]
        shard = shard_id_for_aic(aics[0])

        # 所有 AIC 用同一 observed_at_ms → 在 ZSet 中有相同 score
        for aic in aics:
            await seed_heartbeat(redis_client, aic=aic, observed_at_ms=base_ms)

        exporter = _fresh_exporter()
        now_ms = int(time.time() * 1000)
        entries = await exporter._enumerate_shard_alive(
            redis_client,
            shard,
            now_ms=now_ms,
            cutover_seq=0,
        )

        found_aics = {e.payload.aic for e in entries if e.payload}
        for aic in aics:
            assert aic in found_aics, f"AIC {aic} 不应被 chunk_size 截断（C-SYNC-2 tie-safe）"

    async def test_expired_aics_excluded_from_enumeration(self, redis_client, loaded_functions) -> None:
        """score < silence_threshold 的 AIC（已静默）不应出现在 snapshot 枚举结果中。

        注意：枚举下界 = now_ms - silence_threshold_ms，超过该值的 AIC 在 ZSet 中不被选取。
        """
        aic_fresh = "snap-integ-fresh-filter-001"
        aic_stale = "snap-integ-stale-filter-001"
        shard = shard_id_for_aic(aic_fresh)

        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        now_ms = int(time.time() * 1000)
        fresh_ts = now_ms - 200  # alive，在枚举窗口内
        stale_ts = now_ms - silence_ms - 2_000  # 已超 silence 阈值

        await seed_heartbeat(redis_client, aic=aic_fresh, observed_at_ms=fresh_ts)
        await seed_heartbeat(redis_client, aic=aic_stale, observed_at_ms=stale_ts)

        exporter = _fresh_exporter()
        entries = await exporter._enumerate_shard_alive(
            redis_client,
            shard,
            now_ms=now_ms,
            cutover_seq=0,
        )

        found_aics = {e.payload.aic for e in entries if e.payload}
        assert aic_fresh in found_aics, "fresh AIC 应出现在 snapshot"
        assert aic_stale not in found_aics, "stale AIC 不应出现在 snapshot（超过 silence 阈值）"
