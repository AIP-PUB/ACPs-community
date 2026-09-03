"""app/message/freshness.py — 四读模型独立水位（设计 §2.4，C-MESSAGE-MODEL-7）。

比 access 单一水位更复杂：四读模型各有独立水位：
  events 摄取水位：每分区 + 整体 min（与 access 同构）
  lifecycle / throughput：compactor 完成一轮后推进（单值水位）
  state：collector 写快照后推进（单值水位）

水位双用途（compactor）：WM_LIFECYCLE / WM_THROUGHPUT 既是 rebuild_from 基准，
  又是对应查询端点的 dataFreshnessAt 来源（设计 §2.2 派生视图滞后必须经 meta 明示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

import structlog

from app.core.config import settings
from app.message.exception import ReadModelLaggingError

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.core.amp_api_schema import AMPResponseMeta

logger = structlog.get_logger(__name__)

ReadModel = Literal["events", "lifecycle", "state", "throughput"]

# ── Redis 键 ───────────────────────────────────────────────────────────────────

WM_INGEST_PREFIX: Final = "amp:message:wm:ingest:"  # 键 = amp:message:wm:ingest:{partition_id}
WM_INGEST_PARTITIONS: Final = "amp:message:wm:ingest:partitions"
WM_LIFECYCLE: Final = "amp:message:wm:lifecycle"  # Lifecycle Compactor 水位
WM_THROUGHPUT: Final = "amp:message:wm:throughput"  # Throughput Compactor 水位
WM_STATE: Final = "amp:message:wm:state"  # State Collector 水位


# ── 数据结构 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FreshnessView:
    """新鲜度快照（evaluate_freshness 返回）。"""

    data_freshness_at_ms: int | None
    ingestion_lag_ms: int | None
    lagging: bool


# ── events 摄取水位（每分区 + 整体 min，同 access）─────────────────────────────


async def advance_partition_watermark(
    redis: Redis,
    *,
    partition_id: int,
    batch_max_ts_ms: int,
    now_ms: int,
) -> None:
    """每分区水位推进（设计 §2.4）。

    wm_p = min(now, max(prev_wm_p, batch_max_ts))，单调不回退，上限截断 now。
    """
    key = f"{WM_INGEST_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0
    new_wm = min(now_ms, max(prev_wm, batch_max_ts_ms))
    await redis.set(key, str(new_wm))
    await redis.sadd(WM_INGEST_PARTITIONS, str(partition_id))


async def advance_idle_partition(
    redis: Redis,
    *,
    partition_id: int,
    now_ms: int,
) -> None:
    """空闲分区水位推进（lag=0 时防水位冻结）。"""
    key = f"{WM_INGEST_PREFIX}{partition_id}"
    prev_raw = await redis.get(key)
    prev_wm = int(prev_raw) if prev_raw else 0
    new_wm = max(prev_wm, now_ms)
    await redis.set(key, str(new_wm))
    await redis.sadd(WM_INGEST_PARTITIONS, str(partition_id))


async def read_events_watermark(redis: Redis) -> int | None:
    """整体水位 = min(全部分区 wm_p)；任一分区无水位 → None（保守）。"""
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


# ── Compactor / Collector 单值水位 ────────────────────────────────────────────


def _kind_to_key(kind: str) -> str:
    if kind == "lifecycle":
        return WM_LIFECYCLE
    if kind == "throughput":
        return WM_THROUGHPUT
    raise ValueError(f"Unknown kind: {kind}")


async def read_compaction_watermark(
    redis: Redis,
    *,
    kind: Literal["lifecycle", "throughput"],
) -> int | None:
    """读 compactor 水位；缺失 → None（首轮全量回算）。"""
    raw = await redis.get(_kind_to_key(kind))
    return int(raw) if raw else None


async def set_compaction_watermark(
    redis: Redis,
    *,
    kind: Literal["lifecycle", "throughput"],
    watermark_ms: int,
) -> None:
    """推进 compactor 水位（单调不回退）。"""
    key = _kind_to_key(kind)
    prev_raw = await redis.get(key)
    prev = int(prev_raw) if prev_raw else 0
    new_wm = max(prev, watermark_ms)
    await redis.set(key, str(new_wm))


async def read_state_watermark(redis: Redis) -> int | None:
    """读 state collector 水位。"""
    raw = await redis.get(WM_STATE)
    return int(raw) if raw else None


async def set_state_watermark(redis: Redis, *, captured_at_ms: int) -> None:
    """推进 state collector 水位。"""
    await redis.set(WM_STATE, str(captured_at_ms))


# ── 新鲜度评估 ────────────────────────────────────────────────────────────────


async def evaluate_freshness(
    redis: Redis,
    *,
    read_model: ReadModel,
    now_ms: int | None = None,
) -> FreshnessView:
    """评估对应读模型的新鲜度（spec §6.5.4）。

    各端点对应水位：
      events → read_events_watermark
      lifecycle → read_compaction_watermark("lifecycle")
      throughput → read_compaction_watermark("throughput")
      state → read_state_watermark
    """
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)

    if read_model == "events":
        wm = await read_events_watermark(redis)
    elif read_model == "lifecycle":
        wm = await read_compaction_watermark(redis, kind="lifecycle")
    elif read_model == "throughput":
        wm = await read_compaction_watermark(redis, kind="throughput")
    elif read_model == "state":
        wm = await read_state_watermark(redis)
    else:
        wm = None

    threshold_ms = settings.message_lagging_threshold_ms

    if wm is None:
        return FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)

    lag_ms = now_ms - wm
    lagging = lag_ms > threshold_ms
    return FreshnessView(data_freshness_at_ms=wm, ingestion_lag_ms=lag_ms, lagging=lagging)


# ── 降级策略 ──────────────────────────────────────────────────────────────────


def apply_degrade_policy(view: FreshnessView, *, strict_503: bool = False) -> bool:
    """应用降级策略（严格对齐 access.freshness.apply_degrade_policy）。

    lagging 且 strict_503=True → raise ReadModelLaggingError(503)。
    lagging 且 strict_503=False → 返回 True（service 置 meta.partial=True）。
    不滞后 → 返回 False。
    """
    if not view.lagging:
        return False
    if strict_503:
        raise ReadModelLaggingError("Message read model is lagging behind ingestion.")
    return True


# ── meta 组装 ──────────────────────────────────────────────────────────────────


def build_meta(
    view: FreshnessView,
    *,
    now_ms: int,
    next_cursor: str | None = None,
    partial: bool | None = None,
    elapsed_ms: int | None = None,
    approximate_total: int | None = None,
    partial_data_fields: list[str] | None = None,
    sample_coverage: dict[str, object] | None = None,
) -> AMPResponseMeta:
    """组装 AMPResponseMeta（水位 None 时不注入无效 epoch 0）。

    partial_data_fields / sample_coverage 仅 destinations/query 传入（C-MESSAGE-QUERY-5）。
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
        approximate_total=approximate_total,
        partial_data_fields=partial_data_fields,
        sample_coverage=sample_coverage,
    )


def freshness_headers(view: FreshnessView) -> dict[str, str]:
    """裸资源端点响应头（lifecycles/{messageId}、destinations/throughput，spec §6.5.4）。

    水位非 None → {"AMP-Data-Freshness-At": iso, "AMP-Ingestion-Lag-Ms": str(lag)}。
    水位 None → 空 dict。
    """
    if view.data_freshness_at_ms is None:
        return {}
    freshness_iso = datetime.fromtimestamp(view.data_freshness_at_ms / 1000, tz=UTC).isoformat()
    return {
        "AMP-Data-Freshness-At": freshness_iso,
        "AMP-Ingestion-Lag-Ms": str(view.ingestion_lag_ms or 0),
    }
