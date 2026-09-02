"""tests/integration/test_heartbeat_store_integration.py — store.py 集成测试（§9.3）。

需要 Redis 7+ 在 localhost:6379 可用（测试库 db=3）。
运行：just test integration -k heartbeat_store
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncGenerator

import pytest

from app.heartbeat.redis_keys import (
    delta_outbox_key,
    published_seq_key,
)
from app.heartbeat.sharding import shard_id_for_aic
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    reset_heartbeat_redis_state,
    seed_heartbeat,
)

pytestmark = pytest.mark.integration

AIC = "agent-store-001"
AIC2 = "agent-store-002"
AIC3 = "agent-store-003"
BASE_MS = 1_000_000_000_000


# ── Fixtures ───────────────────────────────────────────────────────────────────


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


# ── store.get_latest / store.mget_latest ──────────────────────────────────────


class TestGetLatest:
    async def test_returns_none_for_missing_aic(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        shard = shard_id_for_aic(AIC)
        result = await store.get_latest(redis_client, shard, AIC)
        assert result is None

    async def test_returns_entry_after_apply(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        entry = await store.get_latest(redis_client, shard, AIC)
        assert entry is not None
        assert entry.aic == AIC
        assert entry.last_seen_at_ms == BASE_MS
        assert entry.alive_membership_state == "alive"

    async def test_mget_latest_preserves_order(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        # AIC 和 AIC2 可能在不同 shard，但 mget_latest 是同 shard 的多个 aic
        shard = shard_id_for_aic(AIC)
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        # AIC3 可能在不同 shard，只测同 shard 的
        results = await store.mget_latest(redis_client, shard, [AIC, "nonexistent-aic"])
        assert len(results) == 2
        assert results[0] is not None
        assert results[0].aic == AIC
        assert results[1] is None


# ── store.zcard / store.zcount_score_at_least ─────────────────────────────────


class TestZcardAndZcount:
    async def test_zcard_empty_shard(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        shard = shard_id_for_aic(AIC)
        count = await store.zcard(redis_client, shard)
        assert count == 0

    async def test_zcard_after_insert(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(AIC)
        count = await store.zcard(redis_client, shard)
        assert count == 1

    async def test_zcount_score_at_least(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=now_ms)
        shard = shard_id_for_aic(AIC)

        count_include = await store.zcount_score_at_least(redis_client, shard, min_score_ms=now_ms)
        count_exclude = await store.zcount_score_at_least(redis_client, shard, min_score_ms=now_ms + 1)
        assert count_include == 1
        assert count_exclude == 0


# ── store.zrange_by_score / store.zrevrange_by_score ─────────────────────────


class TestZrangeByScore:
    async def test_zrange_ascending(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store
        from app.heartbeat.functions import apply_heartbeat

        # 写多个不同 AIC（同 shard 内）
        aics_to_insert = []
        shard = None
        for i in range(10):
            test_aic = f"zrange-store-{i:03d}"
            await apply_heartbeat(redis_client, aic=test_aic, observed_at_ms=BASE_MS + i * 1000)
            s = shard_id_for_aic(test_aic)
            if shard is None:
                shard = s
            if s == shard:
                aics_to_insert.append((test_aic, BASE_MS + i * 1000))

        if not aics_to_insert or shard is None:
            pytest.skip("all AICs in different shards, skip this test")

        results = await store.zrange_by_score(redis_client, shard, lower="-inf", upper="+inf", limit=100)
        scores = [score for _, score in results]
        assert scores == sorted(scores), "zrange_by_score should be ascending"

    async def test_zrevrange_is_reverse_of_zrange(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store
        from app.heartbeat.functions import apply_heartbeat

        shard = None
        for i in range(5):
            test_aic = f"zrev-store-{i:03d}"
            await apply_heartbeat(redis_client, aic=test_aic, observed_at_ms=BASE_MS + i * 100)
            s = shard_id_for_aic(test_aic)
            if shard is None:
                shard = s

        if shard is None:
            pytest.skip("no shard found")

        fwd = await store.zrange_by_score(redis_client, shard, lower="-inf", upper="+inf", limit=100)
        rev = await store.zrevrange_by_score(redis_client, shard, lower="-inf", upper="+inf", limit=100)
        assert list(reversed(fwd)) == rev, "zrevrange should be reverse of zrange"

    async def test_zrange_exclusive_lower(self, redis_client, loaded_functions) -> None:
        """排他下界 '(N' 应排除等于 N 的条目。"""
        from app.heartbeat import store
        from app.heartbeat.functions import apply_heartbeat

        test_aic = "zrange-excl-001"
        await apply_heartbeat(redis_client, aic=test_aic, observed_at_ms=BASE_MS)
        shard = shard_id_for_aic(test_aic)

        # 含界下界：包含 BASE_MS
        inclusive = await store.zrange_by_score(redis_client, shard, lower=str(BASE_MS), upper="+inf", limit=10)
        # 排他下界：排除 BASE_MS
        exclusive = await store.zrange_by_score(redis_client, shard, lower=f"({BASE_MS}", upper="+inf", limit=10)
        assert len(inclusive) == 1
        assert len(exclusive) == 0


# ── store.read_watermarks / store.write_watermarks ───────────────────────────


class TestWatermarks:
    async def test_read_empty_watermarks(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        result = await store.read_watermarks(redis_client)
        assert result == {}

    async def test_write_and_read_watermarks(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        entries = {0: (BASE_MS, BASE_MS + 1000), 1: (BASE_MS + 500, BASE_MS + 1500)}
        await store.write_watermarks(redis_client, entries)
        result = await store.read_watermarks(redis_client)
        assert result == entries

    async def test_write_watermarks_roundtrip(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        entries = {3: (999999999, 1000000000)}
        await store.write_watermarks(redis_client, entries)
        result = await store.read_watermarks(redis_client)
        assert result[3] == (999999999, 1000000000)


# ── store.outbox_publish_lag_ms ───────────────────────────────────────────────


class TestOutboxPublishLag:
    async def test_returns_none_when_outbox_empty(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        shard = shard_id_for_aic(AIC)
        lag = await store.outbox_publish_lag_ms(redis_client, shard)
        assert lag is None

    async def test_returns_lag_from_undelivered_entries(self, redis_client, loaded_functions) -> None:
        """relay 完全停摆时（未投递积压），应检出 lag（覆盖 P1-2 修复路径②）。"""
        from app.heartbeat import store

        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=now_ms)
        shard = shard_id_for_aic(AIC)
        outbox_key = delta_outbox_key(shard)

        # 创建 consumer group（simulate relay 存在但未消费）
        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(outbox_key, "amp-hb-relay", "0", mkstream=True)

        lag = await store.outbox_publish_lag_ms(redis_client, shard)
        assert lag is not None
        assert lag >= 0

    async def test_returns_lag_from_pel(self, redis_client, loaded_functions) -> None:
        """PEL 积压时应检出 lag（覆盖 P1-2 修复路径①）。"""
        from app.heartbeat import store

        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=now_ms)
        shard = shard_id_for_aic(AIC)
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(outbox_key, "amp-hb-relay", "0", mkstream=True)

        # 消费但不 XACK，模拟 PEL 积压
        await redis_client.xreadgroup("amp-hb-relay", "test-consumer", {outbox_key: ">"}, count=10)

        lag = await store.outbox_publish_lag_ms(redis_client, shard)
        assert lag is not None
        assert lag >= 0


# ── store.read_published_seq / read_all_published_seq ─────────────────────────


class TestReadPublishedSeq:
    async def test_returns_zero_when_missing(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        shard = shard_id_for_aic(AIC)
        seq = await store.read_published_seq(redis_client, shard)
        assert seq == 0

    async def test_returns_set_value(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store

        shard = shard_id_for_aic(AIC)
        await redis_client.set(published_seq_key(shard), "42")
        seq = await store.read_published_seq(redis_client, shard)
        assert seq == 42

    async def test_read_all_published_seq(self, redis_client, loaded_functions) -> None:
        from app.heartbeat import store
        from app.heartbeat.sharding import all_shard_ids

        shards = all_shard_ids()
        for i, shard in enumerate(shards):
            await redis_client.set(published_seq_key(shard), str(i + 1))

        result = await store.read_all_published_seq(redis_client)
        for i, shard in enumerate(shards):
            assert result[shard] == i + 1
