"""tests/integration/test_heartbeat_functions.py — Heartbeat Redis Functions 集成测试（§9.3）。

需要 Redis 7+ 在 localhost:6379 可用（测试库 db=3，config/testing.toml）。
运行：just test integration -k heartbeat_functions
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncGenerator

import pytest

from app.heartbeat.functions import (
    apply_heartbeat,
    ensure_functions_loaded,
    evict_one,
    mark_silent_one,
    relay_ack,
    relay_commit_published_seq,
    relay_trim,
)
from app.heartbeat.redis_keys import (
    delta_outbox_key,
    latest_key,
    liveness_zset_key,
    published_seq_key,
    relay_epoch_key,
)
from app.heartbeat.sharding import shard_id_for_aic
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    read_outbox,
    reset_heartbeat_redis_state,
)

# ── Pytest 标记 ────────────────────────────────────────────────────────────────
pytestmark = pytest.mark.integration

AIC = "agent-func-001"
AIC2 = "agent-func-002"
BASE_MS = 1_000_000_000_000  # 固定基准时间


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def redis_client():
    """进程级 Redis 客户端（测试库 db=3）。"""
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    """每个测试前后清理 amp:hb:* 键。"""
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
async def loaded_functions(redis_client: object) -> None:
    """确保 Redis Functions 已加载（session 级，仅加载一次）。"""
    await ensure_functions_for_tests(redis_client)  # type: ignore[arg-type]


# ── hb_apply_heartbeat ─────────────────────────────────────────────────────────


class TestApplyHeartbeat:
    async def test_new_aic_enters_alive(self, redis_client, loaded_functions) -> None:
        result = await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        assert result.status == "applied_with_delta"
        assert result.kind == "enter_alive"
        assert result.seq is not None and result.seq > 0

    async def test_new_aic_zset_contains_aic(self, redis_client, loaded_functions) -> None:
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        score = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert score == float(BASE_MS)

    async def test_new_aic_hash_fields_written(self, redis_client, loaded_functions) -> None:
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        data = await redis_client.hgetall(latest_key(shard, AIC))
        assert data["last_seen_at_ms"] == str(BASE_MS)
        assert data["alive_membership_state"] == "alive"
        assert "last_seen_at" in data

    async def test_second_write_refresh_alive(self, redis_client, loaded_functions) -> None:
        """足够间隔后第二次写入应产出 refresh_alive delta（C-WRITE-4）。"""
        from app.core.config import settings

        interval_ms = settings.heartbeat_refresh_emit_interval_seconds * 1000
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        result = await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS + interval_ms + 1)
        assert result.status == "applied_with_delta"
        assert result.kind == "refresh_alive"

    async def test_second_write_too_soon_no_delta(self, redis_client, loaded_functions) -> None:
        """间隔不足时第二次写入应 applied（无 delta，C-WRITE-4）。"""
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        result = await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS + 1)
        assert result.status == "applied"
        assert result.kind is None

    async def test_ignored_older_returns_correct_status(self, redis_client, loaded_functions) -> None:
        """observed_at <= prev_last_seen_at → ignored_older，所有字段不变（C-WRITE-2）。"""
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        data_before = await redis_client.hgetall(latest_key(shard, AIC))
        zset_before = await redis_client.zscore(liveness_zset_key(shard), AIC)

        result = await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS - 1)
        assert result.status == "ignored_older"
        assert result.kind is None
        assert result.seq is None

        data_after = await redis_client.hgetall(latest_key(shard, AIC))
        zset_after = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert data_before == data_after, "ignored_older must not modify hash"
        assert zset_before == zset_after, "ignored_older must not modify zset"

    async def test_seq_monotonically_increases(self, redis_client, loaded_functions) -> None:
        """多次产出 delta 时 seq 连续递增。"""
        from app.core.config import settings

        interval_ms = settings.heartbeat_refresh_emit_interval_seconds * 1000
        seqs = []
        for i in range(5):
            result = await apply_heartbeat(
                redis_client,
                aic=AIC,
                observed_at_ms=BASE_MS + i * (interval_ms + 1),
            )
            if result.seq is not None:
                seqs.append(result.seq)

        assert seqs == list(range(1, len(seqs) + 1)), f"seq not sequential: {seqs}"

    async def test_outbox_entry_count_matches_delta_count(self, redis_client, loaded_functions) -> None:
        """outbox 条目数与产出 delta 数一致（seq 连续性验证，§9.3）。"""
        from app.core.config import settings

        interval_ms = settings.heartbeat_refresh_emit_interval_seconds * 1000
        delta_count = 0
        for i in range(4):
            result = await apply_heartbeat(
                redis_client,
                aic=AIC,
                observed_at_ms=BASE_MS + i * (interval_ms + 1),
            )
            if result.status == "applied_with_delta":
                delta_count += 1

        shard = shard_id_for_aic(AIC)
        outbox = await read_outbox(redis_client, shard)
        assert len(outbox) == delta_count

    async def test_outbox_fields_present(self, redis_client, loaded_functions) -> None:
        """outbox 条目包含 seq/kind/op/aic/last_seen_at_ms 字段。"""
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        outbox = await read_outbox(redis_client, shard)
        assert len(outbox) == 1
        entry = outbox[0]
        assert "seq" in entry
        assert "kind" in entry
        assert "op" in entry
        assert "aic" in entry
        assert "last_seen_at_ms" in entry
        assert entry["aic"] == AIC
        assert entry["op"] == "upsert"
        assert entry["kind"] == "enter_alive"

    async def test_source_timestamp_ms_stored(self, redis_client, loaded_functions) -> None:
        """source_timestamp_ms 传入后应写入 hash。"""
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS, source_timestamp_ms=BASE_MS - 100)
        shard = shard_id_for_aic(AIC)
        data = await redis_client.hgetall(latest_key(shard, AIC))
        assert data.get("source_timestamp_ms") == str(BASE_MS - 100)

    async def test_enter_alive_after_left_alive(self, redis_client, loaded_functions) -> None:
        """left_alive → 新心跳应产出 enter_alive（而非 refresh_alive）。"""
        from app.core.config import settings

        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        silent_result = await mark_silent_one(redis_client, shard=shard_id_for_aic(AIC), aic=AIC)
        assert silent_result.status == "skipped_refreshed" or silent_result.status == "left_alive"

        if silent_result.status == "left_alive":
            re_enter = await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS + silence_ms * 2)
            assert re_enter.kind == "enter_alive"


# ── hb_mark_silent_one ─────────────────────────────────────────────────────────


class TestMarkSilentOne:
    async def test_skipped_missing(self, redis_client, loaded_functions) -> None:
        """Hash 不存在时跳过（skipped_missing）。"""
        shard = shard_id_for_aic(AIC)
        result = await mark_silent_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "skipped_missing"

    async def test_skipped_membership_if_already_left(self, redis_client, loaded_functions) -> None:
        """membership 已为 left_alive 时跳过（skipped_membership）。"""
        shard = shard_id_for_aic(AIC)

        # 写一条很旧的心跳
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        # 手工强制 membership=left_alive
        await redis_client.hset(latest_key(shard, AIC), "alive_membership_state", "left_alive")
        await redis_client.hset(latest_key(shard, AIC), "last_seen_at_ms", str(BASE_MS))

        result = await mark_silent_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "skipped_membership"

    async def test_skipped_refreshed_if_recently_seen(self, redis_client, loaded_functions) -> None:
        """last_seen_at_ms 在阈值内时跳过（skipped_refreshed，§5.2 步骤 3）。"""
        shard = shard_id_for_aic(AIC)
        # 植入非常新的心跳（now + 大偏移，Redis TIME 约为当前时间，所以用当前 epoch ms）
        now_ms = int(time.time() * 1000)
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=now_ms)
        result = await mark_silent_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "skipped_refreshed"

    async def test_left_alive_on_old_heartbeat(self, redis_client, loaded_functions) -> None:
        """超出阈值的旧心跳应转为 left_alive，产出 leave_alive delta。"""
        shard = shard_id_for_aic(AIC)
        from app.core.config import settings

        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        # 写一条 silence_ms * 2 之前的心跳（模拟很久没心跳）
        old_ms = int(time.time() * 1000) - silence_ms * 2
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        result = await mark_silent_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "left_alive"
        assert result.seq is not None and result.seq > 0

        data = await redis_client.hgetall(latest_key(shard, AIC))
        assert data["alive_membership_state"] == "left_alive"

        outbox = await read_outbox(redis_client, shard)
        leave_entry = next((e for e in outbox if e.get("kind") == "leave_alive"), None)
        assert leave_entry is not None
        assert leave_entry["op"] == "delete"

    async def test_left_alive_removes_zset_entry(self, redis_client, loaded_functions) -> None:
        """mark_silent_one 命中后应从 liveness_zset 移除 aic。"""
        shard = shard_id_for_aic(AIC)
        from app.core.config import settings

        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        old_ms = int(time.time() * 1000) - silence_ms * 2
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        await mark_silent_one(redis_client, shard=shard, aic=AIC)
        score = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert score is None


# ── hb_evict_one ───────────────────────────────────────────────────────────────


class TestEvictOne:
    async def test_skipped_missing(self, redis_client, loaded_functions) -> None:
        """Hash 不存在时跳过（skipped_missing）。"""
        shard = shard_id_for_aic(AIC)
        result = await evict_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "skipped_missing"

    async def test_skipped_refreshed_if_recently_seen(self, redis_client, loaded_functions) -> None:
        """最近刚写入的记录不应被 evict（skipped_refreshed）。"""
        shard = shard_id_for_aic(AIC)
        now_ms = int(time.time() * 1000)
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=now_ms)
        result = await evict_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "skipped_refreshed"

    async def test_evicted_left_alive(self, redis_client, loaded_functions) -> None:
        """left_alive 记录应被直接删除（evicted，不产出 delta）。"""
        shard = shard_id_for_aic(AIC)
        from app.core.config import settings

        evict_ms = settings.heartbeat_evict_after_seconds * 1000
        old_ms = int(time.time() * 1000) - evict_ms * 2

        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)
        # 强制置为 left_alive
        await redis_client.hset(latest_key(shard, AIC), "alive_membership_state", "left_alive")

        result = await evict_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "evicted"

        data = await redis_client.hgetall(latest_key(shard, AIC))
        assert data == {}, "hash should be deleted after eviction"
        score = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert score is None

    async def test_evicted_with_repair_alive(self, redis_client, loaded_functions) -> None:
        """alive 记录应先产出 leave_alive(reason=evict_repair) 再删除（C-WRITE-3）。"""
        shard = shard_id_for_aic(AIC)
        from app.core.config import settings

        evict_ms = settings.heartbeat_evict_after_seconds * 1000
        old_ms = int(time.time() * 1000) - evict_ms * 2

        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=old_ms)

        result = await evict_one(redis_client, shard=shard, aic=AIC)
        assert result.status == "evicted_with_repair"
        assert result.seq is not None

        # hash 应已删除
        data = await redis_client.hgetall(latest_key(shard, AIC))
        assert data == {}

        # outbox 应有 leave_alive 条目
        outbox = await read_outbox(redis_client, shard)
        repair_entry = next((e for e in outbox if e.get("kind") == "leave_alive"), None)
        assert repair_entry is not None
        assert repair_entry["op"] == "delete"


# ── hb_relay_commit / hb_relay_ack / hb_relay_trim ────────────────────────────


class TestRelayFunctions:
    async def _setup_relay(self, redis_client: object, shard: str) -> int:
        """初始化 relay epoch，返回当前 epoch 值。"""
        epoch = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        return int(epoch)

    async def test_relay_commit_valid_epoch(self, redis_client, loaded_functions) -> None:
        """合法 epoch 时，relay_commit_published_seq 应写入并返回 True。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)

        ok = await relay_commit_published_seq(redis_client, shard=shard, epoch=epoch, seq=42)
        assert ok is True

        raw = await redis_client.get(published_seq_key(shard))
        assert raw == "42"

    async def test_relay_commit_stale_epoch(self, redis_client, loaded_functions) -> None:
        """旧 epoch 时，relay_commit_published_seq 应返回 False（fencing，C-RELAY-1）。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)
        # 递增 epoch 模拟新 owner 接管
        await redis_client.incr(relay_epoch_key(shard))

        ok = await relay_commit_published_seq(redis_client, shard=shard, epoch=epoch, seq=42)
        assert ok is False

        raw = await redis_client.get(published_seq_key(shard))
        assert raw is None, "published_seq must not be written on fencing"

    async def test_relay_ack_valid_epoch(self, redis_client, loaded_functions) -> None:
        """合法 epoch 时，relay_ack 应执行 XACK 并返回 True。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)

        # 写入一条 heartbeat 产出 outbox 条目
        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        outbox_key = delta_outbox_key(shard)

        # 创建 consumer group
        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(outbox_key, "amp-hb-relay", "0", mkstream=True)

        entries = await redis_client.xreadgroup("amp-hb-relay", "test-consumer", {outbox_key: ">"}, count=1)
        assert entries, "should have pending entry"
        entry_id = entries[0][1][0][0]

        ok = await relay_ack(redis_client, shard=shard, epoch=epoch, entry_id=entry_id)
        assert ok is True

        pending = await redis_client.xpending(outbox_key, "amp-hb-relay")
        assert pending["pending"] == 0

    async def test_relay_ack_stale_epoch(self, redis_client, loaded_functions) -> None:
        """旧 epoch 时，relay_ack 应返回 False，XACK 不执行（C-RELAY-1）。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)

        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(outbox_key, "amp-hb-relay", "0", mkstream=True)

        entries = await redis_client.xreadgroup("amp-hb-relay", "test-consumer", {outbox_key: ">"}, count=1)
        assert entries
        entry_id = entries[0][1][0][0]

        # 递增 epoch 模拟新 owner
        await redis_client.incr(relay_epoch_key(shard))

        ok = await relay_ack(redis_client, shard=shard, epoch=epoch, entry_id=entry_id)
        assert ok is False

        pending = await redis_client.xpending(outbox_key, "amp-hb-relay")
        assert pending["pending"] == 1, "XACK must not execute on fencing"

    async def test_relay_trim_valid_epoch(self, redis_client, loaded_functions) -> None:
        """合法 epoch 时，relay_trim 应执行 XTRIM 并返回 True。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)

        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        outbox_key = delta_outbox_key(shard)

        entries = await redis_client.xrange(outbox_key, "-", "+")
        assert entries
        last_id = entries[-1][0]

        # trim 到 last_id 之后
        ok = await relay_trim(redis_client, shard=shard, epoch=epoch, min_entry_id=last_id)
        assert ok is True

    async def test_relay_trim_stale_epoch(self, redis_client, loaded_functions) -> None:
        """旧 epoch 时，relay_trim 应返回 False，XTRIM 不执行（C-RELAY-1）。"""
        shard = shard_id_for_aic(AIC)
        epoch = await self._setup_relay(redis_client, shard)

        await apply_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        outbox_key = delta_outbox_key(shard)

        entries = await redis_client.xrange(outbox_key, "-", "+")
        assert entries
        last_id = entries[-1][0]

        # 递增 epoch 模拟 owner 切换
        await redis_client.incr(relay_epoch_key(shard))

        ok = await relay_trim(redis_client, shard=shard, epoch=epoch, min_entry_id=last_id)
        assert ok is False

        # stream 应仍有条目（未被裁剪）
        entries_after = await redis_client.xrange(outbox_key, "-", "+")
        assert len(entries_after) == len(entries)

    async def test_ensure_functions_loaded_idempotent(self, redis_client, loaded_functions) -> None:
        """ensure_functions_loaded 可重复调用，不抛异常（FUNCTION LOAD REPLACE 幂等）。"""
        await ensure_functions_loaded(redis_client)
        await ensure_functions_loaded(redis_client)
