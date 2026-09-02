"""tests/integration/message/test_message_dedupe.py — Message Redis 去重/水位集成测试（C-2）。

测试真实 Redis 下 dedupe.py / freshness.py 的行为。
需要真实 Redis（dev-infra redis 服务，db=1）。
"""

from __future__ import annotations

import time

import pytest
from redis.asyncio import Redis

from tests.support.constants import TEST_REDIS_URL
from tests.support.redis_helper import reset_message_redis_state

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_message():
    """提供连接测试库 db=1 的 Redis 客户端；前后清理 amp:message:* 键。"""
    r = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as exc:
        await r.aclose()
        pytest.skip(f"Redis 不可达，跳过集成测试：{exc}")

    await reset_message_redis_state(r)
    yield r
    await reset_message_redis_state(r)
    await r.aclose()


class TestMessageDedupe:
    async def test_unseen_before_mark(self, redis_message: Redis) -> None:
        from app.message.dedupe import filter_unseen

        log_id = "msg-dedup-int-001"
        unseen, available = await filter_unseen(redis_message, [log_id])
        assert available is True
        assert log_id in unseen

    async def test_seen_after_mark(self, redis_message: Redis) -> None:
        from app.message.dedupe import filter_unseen, mark_seen

        log_id = "msg-dedup-int-002"
        await mark_seen(redis_message, [log_id], ttl_seconds=3600)
        unseen, available = await filter_unseen(redis_message, [log_id])
        assert available is True
        assert log_id not in unseen

    async def test_redis_failure_is_fail_open(self) -> None:
        from app.message.dedupe import filter_unseen

        bad_redis = Redis.from_url("redis://localhost:19999/99", decode_responses=True)
        try:
            unseen, available = await filter_unseen(bad_redis, ["any-id"])
            assert available is False
            assert "any-id" in unseen
        finally:
            await bad_redis.aclose()


class TestMessageFreshness:
    async def test_advance_and_read_events_watermark(self, redis_message: Redis) -> None:
        from app.message.freshness import advance_partition_watermark, read_events_watermark

        now_ms = int(time.time() * 1000)
        await advance_partition_watermark(
            redis_message,
            partition_id=0,
            batch_max_ts_ms=now_ms,
            now_ms=now_ms,
        )
        wm = await read_events_watermark(redis_message)
        assert wm == now_ms

    async def test_compaction_watermark_lifecycle(self, redis_message: Redis) -> None:
        from app.message.freshness import read_compaction_watermark, set_compaction_watermark

        ts = int(time.time() * 1000)
        await set_compaction_watermark(redis_message, kind="lifecycle", watermark_ms=ts)
        wm = await read_compaction_watermark(redis_message, kind="lifecycle")
        assert wm == ts

    async def test_state_watermark(self, redis_message: Redis) -> None:
        from app.message.freshness import read_state_watermark, set_state_watermark

        ts = int(time.time() * 1000)
        await set_state_watermark(redis_message, captured_at_ms=ts)
        wm = await read_state_watermark(redis_message)
        assert wm == ts
