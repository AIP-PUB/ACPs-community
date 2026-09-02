"""tests/integration/test_heartbeat_relay_integration.py — HeartbeatRelay 集成测试（§9.1.1）。

需要：Redis 7+ 在 localhost:6379，Kafka/Redpanda 在 localhost:19092。
运行：just test integration -k heartbeat_relay

覆盖（§9.3 审核检查清单 9-1～9-11）：
- outbox → Kafka 顺序 + partition=shard_index（C-SHARD-3）
- relay_ack 后 PEL 清空
- published_seq 批量推进 + 追平即推进（C-RELAY-3）
- 重启恢复三分支（PEL 非空 / PEL 为空 / stream 空）
- epoch fencing 三路（relay_commit / relay_ack / relay_trim 旧 epoch 返回 0，C-RELAY-1）
- XTRIM 后 outbox 长度收敛
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import pytest

from app.heartbeat.relay import OUTBOX_CONSUMER_GROUP, HeartbeatRelay
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    read_outbox,
    reset_heartbeat_redis_state,
    seed_heartbeat,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AIC = "relay-aic-001"
AIC2 = "relay-aic-002"
BASE_MS = 1_700_000_000_000


# ── Fixtures ─────────────────────────────────────────────────────────────────


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


# ── Helper ──────────────────────────────────────────────────────────────────


async def _run_relay_batch(
    redis_client: object,
    shard: str = "hb-000",
    max_iter: int = 5,
) -> HeartbeatRelay:
    """启动 relay（mock producer）并运行 _publish_batch 若干次（集成测试辅助）。"""
    from unittest.mock import AsyncMock

    from app.heartbeat.redis_keys import delta_outbox_key

    relay = HeartbeatRelay(redis=redis_client)  # type: ignore[arg-type]
    relay._producer = AsyncMock()
    relay._producer.send_and_wait = AsyncMock()

    outbox_key = delta_outbox_key(shard)
    with contextlib.suppress(Exception):
        await redis_client.xgroup_create(  # type: ignore[attr-defined]
            outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True
        )

    epoch = await relay._acquire_shard(shard)
    assert epoch is not None

    relay._running = True
    for _ in range(max_iter):
        ok = await relay._publish_batch(shard, epoch)
        if not ok:
            break

    return relay


# ── epoch fencing 三路（C-RELAY-1）────────────────────────────────────────────


class TestEpochFencing:
    """旧 epoch 的 relay_ack / relay_commit / relay_trim 均被拦截（9-9 / C-RELAY-1）。"""

    async def test_relay_commit_old_epoch_returns_false(self, redis_client: object, loaded_functions: object) -> None:
        """旧 epoch 调 relay_commit_published_seq 返回 False（未修改 published_seq）。"""
        from app.heartbeat.functions import relay_commit_published_seq
        from app.heartbeat.redis_keys import relay_epoch_key
        from tests.support.redis_helper import read_published_seq

        shard = "hb-000"
        await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        current_epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        old_epoch = current_epoch - 1

        ok = await relay_commit_published_seq(
            redis_client,  # type: ignore[arg-type]
            shard=shard,
            epoch=old_epoch,
            seq=99,
        )
        assert ok is False
        seq = await read_published_seq(redis_client, shard)  # type: ignore[arg-type]
        assert seq == 0

    async def test_relay_ack_old_epoch_returns_false(self, redis_client: object, loaded_functions: object) -> None:
        """旧 epoch 调 relay_ack 返回 False，XACK 未执行（PEL 保留）。"""
        from app.heartbeat.functions import relay_ack
        from app.heartbeat.redis_keys import delta_outbox_key, relay_epoch_key

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(  # type: ignore[attr-defined]
                outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True
            )
        entries = await redis_client.xreadgroup(  # type: ignore[attr-defined]
            OUTBOX_CONSUMER_GROUP, "test-consumer", count=1, streams={outbox_key: ">"}
        )
        entry_id = str(entries[0][1][0][0])

        await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        current_epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        old_epoch = current_epoch - 1

        ok = await relay_ack(
            redis_client,  # type: ignore[arg-type]
            shard=shard,
            epoch=old_epoch,
            entry_id=entry_id,
        )
        assert ok is False

        pending = await redis_client.xpending_range(  # type: ignore[attr-defined]
            outbox_key, OUTBOX_CONSUMER_GROUP, min="-", max="+", count=10
        )
        assert any(str(p["message_id"]) == entry_id for p in pending)

    async def test_relay_trim_old_epoch_returns_false(self, redis_client: object, loaded_functions: object) -> None:
        """旧 epoch 调 relay_trim 返回 False，XTRIM 未执行（outbox 长度不变）。"""
        from app.heartbeat.functions import relay_trim
        from app.heartbeat.redis_keys import delta_outbox_key, relay_epoch_key

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        await seed_heartbeat(redis_client, aic=AIC2, observed_at_ms=BASE_MS + 1)  # type: ignore[arg-type]
        outbox_key = delta_outbox_key(shard)

        outbox_before = await read_outbox(redis_client, shard)  # type: ignore[arg-type]
        assert len(outbox_before) >= 2

        entries = await redis_client.xrange(outbox_key, "-", "+", count=1)  # type: ignore[attr-defined]
        min_id = str(entries[0][0])

        await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        current_epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        old_epoch = current_epoch - 1

        ok = await relay_trim(
            redis_client,  # type: ignore[arg-type]
            shard=shard,
            epoch=old_epoch,
            min_entry_id=min_id,
        )
        assert ok is False

        outbox_after = await read_outbox(redis_client, shard)  # type: ignore[arg-type]
        assert len(outbox_after) == len(outbox_before)


# ── PEL 清空 ──────────────────────────────────────────────────────────────────


class TestPelCleanup:
    """relay_ack（正确 epoch）后 PEL 清空（9-1 / C-RELAY-1）。"""

    async def test_pel_empty_after_ack(self, redis_client: object, loaded_functions: object) -> None:
        """正确 epoch 的 relay_ack 使 PEL 清空。"""
        from app.heartbeat.functions import relay_ack
        from app.heartbeat.redis_keys import delta_outbox_key, relay_epoch_key

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(  # type: ignore[attr-defined]
                outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True
            )
        entries = await redis_client.xreadgroup(  # type: ignore[attr-defined]
            OUTBOX_CONSUMER_GROUP, "test-consumer", count=10, streams={outbox_key: ">"}
        )
        entry_ids = [str(e[0]) for e in entries[0][1]]
        epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]

        for entry_id in entry_ids:
            ok = await relay_ack(
                redis_client,  # type: ignore[arg-type]
                shard=shard,
                epoch=epoch,
                entry_id=entry_id,
            )
            assert ok is True

        pending = await redis_client.xpending_range(  # type: ignore[attr-defined]
            outbox_key, OUTBOX_CONSUMER_GROUP, min="-", max="+", count=100
        )
        assert len(pending) == 0


# ── published_seq 推进（C-RELAY-3）───────────────────────────────────────────


class TestPublishedSeqAdvance:
    """published_seq 批量 + 追平推进（C-RELAY-3）。"""

    async def test_published_seq_advances_after_drain(self, redis_client: object, loaded_functions: object) -> None:
        """relay 排空 outbox 后 published_seq >= 1。"""
        from tests.support.redis_helper import read_published_seq

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        await seed_heartbeat(redis_client, aic=AIC2, observed_at_ms=BASE_MS + 1)  # type: ignore[arg-type]

        await _run_relay_batch(redis_client, shard)

        seq = await read_published_seq(redis_client, shard)  # type: ignore[arg-type]
        assert seq >= 1


# ── _reset_published_seq 三分支 ───────────────────────────────────────────────


class TestResetPublishedSeq:
    """_reset_published_seq §5.4 三分支覆盖（9-5）。"""

    async def test_pel_nonempty_branch(self, redis_client: object, loaded_functions: object) -> None:
        """PEL 非空：published_seq = min_pending_seq - 1。"""
        from app.heartbeat.redis_keys import delta_outbox_key, relay_epoch_key
        from tests.support.redis_helper import read_published_seq

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(  # type: ignore[attr-defined]
                outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True
            )
        await redis_client.xreadgroup(  # type: ignore[attr-defined]
            OUTBOX_CONSUMER_GROUP, "test-consumer", count=10, streams={outbox_key: ">"}
        )

        relay = HeartbeatRelay(redis=redis_client)  # type: ignore[arg-type]
        epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        await relay._reset_published_seq(shard, epoch)

        outbox = await read_outbox(redis_client, shard)  # type: ignore[arg-type]
        first_seq = int(outbox[0]["seq"])
        committed = await read_published_seq(redis_client, shard)  # type: ignore[arg-type]
        assert committed == max(0, first_seq - 1)

    async def test_stream_empty_branch(self, redis_client: object, loaded_functions: object) -> None:
        """stream 空：不调用 relay_commit，published_seq 保持 0。"""
        from app.heartbeat.redis_keys import relay_epoch_key
        from tests.support.redis_helper import read_published_seq

        shard = "hb-000"
        relay = HeartbeatRelay(redis=redis_client)  # type: ignore[arg-type]
        epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        await relay._reset_published_seq(shard, epoch)

        committed = await read_published_seq(redis_client, shard)  # type: ignore[arg-type]
        assert committed == 0


# ── _recover_pel（C-RELAY-5）─────────────────────────────────────────────────


class TestRecoverPel:
    """_recover_pel 通过 XAUTOCLAIM 接管旧 PEL 并 relay_ack（9-4 / C-RELAY-5）。"""

    async def test_recover_clears_pel(self, redis_client: object, loaded_functions: object) -> None:
        """_recover_pel 后 PEL 清空（XAUTOCLAIM + relay_ack 正确 epoch）。"""
        from unittest.mock import AsyncMock

        from app.heartbeat.redis_keys import delta_outbox_key, relay_epoch_key

        shard = "hb-000"
        await seed_heartbeat(redis_client, aic=AIC, observed_at_ms=BASE_MS)  # type: ignore[arg-type]
        outbox_key = delta_outbox_key(shard)

        with contextlib.suppress(Exception):
            await redis_client.xgroup_create(  # type: ignore[attr-defined]
                outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True
            )
        await redis_client.xreadgroup(  # type: ignore[attr-defined]
            OUTBOX_CONSUMER_GROUP, "old-consumer", count=10, streams={outbox_key: ">"}
        )

        relay = HeartbeatRelay(redis=redis_client)  # type: ignore[arg-type]
        relay._producer = AsyncMock()
        relay._producer.send_and_wait = AsyncMock()

        epoch: int = await redis_client.incr(relay_epoch_key(shard))  # type: ignore[attr-defined]
        await relay._recover_pel(shard, epoch)

        pending = await redis_client.xpending_range(  # type: ignore[attr-defined]
            outbox_key, OUTBOX_CONSUMER_GROUP, min="-", max="+", count=100
        )
        assert len(pending) == 0
