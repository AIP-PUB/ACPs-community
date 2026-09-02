"""alive-sync 进程级单例与 start/stop 入口（lifespan 调用）。

runtime.py 只提供进程级单例与入口，不负责调度策略。
AliveSyncService.run() 包进 asyncio.create_task 托管。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from acps_sdk.amp.alive_sync.engine import AliveSyncEngine

from app.heartbeat_sync.kafka_consumer import AliveDeltaKafkaConsumer
from app.heartbeat_sync.service import AliveSyncService
from app.heartbeat_sync.source_client import AliveSyncSourceClient
from app.heartbeat_sync.store import PostgresAliveSyncStore

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

_service: AliveSyncService | None = None
_task: asyncio.Task[None] | None = None


def _build_service(settings: Settings) -> AliveSyncService:
    store = PostgresAliveSyncStore()
    source_client = AliveSyncSourceClient(
        base_url=settings.ALIVE_SYNC_PROVIDER_BASE_URL,
        timeout=settings.ALIVE_SYNC_HTTP_TIMEOUT,
        bearer_token=settings.ALIVE_SYNC_PROVIDER_BEARER_TOKEN,
    )
    kafka_consumer = AliveDeltaKafkaConsumer(
        bootstrap_servers=settings.ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS,
        group_id=settings.ALIVE_SYNC_KAFKA_GROUP_ID,
        topic=settings.ALIVE_SYNC_KAFKA_TOPIC,
        max_lookback_seconds=settings.ALIVE_SYNC_BOOTSTRAP_MAX_LOOKBACK_SECONDS,
    )
    engine = AliveSyncEngine(store)
    return AliveSyncService(
        settings=settings,
        store=store,
        source_client=source_client,
        kafka_consumer=kafka_consumer,
        engine=engine,
    )


async def start_alive_sync(settings: Settings) -> None:
    """启动 alive-sync 后台 task（lifespan 调用）。"""
    global _service, _task
    _service = _build_service(settings)
    _task = asyncio.create_task(_service.run(), name="alive-sync")
    logger.info("alive-sync 后台 task 已启动")


async def stop_alive_sync() -> None:
    """停止 alive-sync 后台 task（lifespan 调用）。"""
    global _service, _task
    if _task is not None and not _task.done():
        _task.cancel()
        with suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(_task), timeout=10.0)
    _task = None
    _service = None
    logger.info("alive-sync 后台 task 已停止")


def get_alive_sync_service() -> AliveSyncService | None:
    """返回当前 AliveSyncService 实例（如已启动），供 admin API 使用。"""
    return _service
