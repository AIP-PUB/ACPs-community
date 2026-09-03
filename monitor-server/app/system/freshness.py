"""app/system/freshness.py — 保守摄取水位（设计 §2.4）+ 滞后评估 + meta 组装。

保守：water = min(now, max(prev, batch_max_event_ts - reorder_margin))，单调不回退。
不取 max(timestamp)（乱序到达会高估完整性，设计 §2.4 / D-5）。
多 Kafka 分区 → 每分区水位 + 整体 min。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from app.system.exception import ReadModelLaggingError
from app.system.metrics import AMP_SYSTEM_READ_MODEL_LAG_MS, metrics

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)

WM_INGEST_PREFIX: Final = "amp:system:wm:ingest:"  # key = amp:system:wm:ingest:{partition_id}
WM_INGEST_PARTITIONS: Final = "amp:system:wm:ingest:partitions"


@dataclass(frozen=True)
class FreshnessView:
    """新鲜度快照（evaluate_freshness 返回）。"""

    data_freshness_at_ms: int | None
    ingestion_lag_ms: int | None
    lagging: bool


async def advance_partition_watermark(
    redis: Redis,
    *,
    partition_id: int,
    batch_max_event_ts_ms: int,
    now_ms: int,
    reorder_margin_ms: int,
) -> None:
    """每分区保守水位推进（设计 §2.4 / D-5）。

    candidate = min(now_ms, batch_max_event_ts_ms - reorder_margin_ms)
    new = min(now_ms, max(prev, candidate))，单调不回退。
    reorder_margin_ms 同时覆盖迟到事件乱序与 OpenSearch refresh 可见延迟。
    """
    key = f"{WM_INGEST_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0

    candidate = min(now_ms, batch_max_event_ts_ms - reorder_margin_ms)
    new_wm = min(now_ms, max(prev_wm, candidate))

    await redis.set(key, str(new_wm))
    await redis.sadd(WM_INGEST_PARTITIONS, str(partition_id))


async def advance_idle_partition(
    redis: Redis,
    *,
    partition_id: int,
    now_ms: int,
    reorder_margin_ms: int,
) -> None:
    """空闲分区（追平 highwater）水位向 (now - reorder_margin) 收敛（防水位冻结）。

    确保分区有活跃消费但无新事件时，水位仍推进而不冻结。
    """
    key = f"{WM_INGEST_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0

    idle_wm = now_ms - reorder_margin_ms
    new_wm = max(prev_wm, idle_wm)

    await redis.set(key, str(new_wm))
    await redis.sadd(WM_INGEST_PARTITIONS, str(partition_id))


async def read_watermark(redis: Redis) -> int | None:
    """整体水位 = min(全部分区水位)；任一分区缺水位 → None（保守）。"""
    partitions = await redis.smembers(WM_INGEST_PARTITIONS)
    if not partitions:
        return None

    keys = [f"{WM_INGEST_PREFIX}{p!s}" if isinstance(p, str) else f"{WM_INGEST_PREFIX}{p.decode()}" for p in partitions]
    values = await redis.mget(*keys)

    wm_list: list[int] = []
    for v in values:
        if v is None:
            return None
        wm_list.append(int(v))

    return min(wm_list) if wm_list else None


async def evaluate_freshness(
    redis: Redis,
    *,
    now_ms: int | None = None,
    lagging_threshold_ms: int,
) -> FreshnessView:
    """评估新鲜度（spec §6.1.4 / 设计 §2.4）。

    wm None → (None, None, lagging=True)（保守，无水位视为滞后）。
    """
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)

    wm = await read_watermark(redis)

    if wm is None:
        return FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)

    lag_ms = now_ms - wm
    lagging = lag_ms > lagging_threshold_ms
    metrics.set_gauge(AMP_SYSTEM_READ_MODEL_LAG_MS, float(lag_ms))
    return FreshnessView(data_freshness_at_ms=wm, ingestion_lag_ms=lag_ms, lagging=lagging)


def apply_degrade_policy(view: FreshnessView, *, strict_503: bool = False) -> bool:
    """应用降级策略（对齐 message.freshness.apply_degrade_policy）。

    lagging 且 strict_503 → raise ReadModelLaggingError(503)。
    lagging 且非 strict → True（meta.partial）。
    不滞后 → False。
    """
    if not view.lagging:
        return False
    if strict_503:
        raise ReadModelLaggingError("System read model is lagging behind ingestion.")
    return True


def build_meta(
    view: FreshnessView,
    *,
    now_ms: int,
    next_cursor: str | None = None,
    partial: bool | None = None,
    elapsed_ms: int | None = None,
) -> AMPResponseMeta:
    """组装 AMPResponseMeta（水位 None 时不注入 dataFreshnessAt）。

    system 不用 partialDataFields/sampleCoverage（事件检索非聚合，设计 §5.3）。
    """
    from app.core.amp_api_schema import AMPResponseMeta

    if view.data_freshness_at_ms is not None:
        freshness_iso: str | None = datetime.fromtimestamp(view.data_freshness_at_ms / 1000, tz=UTC).isoformat()
    else:
        freshness_iso = None

    return AMPResponseMeta(
        data_freshness_at=freshness_iso,
        ingestion_lag_ms=view.ingestion_lag_ms,
        next_cursor=next_cursor,
        partial=partial,
        elapsed_ms=elapsed_ms,
    )
