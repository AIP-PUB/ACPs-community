"""tests/integration/test_heartbeat_writer_integration.py — HeartbeatWriter 集成测试（Step 5 / §9.3）。

需要：Redis 7+ 在 localhost:6379（testing.toml db=3）；Kafka 不需要（直接调 handle_message 注入消息）。
运行：just test integration -k heartbeat_writer

覆盖：
- 投递心跳消息 → Redis 当前态正确（alive_membership_state / last_seen_at_ms / liveness_zset）
- 重复投递幂等（相同 observed_at_ms → ignored_older，zset/hash 不变）
- 非 heartbeat logType → 跳过，Redis 无变化
- 无 observedAt（LogAppendTime 缺失且无 observedTimestamp）→ UntimedHeartbeatError（DLQ 路径触发）
- watermark flush 后 Redis WATERMARKS_KEY 有对应分区条目
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from app.heartbeat.exception import UntimedHeartbeatError
from app.heartbeat.redis_keys import WATERMARKS_KEY, liveness_zset_key
from app.heartbeat.sharding import shard_id_for_aic
from app.heartbeat.store import get_latest, read_watermarks
from app.heartbeat.writer import HeartbeatWriter
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    reset_heartbeat_redis_state,
)

pytestmark = pytest.mark.integration

AIC = "writer-integ-aic-001"
BASE_MS = 1_700_000_000_000


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


# ── 辅助：构造模拟 Kafka ConsumerRecord ───────────────────────────────────────


def _mock_msg(
    aic: str,
    *,
    timestamp_type: int = 1,
    timestamp: int | None = None,
    observed_timestamp: str | None = None,
    log_type: str = "heartbeat",
    partition: int = 0,
) -> MagicMock:
    """构造模拟 aiokafka ConsumerRecord。

    Args:
        aic: Agent Identity Code。
        timestamp_type: 0=CreateTime，1=LogAppendTime（默认）。
        timestamp: broker 时间戳（ms），None 时取当前毫秒时间。
        observed_timestamp: LogRecord.observedTimestamp ISO 字符串（可选）。
        log_type: logType 字段，默认 "heartbeat"。
        partition: Kafka 分区编号，默认 0。
    """
    msg = MagicMock()
    msg.timestamp_type = timestamp_type
    msg.timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
    msg.partition = partition
    msg.offset = 0
    body: dict[str, str] = {"logType": log_type, "aic": aic}
    if observed_timestamp is not None:
        body["observedTimestamp"] = observed_timestamp
    msg.value = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return msg


# ── 测试用例 ──────────────────────────────────────────────────────────────────


class TestWriterHandleMessage:
    async def test_heartbeat_sets_redis_state(self, redis_client, loaded_functions) -> None:
        """投递合法心跳消息 → Redis hash 字段与 liveness_zset score 正确（§3.1 step 3）。"""
        writer = HeartbeatWriter(redis=redis_client)
        now_ms = int(time.time() * 1000)
        msg = _mock_msg(AIC, timestamp=now_ms)
        await writer.handle_message(msg)

        shard = shard_id_for_aic(AIC)
        entry = await get_latest(redis_client, shard, AIC)
        assert entry is not None, "Redis hash 应存在 latest 记录"
        assert entry.aic == AIC
        assert entry.alive_membership_state == "alive"
        assert abs(entry.last_seen_at_ms - now_ms) < 5_000  # broker 时间戳近似当前时间

        # liveness_zset 应有 AIC（score = observed_at_ms = now_ms）
        score = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert score is not None
        assert int(score) == now_ms

        assert writer._accepted >= 1

    async def test_duplicate_heartbeat_idempotent(self, redis_client, loaded_functions) -> None:
        """相同 observed_at_ms 重复投递 → ignored_older，zset score 与 hash 不变（C-WRITE-2）。"""
        writer = HeartbeatWriter(redis=redis_client)
        ts = int(time.time() * 1000) - 500  # 0.5s 前

        # 第一次：enter_alive
        msg1 = _mock_msg(AIC, timestamp=ts)
        await writer.handle_message(msg1)

        shard = shard_id_for_aic(AIC)
        score_before = await redis_client.zscore(liveness_zset_key(shard), AIC)

        # 第二次：相同 timestamp → ignored_older
        msg2 = _mock_msg(AIC, timestamp=ts)
        await writer.handle_message(msg2)

        score_after = await redis_client.zscore(liveness_zset_key(shard), AIC)
        assert score_before == score_after, "ignored_older 不应改变 zset score"
        assert writer._ignored_older >= 1

    async def test_older_heartbeat_does_not_revert_state(self, redis_client, loaded_functions) -> None:
        """旧时间戳心跳不回退最新状态（C-WRITE-2 零副作用）。"""
        writer = HeartbeatWriter(redis=redis_client)
        now_ms = int(time.time() * 1000)
        older_ms = now_ms - 5_000  # 5s 前

        # 先写新的
        await writer.handle_message(_mock_msg(AIC, timestamp=now_ms))
        shard = shard_id_for_aic(AIC)
        score_after_new = await redis_client.zscore(liveness_zset_key(shard), AIC)

        # 再写旧的
        await writer.handle_message(_mock_msg(AIC, timestamp=older_ms))
        score_final = await redis_client.zscore(liveness_zset_key(shard), AIC)

        assert score_after_new == score_final, "旧时间戳不应回退 zset score"

    async def test_non_heartbeat_logtype_skipped(self, redis_client, loaded_functions) -> None:
        """非 heartbeat logType 消息 → 跳过处理，Redis 无任何 latest key 写入。"""
        writer = HeartbeatWriter(redis=redis_client)
        msg = _mock_msg(AIC, log_type="audit")
        await writer.handle_message(msg)

        shard = shard_id_for_aic(AIC)
        entry = await get_latest(redis_client, shard, AIC)
        assert entry is None, "非 heartbeat 消息不应写入 Redis"
        assert writer._accepted == 0

    async def test_untimed_heartbeat_raises_error(self, redis_client, loaded_functions) -> None:
        """timestamp_type=0 且无 observedTimestamp → UntimedHeartbeatError（DLQ 路径）。"""
        writer = HeartbeatWriter(redis=redis_client)
        # timestamp_type=0（CreateTime），无 observedTimestamp → observed_at_ms = None
        msg = _mock_msg(AIC, timestamp_type=0, observed_timestamp=None)

        with pytest.raises(UntimedHeartbeatError):
            await writer.handle_message(msg)

    async def test_observedtimestamp_fallback_when_no_logappendtime(self, redis_client, loaded_functions) -> None:
        """timestamp_type=0 但 observedTimestamp 存在 → 用 observedTimestamp 作为 observed_at_ms。"""
        writer = HeartbeatWriter(redis=redis_client)
        obs_ts = "2024-01-01T00:00:00+00:00"  # 固定时间，不影响测试
        msg = _mock_msg(AIC, timestamp_type=0, observed_timestamp=obs_ts)
        await writer.handle_message(msg)

        shard = shard_id_for_aic(AIC)
        entry = await get_latest(redis_client, shard, AIC)
        assert entry is not None, "observedTimestamp 回退路径应成功写入 Redis"
        assert entry.alive_membership_state == "alive"


class TestWriterWatermark:
    async def test_watermark_flush_writes_to_redis(self, redis_client, loaded_functions) -> None:
        """handle_message + _flush_watermarks → WATERMARKS_KEY hash 有对应分区条目（§6.2.1）。"""
        writer = HeartbeatWriter(redis=redis_client)
        partition = 0
        ts = int(time.time() * 1000)
        msg = _mock_msg(AIC, timestamp=ts, partition=partition)
        await writer.handle_message(msg)

        # 强制 flush（间隔检查可能未触发）
        await writer._flush_watermarks()

        watermarks = await read_watermarks(redis_client)
        assert partition in watermarks, f"分区 {partition} 水位应写入 Redis"
        watermark_ms, updated_at_ms = watermarks[partition]
        assert watermark_ms == ts, "水位值应等于消息 observed_at_ms"
        assert updated_at_ms > 0

    async def test_watermark_not_written_without_messages(self, redis_client, loaded_functions) -> None:
        """无消息时 _flush_watermarks 不写空 HSET（幂等）。"""
        writer = HeartbeatWriter(redis=redis_client)
        await writer._flush_watermarks()  # 空水位，不应写入

        raw = await redis_client.hgetall(WATERMARKS_KEY)
        assert raw == {}, "无消息时 WATERMARKS_KEY 应为空"

    async def test_watermark_monotonic_advance(self, redis_client, loaded_functions) -> None:
        """水位单调递增：旧消息不回退分区水位。"""
        writer = HeartbeatWriter(redis=redis_client)
        now_ms = int(time.time() * 1000)
        older_ms = now_ms - 3_000

        # 先处理新消息
        await writer.handle_message(_mock_msg(AIC, timestamp=now_ms, partition=0))
        await writer._flush_watermarks()
        wm_after_new = (await read_watermarks(redis_client))[0][0]

        # 再处理旧消息（相同分区）
        aic2 = "writer-integ-aic-002"
        await writer.handle_message(_mock_msg(aic2, timestamp=older_ms, partition=0))
        await writer._flush_watermarks()
        wm_final = (await read_watermarks(redis_client))[0][0]

        # 水位应保持 now_ms（不被 older_ms 回退）
        assert wm_final == wm_after_new, "旧消息不应回退分区水位"
