"""app/access/freshness.py — 每分区水位与整体水位（设计 §2.3，spec §6.1.4）。

三表共享同一水位（C-ACCESS-MODEL-6）：access_events / trace_span / topology_edge_5m
由增量 MV 同 INSERT 同步写，不存在派生视图稳态滞后。
六端点全部用同一 access_events 写入水位填充新鲜度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from app.access.exception import ReadModelLaggingError
from app.core.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)

WM_KEY_PREFIX: Final = "amp:access:wm:"  # 键 = amp:access:wm:{partition_id}
WM_PARTITIONS_KEY: Final = "amp:access:wm:partitions"  # 已知分区集合


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
    batch_max_ts_ms: int,
    now_ms: int,
) -> None:
    """每分区水位推进（设计 §2.3）。

    水位 = min(now, max(prev_wm_p, batch_max_ts))，单调不回退（乱序批次）。
    上限截断 now 防未来时间戳膨胀。
    """
    key = f"{WM_KEY_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0
    new_wm = min(now_ms, max(prev_wm, batch_max_ts_ms))
    await redis.set(key, str(new_wm))
    await redis.sadd(WM_PARTITIONS_KEY, str(partition_id))


async def advance_idle_partition(redis: Redis, *, partition_id: int, now_ms: int) -> None:
    """空闲分区水位推进（lag=0 时防水位冻结，设计 §2.3）。

    wm_p = max(prev, now)。
    """
    key = f"{WM_KEY_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0
    new_wm = max(prev_wm, now_ms)
    await redis.set(key, str(new_wm))
    await redis.sadd(WM_PARTITIONS_KEY, str(partition_id))


async def read_overall_watermark(redis: Redis) -> int | None:
    """整体水位 = min(全部分区 wm_p)（设计 §2.3）。

    取 min 而非 max——max 会让滞后分区缺口被快分区掩盖，导致 LAGGING 漏报。
    任一已知分区无水位 → None（保守，视为最旧）。
    全部分区缺失 → None。
    """
    partitions = await redis.smembers(WM_PARTITIONS_KEY)
    if not partitions:
        return None

    keys = [f"{WM_KEY_PREFIX}{p!s}" for p in partitions]
    values = await redis.mget(*keys)

    wm_list: list[int] = []
    for v in values:
        if v is None:
            return None  # 保守：有分区无水位 → 整体 None
        wm_list.append(int(v))

    return min(wm_list) if wm_list else None


async def evaluate_freshness(
    redis: Redis,
    *,
    now_ms: int | None = None,
) -> FreshnessView:
    """评估当前数据新鲜度（spec §6.1.4）。

    lag = now - wm；lagging = lag > access_lagging_threshold_ms（默认 300s）。
    wm 为 None → lagging=True（水位未知，尚无任何写入）。
    """
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)

    wm = await read_overall_watermark(redis)
    threshold_ms = settings.access_lagging_threshold_ms

    if wm is None:
        return FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)

    lag_ms = now_ms - wm
    lagging = lag_ms > threshold_ms
    # 上报当前读模型滞后量（设计 §6.19 amp_access_read_model_lag_ms）
    try:
        from app.access.metrics import metrics as _metrics

        _metrics.set_gauge("amp_access_read_model_lag_ms", float(lag_ms))
    except Exception:
        logger.debug("failed to report read_model_lag_ms gauge", exc_info=True)
    return FreshnessView(data_freshness_at_ms=wm, ingestion_lag_ms=lag_ms, lagging=lagging)


def apply_degrade_policy(view: FreshnessView, *, strict_503: bool = False) -> bool:
    """应用降级策略（spec §6.1.4）。

    lagging 且 strict_503=True → raise ReadModelLaggingError(503)。
    lagging 且 strict_503=False → 返回 True（调用方置 meta.partial=True）。
    否则 False。
    """
    if not view.lagging:
        return False
    if strict_503:
        raise ReadModelLaggingError("Access read model is lagging behind ingestion")
    return True


def build_meta(
    view: FreshnessView,
    *,
    now_ms: int,
    next_cursor: str | None = None,
    partial: bool | None = None,
    elapsed_ms: int | None = None,
    approximate_total: int | None = None,
) -> AMPResponseMeta:
    """组装 AMPResponseMeta（设计 §3.3 第 7 步）。"""
    from app.core.amp_api_schema import AMPResponseMeta

    if view.data_freshness_at_ms is not None:
        freshness_iso: str | None = datetime.fromtimestamp(view.data_freshness_at_ms / 1000, tz=UTC).isoformat()
    else:
        freshness_iso = None  # 水位未知时不注入无效 epoch 0

    return AMPResponseMeta(
        data_freshness_at=freshness_iso,
        ingestion_lag_ms=view.ingestion_lag_ms,
        next_cursor=next_cursor,
        partial=partial,
        elapsed_ms=elapsed_ms,
        approximate_total=approximate_total,
    )


def freshness_headers(view: FreshnessView) -> dict[str, str]:
    """traces/{traceId} 裸资源响应头（spec §6.4.4）。

    水位非 None → {"AMP-Data-Freshness-At": iso, "AMP-Ingestion-Lag-Ms": str(lag)}。
    水位为 None → 空 dict（不注入无效值）。
    """
    if view.data_freshness_at_ms is None:
        return {}
    freshness_iso = datetime.fromtimestamp(view.data_freshness_at_ms / 1000, tz=UTC).isoformat()
    return {
        "AMP-Data-Freshness-At": freshness_iso,
        "AMP-Ingestion-Lag-Ms": str(view.ingestion_lag_ms or 0),
    }
