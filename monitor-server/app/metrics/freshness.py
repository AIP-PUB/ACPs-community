"""app/metrics/freshness.py — 读模型新鲜度（§2.3、spec §6.1.4）。

dataFreshnessAt 水位来自 TSDB 写入进度，不是 Redis 缓存写时间（C-METRIC-QUERY-5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from redis.asyncio import Redis

from app.core.amp_api_schema import AMPResponseMeta
from app.metrics.exception import ReadModelLaggingError

logger = structlog.get_logger(__name__)

DATA_FRESHNESS_KEY: Final = "amp:metrics:data_freshness_at_ms"
"""Redis String 键，存储最近一次成功写入 TSDB 的事件时间水位（毫秒）。"""


# ── FreshnessView ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FreshnessView:
    """读模型新鲜度快照。"""

    data_freshness_at_ms: int | None
    """最近一次成功写入 TSDB 的事件时间水位；None 表示尚无任何写入。"""

    ingestion_lag_ms: int | None
    """now() - dataFreshnessAt（毫秒）；None 表示水位未知。"""

    lagging: bool
    """lag > metrics_lagging_threshold_ms，或水位完全未知。"""


# ── Redis 操作 ────────────────────────────────────────────────────────────────

_LUA_ADVANCE = """
local key = KEYS[1]
local new_val = tonumber(ARGV[1])
local existing = redis.call('GET', key)
if not existing or tonumber(existing) < new_val then
    redis.call('SET', key, tostring(new_val))
    return 1
end
return 0
"""


async def advance_watermark(redis: Redis, observed_at_ms: int) -> None:
    """推进 dataFreshnessAt 水位（Remote Write 成功后调用，C-METRIC-QUERY-5）。

    使用 Lua 脚本实现 max(existing, observed_at_ms) 原子操作。

    Args:
        redis: Redis 客户端。
        observed_at_ms: 本次写入批次的最大事件时间戳（毫秒）。
    """
    await redis.eval(_LUA_ADVANCE, 1, DATA_FRESHNESS_KEY, str(observed_at_ms))


async def read_watermark(redis: Redis) -> int | None:
    """读取当前 dataFreshnessAt 水位。

    Returns:
        int | None: 毫秒时间戳；键缺失返回 None。
    """
    raw: bytes | str | None = await redis.get(DATA_FRESHNESS_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def evaluate_freshness(
    redis: Redis,
    *,
    now_ms: int | None = None,
) -> FreshnessView:
    """评估当前读模型新鲜度（§2.3）。

    Args:
        redis: Redis 客户端。
        now_ms: 当前时间戳（毫秒）；None 则取 datetime.now(UTC)。

    Returns:
        FreshnessView
    """
    from app.core.config import get_settings

    s = get_settings()
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)

    wm = await read_watermark(redis)
    if wm is None:
        return FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)

    lag_ms = now_ms - wm
    lagging = lag_ms > s.metrics_lagging_threshold_ms
    return FreshnessView(
        data_freshness_at_ms=wm,
        ingestion_lag_ms=lag_ms,
        lagging=lagging,
    )


# ── 降级策略 ──────────────────────────────────────────────────────────────────


def apply_degrade_policy(view: FreshnessView, *, strict_503: bool = False) -> bool:
    """根据新鲜度视图决定是否降级（spec §6.1.4）。

    - 水位未知（data_freshness_at_ms=None） → 一律 raise（无法给出 dataFreshnessAt）
    - lagging=True → strict_503 或 lagging_response_mode=="503" → raise ReadModelLaggingError
    - lagging=True 且 "partial" 模式 → 返回 True（调用方置 meta.partial=True）
    - lagging=False → 返回 False

    Args:
        view: FreshnessView。
        strict_503: 强制 503（无论 lagging_response_mode 配置）。

    Returns:
        bool: True 表示"可继续，但结果标为 partial"。

    Raises:
        ReadModelLaggingError: 需要返回 503。
    """
    from app.core.config import get_settings

    if view.data_freshness_at_ms is None:
        raise ReadModelLaggingError("Metrics read model watermark is unknown. No data has been ingested yet.")

    if view.lagging:
        s = get_settings()
        if strict_503 or s.metrics_lagging_response_mode == "503":
            lag_str = f"{view.ingestion_lag_ms}ms" if view.ingestion_lag_ms is not None else "unknown"
            raise ReadModelLaggingError(f"Metrics read model is lagging by {lag_str}. Please retry later.")
        # partial 模式
        return True

    return False


# ── meta 组装 ─────────────────────────────────────────────────────────────────


def build_meta(
    view: FreshnessView,
    *,
    now_ms: int,
    next_cursor: str | None = None,
    partial: bool | None = None,
    elapsed_ms: int | None = None,
    approximate_total: int | None = None,
) -> AMPResponseMeta:
    """统一组装 AMPResponseMeta（设计 §3.2 第 5 条）。

    Args:
        view: FreshnessView（data_freshness_at_ms 必须非 None，否则应在调用方先 apply_degrade_policy）。
        now_ms: 当前时间戳（毫秒）。
        next_cursor: 下一页游标（无时传 None）。
        partial: 是否为部分结果（读模型滞后时为 True）。
        elapsed_ms: Provider 查询耗时（毫秒）。
        approximate_total: 近似总量。

    Returns:
        AMPResponseMeta
    """
    if view.data_freshness_at_ms is None:
        raise ValueError("build_meta 要求 data_freshness_at_ms 非 None")

    freshness_dt = datetime.fromtimestamp(view.data_freshness_at_ms / 1000, tz=UTC)
    freshness_iso = freshness_dt.isoformat()

    return AMPResponseMeta(
        data_freshness_at=freshness_iso,
        ingestion_lag_ms=view.ingestion_lag_ms,
        next_cursor=next_cursor,
        partial=partial,
        elapsed_ms=elapsed_ms,
        approximate_total=approximate_total,
    )


__all__ = [
    "DATA_FRESHNESS_KEY",
    "FreshnessView",
    "advance_watermark",
    "apply_degrade_policy",
    "build_meta",
    "evaluate_freshness",
    "read_watermark",
]
