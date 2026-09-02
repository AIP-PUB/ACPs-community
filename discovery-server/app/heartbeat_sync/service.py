"""AliveSyncService — alive-sync 三阶段主循环编排（bootstrap / delta 消费 / resync）。

组合 AliveSyncSourceClient、AliveDeltaKafkaConsumer、AliveSyncEngine（SDK）。
对外暴露 run() 入口，runtime.py 通过 asyncio.create_task(service.run()) 托管。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from acps_sdk.amp.alive_sync.errors import AliveSyncError as _EngineError
from acps_sdk.amp.alive_sync.errors import ResyncRequired

from app.heartbeat_sync.exception import AliveSyncError

if TYPE_CHECKING:
    from acps_sdk.amp.alive_sync.engine import AliveSyncEngine

    from app.core.config import Settings
    from app.heartbeat_sync.kafka_consumer import AliveDeltaKafkaConsumer
    from app.heartbeat_sync.source_client import AliveSyncSourceClient
    from app.heartbeat_sync.store import PostgresAliveSyncStore

logger = logging.getLogger(__name__)


class AliveSyncService:
    """alive-sync Consumer 三阶段主循环。

    - bootstrap：/sync/info + /sync/snapshot → engine.apply_snapshot
    - run：resume_or_bootstrap → kafka poll_apply
    - resync：store.reset → sleep(backoff) → bootstrap
    """

    def __init__(
        self,
        settings: Settings,
        store: PostgresAliveSyncStore,
        source_client: AliveSyncSourceClient,
        kafka_consumer: AliveDeltaKafkaConsumer,
        engine: AliveSyncEngine,
    ) -> None:
        self._settings = settings
        self._store = store
        self._source_client = source_client
        self._kafka_consumer = kafka_consumer
        self._engine = engine

    async def bootstrap(self) -> None:
        """① 拉取 /sync/info → ② stream_snapshot + apply_snapshot → ③ 设置 Kafka seek plan。"""
        logger.info("alive-sync bootstrap 开始")
        await self._source_client.fetch_sync_info()  # 验证 Provider 在线（结果不使用）

        async with self._source_client.stream_snapshot() as (meta, rows):
            await self._engine.apply_snapshot(meta, rows)

        cutover_by_shard = self._engine.cutover_seq_by_shard()
        self._kafka_consumer.set_seek_plan(
            cutover_by_shard=cutover_by_shard,
            generated_at=meta.generated_at,
            lookback_seconds=self._settings.ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS,
            checkpoints_by_shard={},
        )
        logger.info("alive-sync bootstrap 完成，cutover=%s", cutover_by_shard)

    async def resume_or_bootstrap(self) -> None:
        """优先续跑，无 checkpoint 或 hydrate 失败则 bootstrap。"""
        cps = await self._store.load_checkpoints()
        if cps:
            try:
                await self._engine.hydrate()
                cp_by_shard: dict[str, int | None] = {
                    cp.shard: cp.kafka_next_offset for cp in cps if cp.kafka_next_offset is not None
                }
                # 读取 cutover（从 checkpoint 恢复）
                cutover_by_shard = {cp.shard: cp.cutover_seq for cp in cps}
                generated_at = cps[0].snapshot_generated_at or ""
                self._kafka_consumer.set_seek_plan(
                    cutover_by_shard=cutover_by_shard,
                    generated_at=generated_at,
                    lookback_seconds=self._settings.ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS,
                    checkpoints_by_shard=cp_by_shard,
                )
                logger.info("alive-sync 续跑成功，checkpoint 数=%d", len(cps))
                return
            except (ResyncRequired, _EngineError) as exc:
                logger.warning("alive-sync 续跑失败（%s），回退到 bootstrap", exc)

        await self.bootstrap()

    async def request_resync(self, reason: str = "unknown") -> None:
        """重同步入口：reset → backoff sleep → bootstrap。"""
        logger.warning("alive-sync 请求重同步，原因: %s", reason)
        await self._store.reset()
        backoff = self._settings.ALIVE_SYNC_RESYNC_BACKOFF_SECONDS
        logger.info("alive-sync 重同步退避 %d 秒", backoff)
        await asyncio.sleep(backoff)
        await self.bootstrap()

    async def status(self) -> dict[str, Any]:
        """返回 admin API 可直接透出的运行状态快照。"""
        checkpoints = await self._store.load_checkpoints()
        local_versions = await self._store.load_local_versions()
        shards = {
            cp.shard: {
                "lastSeenSeq": cp.last_seen_seq,
                "cutoverSeq": cp.cutover_seq,
                "kafkaNextOffset": cp.kafka_next_offset,
                "snapshotGeneratedAt": cp.snapshot_generated_at,
            }
            for cp in checkpoints
        }
        return {
            "running": True,
            "aliveCount": len(local_versions),
            "checkpointCount": len(checkpoints),
            "shards": shards,
        }

    async def _ensure_kafka_topic(self) -> None:
        """解析 Kafka 主题：配置留空时从 Provider /sync/info 获取。"""
        topic = self._settings.ALIVE_SYNC_KAFKA_TOPIC.strip()
        if not topic:
            info = await self._source_client.fetch_sync_info()
            topic = info.kafka_topic
        self._kafka_consumer.set_topic(topic)

    async def run(self) -> None:
        """主循环：resume_or_bootstrap → 启动 Kafka consumer → poll_apply。

        bootstrap 必须先于 consumer.start()，以便 rebalance listener 能按 seek plan 定位 offset。
        503 降级（AliveSyncError）按 RETRY_INTERVAL 退避重试；
        asyncio.CancelledError 透传（后台任务退出机制）。
        """
        await self._ensure_kafka_topic()
        try:
            while True:
                try:
                    await self.resume_or_bootstrap()
                    await self._kafka_consumer.stop()
                    await self._kafka_consumer.start()
                    await self._kafka_consumer.poll_apply(
                        self._engine,
                        on_gap=lambda shard: self.request_resync(f"gap on shard {shard}"),
                    )
                except asyncio.CancelledError:
                    raise
                except (AliveSyncError, _EngineError) as exc:
                    logger.warning(
                        "alive-sync 降级/错误，%d 秒后重试: %s", self._settings.ALIVE_SYNC_RETRY_INTERVAL_SECONDS, exc
                    )
                    await asyncio.sleep(self._settings.ALIVE_SYNC_RETRY_INTERVAL_SECONDS)
                except ResyncRequired as exc:
                    logger.warning("alive-sync ResyncRequired: %s", exc.reason)
                    await self.request_resync(exc.reason)
                except Exception as exc:
                    logger.exception("alive-sync 未预期异常: %s", exc)
                    await asyncio.sleep(self._settings.ALIVE_SYNC_RETRY_INTERVAL_SECONDS)
        finally:
            await self._kafka_consumer.stop()
