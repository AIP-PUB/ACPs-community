"""AMP alive-delta Kafka Consumer（aiokafka 封装）。

使用 subscribe（消费组）+ ConsumerRebalanceListener 显式 seek，
位点真相源是本地 PG checkpoint 的 kafka_next_offset（enable_auto_commit=False）。
不手动 assign()，以支持多实例自动分区均衡。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from acps_sdk.amp.alive_sync.bootstrap import next_lookback_seconds, seek_timestamp_ms
from acps_sdk.amp.alive_sync.errors import ResyncRequired
from acps_sdk.amp.heartbeat_sync import AliveDeltaEnvelope, shard_index_from_id
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.abc import ConsumerRebalanceListener
from aiokafka.errors import OffsetOutOfRangeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from acps_sdk.amp.alive_sync.engine import AliveSyncEngine

logger = logging.getLogger(__name__)


class _SeekOnAssign(ConsumerRebalanceListener):  # type: ignore[misc]
    """ConsumerRebalanceListener：rebalance 后对新分配分区执行显式 seek。

    续跑：有 kafka_next_offset → seek 到该 offset。
    自举：无 offset → offsetsForTimes(snapshot 生成时间 - 回看裕量)，
         失败（时间戳无匹配 offset）则 seek_to_beginning。
    禁止 latest：任何情况下均不 seek_to_end，保证不漏事件（C-SYNC-6）。
    """

    def __init__(
        self,
        consumer: AliveDeltaKafkaConsumer,
    ) -> None:
        self._c = consumer

    async def on_partitions_revoked(self, revoked: set[TopicPartition]) -> None:
        logger.debug("分区撤销: %s", revoked)

    async def on_partitions_assigned(self, assigned: set[TopicPartition]) -> None:
        logger.debug("分区指派: %s", assigned)
        plan = self._c._seek_plan
        if plan is None:
            logger.warning("_on_partitions_assigned: seek plan 未设置，跳过 seek")
            return
        if self._c._consumer is None:
            logger.warning("_on_partitions_assigned: consumer 未启动，跳过 seek")
            return

        _cutover_by_shard = plan["cutover_by_shard"]
        generated_at = plan["generated_at"]
        lookback_seconds = plan["lookback_seconds"]
        checkpoints_by_shard = plan["checkpoints_by_shard"]  # shard -> kafka_next_offset

        for tp in assigned:
            shard_idx = tp.partition
            # 根据 partition index 推算 shard key
            shard_key = f"hb-{shard_idx:03d}"

            checkpoint_offset = checkpoints_by_shard.get(shard_key)
            if checkpoint_offset is not None:
                # 续跑：按持久化 offset seek
                logger.info("shard %s 续跑，seek to offset %d", shard_key, checkpoint_offset)
                self._c._consumer.seek(tp, checkpoint_offset)
            else:
                # 自举：按时间 seek
                ts_ms = seek_timestamp_ms(generated_at, lookback_seconds)
                logger.info("shard %s 自举，offsetsForTimes ts=%d", shard_key, ts_ms)
                result = await self._c._consumer.offsets_for_times({tp: ts_ms})
                offset_and_ts = result.get(tp)
                if offset_and_ts is not None and offset_and_ts.offset >= 0:
                    self._c._consumer.seek(tp, offset_and_ts.offset)
                else:
                    logger.warning("shard %s 无时间戳匹配 offset，seek_to_beginning", shard_key)
                    await self._c._consumer.seek_to_beginning(tp)


class AliveDeltaKafkaConsumer:
    """aiokafka alive-delta 消费器（消费组 + rebalance listener 显式 seek）。"""

    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        topic: str,
        max_lookback_seconds: int = 86400,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._topic = topic
        self._max_lookback_seconds = max_lookback_seconds
        self._seek_plan: dict[str, Any] | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._listener = _SeekOnAssign(self)

    def set_topic(self, topic: str) -> None:
        """设置订阅主题（须在 start() 之前调用）。"""
        if self._consumer is not None:
            raise RuntimeError("set_topic 须在 start() 之前调用")
        self._topic = topic

    def set_seek_plan(
        self,
        cutover_by_shard: dict[str, int],
        generated_at: str,
        lookback_seconds: int,
        checkpoints_by_shard: dict[str, int | None],
    ) -> None:
        """存储 rebalance 时 seek 所需输入。在 consumer.start() 之前或之后均可调用。"""
        self._seek_plan = {
            "cutover_by_shard": cutover_by_shard,
            "generated_at": generated_at,
            "lookback_seconds": lookback_seconds,
            "checkpoints_by_shard": checkpoints_by_shard,
        }

    async def start(self) -> None:
        """启动 consumer，订阅 topic。"""
        if not self._topic.strip():
            raise RuntimeError("Kafka topic 未配置，须在 start() 前通过 set_topic 或构造参数指定")
        self._consumer = AIOKafkaConsumer(
            enable_auto_commit=False,
            auto_offset_reset="none",
            group_id=self._group_id,
            bootstrap_servers=self._bootstrap_servers,
        )
        self._consumer.subscribe(
            topics=[self._topic],
            listener=self._listener,
        )
        await self._consumer.start()

    async def poll_apply(
        self,
        engine: AliveSyncEngine,
        on_gap: Callable[[str], Awaitable[None]],
    ) -> None:
        """持续消费 delta 消息，应用到 engine。

        - GapDetectedError → await on_gap(shard)（不在消费循环里自行恢复）
        - OffsetOutOfRangeError → 上抛 ResyncRequired（续跑时 offset 被 retention 淘汰）
        - asyncio.CancelledError → 透传（后台任务标准退出机制）
        """
        if self._consumer is None:
            raise RuntimeError("poll_apply 前必须先调用 start()")
        try:
            async for msg in self._consumer:
                try:
                    env = AliveDeltaEnvelope.model_validate_json(msg.value)
                    await engine.apply_delta(env, kafka_next_offset=msg.offset + 1)
                except Exception as exc:
                    from acps_sdk.amp.alive_sync.errors import GapDetectedError

                    if isinstance(exc, GapDetectedError):
                        logger.warning("检测到缺口 shard=%s: %s", exc.shard, exc)
                        await on_gap(exc.shard)
                        return  # 返回后 service 会 request_resync
                    raise
        except OffsetOutOfRangeError as exc:
            raise ResyncRequired(reason=f"kafka_offset_out_of_range: {exc}") from exc
        except asyncio.CancelledError:
            raise

    async def reseek_with_backoff(
        self,
        shard: str,
        current_lookback: int,
    ) -> int:
        """自举免误报：对指定 shard 的分区倍增回看并重 seek。

        返回新的 lookback_seconds（已更新到 seek plan 中）。
        达顶（== max_lookback_seconds）则 seek_to_beginning 并返回 max。
        """
        new_lookback = next_lookback_seconds(current_lookback, max_seconds=self._max_lookback_seconds)
        plan = self._seek_plan
        if plan is None:
            return new_lookback

        if self._consumer is None:
            raise RuntimeError("reseek_with_backoff 前必须先调用 start()")
        generated_at = plan["generated_at"]
        shard_idx = shard_index_from_id(shard)
        tp = TopicPartition(self._topic, shard_idx)

        if new_lookback >= self._max_lookback_seconds:
            logger.warning("shard %s 回看达顶，seek_to_beginning", shard)
            await self._consumer.seek_to_beginning(tp)
        else:
            ts_ms = seek_timestamp_ms(generated_at, new_lookback)
            result = await self._consumer.offsets_for_times({tp: ts_ms})
            offset_and_ts = result.get(tp)
            if offset_and_ts is not None and offset_and_ts.offset >= 0:
                self._consumer.seek(tp, offset_and_ts.offset)
            else:
                await self._consumer.seek_to_beginning(tp)

        # 更新 seek plan
        plan["lookback_seconds"] = new_lookback
        return new_lookback

    async def stop(self) -> None:
        """停止 consumer。"""
        if self._consumer is None:
            return
        try:
            await self._consumer.stop()
        except Exception as exc:
            logger.warning("停止 Kafka consumer 时出错（忽略）: %s", exc)
