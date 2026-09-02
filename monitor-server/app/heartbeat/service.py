"""Heartbeat 模块 — Query API 业务逻辑层（§7.1.2）。

职责：
1. 将 Redis LatestEntry 转换为 HTTP 响应视图模型
2. 提供 get_liveness / query_liveness / silence_top / get_summary 四个查询操作
3. 捕获 Redis 异常 → ReadModelLaggingError（P1-4）
4. summary / silence_top 使用简单内存 TTL 缓存
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import settings
from app.heartbeat import store
from app.heartbeat.exception import (
    HeartbeatAicUnknownError,
    QueryRequiresSelectiveFilterError,
    ReadModelLaggingError,
    UnsupportedFieldError,
)
from app.heartbeat.freshness import FreshnessView, evaluate_freshness, point_lookup_partitions
from app.heartbeat.schema import (
    HeartbeatLivenessQueryRequest,
    HeartbeatLivenessView,
    HeartbeatResponseMetaExt,
    HeartbeatSilenceRankItem,
    HeartbeatSilenceTopRequest,
    HeartbeatSummaryView,
)
from app.heartbeat.sharding import all_shard_ids, shard_id_for_aic

logger = structlog.get_logger(__name__)


# ── 内部工具 ──────────────────────────────────────────────────────────────────


def _ms_to_iso(ms: int) -> str:
    """将 epoch ms 转换为 ISO 8601 UTC 字符串。"""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _build_view(entry: Any, now_ms: int) -> HeartbeatLivenessView:
    """将 LatestEntry 转换为 HeartbeatLivenessView（7-1：含界 silence 判断）。

    is_alive 判定：silence_ms <= silence_threshold_ms（含界）。

    Args:
        entry: store.LatestEntry dataclass。
        now_ms: 当前时间（epoch ms）。

    Returns:
        HeartbeatLivenessView。
    """
    silence_threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000
    silence_ms = now_ms - entry.last_seen_at_ms
    is_alive = silence_ms <= silence_threshold_ms
    liveness_state = "alive" if is_alive else "silent"
    silence_duration_seconds = max(0, int(silence_ms / 1000))

    source_timestamp: str | None = None
    if entry.source_timestamp_ms is not None:
        source_timestamp = _ms_to_iso(entry.source_timestamp_ms)

    return HeartbeatLivenessView(
        aic=entry.aic,
        is_alive=is_alive,
        liveness_state=liveness_state,
        last_seen_at=entry.last_seen_at,
        source_timestamp=source_timestamp,
        silence_duration_seconds=silence_duration_seconds,
    )


def _build_meta(
    now_ms: int,
    fresh: FreshnessView,
    *,
    next_cursor: str | None = None,
    approximate_total: int | None = None,
    elapsed_ms: int | None = None,
) -> HeartbeatResponseMetaExt:
    """构造 HeartbeatResponseMetaExt。

    Args:
        now_ms: 当前时间（epoch ms）。
        fresh: FreshnessView 新鲜度快照。
        next_cursor: 下一页游标（可选）。
        approximate_total: 近似总量（可选）。
        elapsed_ms: 本次查询耗时（可选）。

    Returns:
        HeartbeatResponseMetaExt。
    """
    partial = fresh.lagging_partition_count > 0

    return HeartbeatResponseMetaExt(
        evaluated_at=_ms_to_iso(now_ms),
        silence_threshold_seconds=settings.heartbeat_silence_threshold_seconds,
        evict_after_seconds=settings.heartbeat_evict_after_seconds,
        data_freshness_at=_ms_to_iso(fresh.min_watermark_ms),
        ingestion_lag_ms=now_ms - fresh.min_watermark_ms,
        next_cursor=next_cursor,
        approximate_total=approximate_total,
        partial=partial,
        elapsed_ms=elapsed_ms,
    )


# ── TTL 缓存 ──────────────────────────────────────────────────────────────────


class _TTLCache[T]:
    """简单单值内存 TTL 缓存（summary / silence_top 专用）。

    partial=True 的结果不入缓存（C-QUERY-7）。
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._value: T | None = None
        self._expires_at: float = 0.0

    def get(self) -> T | None:
        if self._value is not None and time.monotonic() < self._expires_at:
            return self._value
        return None

    def set(self, value: T, *, partial: bool = False) -> None:
        if partial:
            return
        self._value = value
        self._expires_at = time.monotonic() + self._ttl


_summary_cache: _TTLCache[tuple[Any, Any]] = _TTLCache(settings.heartbeat_summary_cache_ttl_seconds)
_silence_top_cache: _TTLCache[tuple[Any, Any]] = _TTLCache(settings.heartbeat_silence_top_cache_ttl_seconds)


# ── Query Plan ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LivenessQueryPlan:
    """liveness/query 执行计划。"""

    mode: str
    """查询模式：'aic_in'（AIC 批量点查）、'silence_range'（ZSet 区间扫描）"""

    aics: list[str] | None
    """mode=aic_in 时的 AIC 列表"""

    lower: str | None
    """mode=silence_range 时的 ZSet 下界（Redis ZREVRANGEBYSCORE 语法）"""

    upper: str | None
    """mode=silence_range 时的 ZSet 上界"""

    limit: int
    """单次扫描上限"""

    descending: bool
    """是否降序（P2-6 修复：query_liveness 默认降序）"""


def _plan_liveness_query(
    request: HeartbeatLivenessQueryRequest,
    now_ms: int,
) -> LivenessQueryPlan:
    """分析查询请求，生成执行计划（P2-11：拒绝 time_range；C-QUERY-1：要求选择性 filter）。

    Args:
        request: liveness query 请求体。
        now_ms: 当前时间（epoch ms），用于计算 ZSet 区间边界。

    Returns:
        LivenessQueryPlan 执行计划。

    Raises:
        UnsupportedFieldError: time_range 非 None。
        QueryRequiresSelectiveFilterError: 无有效 filter。
        UnsupportedFieldError / InvalidFilterError: filter 字段/运算符不支持。
    """
    # P2-11：拒绝 time_range（heartbeat 是实时快照，不支持历史时间范围查询）
    if request.time_range is not None:
        raise UnsupportedFieldError("timeRange")

    page_limit = request.page.limit if request.page else 50

    # 分析 filter
    if request.filter is None:
        raise QueryRequiresSelectiveFilterError()

    conditions = request.filter.conditions or []

    # 尝试提取 aic eq/in 条件
    aic_values: list[str] | None = None
    for cond in conditions:
        if cond.field == "aic":
            if cond.op == "eq" and isinstance(cond.value, str):
                aic_values = [cond.value]
                break
            if cond.op == "in" and isinstance(cond.value, list):
                aic_values = [str(v) for v in cond.value]
                break

    if aic_values is not None:
        return LivenessQueryPlan(
            mode="aic_in",
            aics=aic_values,
            lower=None,
            upper=None,
            limit=page_limit,
            descending=True,
        )

    # 尝试 silence_duration_seconds range → 转换为 ZSet score 区间
    min_silence_ms: int | None = None
    max_silence_ms: int | None = None
    for cond in conditions:
        if cond.field == "silenceDurationSeconds":
            if cond.op in ("gte", "gt") and isinstance(cond.value, int | float):
                v = int(cond.value) * 1000
                min_silence_ms = v if cond.op == "gte" else v + 1
            if cond.op in ("lte", "lt") and isinstance(cond.value, int | float):
                v = int(cond.value) * 1000
                max_silence_ms = v if cond.op == "lte" else v - 1

    if min_silence_ms is not None or max_silence_ms is not None:
        # ZSet score = last_seen_at_ms；silence_ms = now - score
        # max_silence_ms → min score (lower)
        # min_silence_ms → max score (upper)
        upper = str(now_ms - (min_silence_ms or 0))
        lower = str(now_ms - (max_silence_ms or 0)) if max_silence_ms is not None else "-inf"
        return LivenessQueryPlan(
            mode="silence_range",
            aics=None,
            lower=lower,
            upper=upper,
            limit=page_limit,
            descending=True,
        )

    raise QueryRequiresSelectiveFilterError()


# ── 公开查询函数 ──────────────────────────────────────────────────────────────


async def get_liveness(
    redis: Redis,
    aic: str,
) -> tuple[HeartbeatLivenessView, HeartbeatResponseMetaExt]:
    """点查 AIC liveness（GET /liveness/{aic}）。

    Args:
        redis: Redis 客户端。
        aic: Agent Identity Code。

    Returns:
        (HeartbeatLivenessView, HeartbeatResponseMetaExt) 元组。

    Raises:
        HeartbeatAicUnknownError: AIC 不存在（404）。
        ReadModelLaggingError: Redis 连接异常（503，P1-4）。
    """
    try:
        now_ms = await store.redis_now_ms(redis)
        from app.heartbeat.sharding import shard_id_for_aic

        shard = shard_id_for_aic(aic)
        entry = await store.get_latest(redis, shard, aic)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    # 404 check 先于 freshness 评估（AIC 不存在时不需要读水位）
    if entry is None:
        raise HeartbeatAicUnknownError(aic)

    t0 = time.monotonic()

    try:
        partitions = point_lookup_partitions(aic)
        fresh = await evaluate_freshness(redis, partitions=partitions, now_ms=now_ms)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    view = _build_view(entry, now_ms)
    meta = _build_meta(now_ms, fresh, elapsed_ms=elapsed_ms)
    return view, meta


async def query_liveness(
    redis: Redis,
    request: HeartbeatLivenessQueryRequest,
) -> tuple[list[HeartbeatLivenessView], HeartbeatResponseMetaExt]:
    """列表查询（POST /liveness/query）。

    Args:
        redis: Redis 客户端。
        request: liveness query 请求体。

    Returns:
        (items, meta) 元组。

    Raises:
        UnsupportedFieldError / QueryRequiresSelectiveFilterError: 计划失败。
        ReadModelLaggingError: Redis 连接异常（503，P1-4）。
    """
    try:
        now_ms = await store.redis_now_ms(redis)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    t0 = time.monotonic()

    # 生成执行计划（P2-11 + C-QUERY-1 验证在此发生）
    plan = _plan_liveness_query(request, now_ms)

    try:
        items: list[HeartbeatLivenessView] = []
        next_cursor: str | None = None

        if plan.mode == "aic_in":
            assert plan.aics is not None  # nosec B101
            # 批量点查：按 AIC 分组到各 shard
            from app.heartbeat.sharding import shard_id_for_aic

            shard_aics: dict[str, list[str]] = {}
            for a in plan.aics:
                s = shard_id_for_aic(a)
                shard_aics.setdefault(s, []).append(a)

            for shard, aics in shard_aics.items():
                entries = await store.mget_latest(redis, shard, aics)
                for entry in entries:
                    if entry is not None:
                        items.append(_build_view(entry, now_ms))

        elif plan.mode == "silence_range":
            # ZSet 区间扫描（降序，P2-6）
            from app.heartbeat.cursor import (
                LivenessQueryCursor,
                ShardPosition,
                decode_cursor,
                encode_cursor,
                filter_fingerprint,
            )
            from app.heartbeat.sharding import shard_id_for_aic

            fingerprint = filter_fingerprint(request)
            cursor: LivenessQueryCursor | None = None
            if request.page and request.page.cursor:
                cursor = decode_cursor(request.page.cursor, fingerprint)

            shards = all_shard_ids()
            new_positions: dict[str, ShardPosition] = {}
            collected = 0
            limit = plan.limit

            assert plan.upper is not None  # nosec B101

            for shard in shards:
                if collected >= limit:
                    break
                remaining = limit - collected

                # 使用 cursor 位置
                upper = plan.upper
                if cursor and shard in cursor.positions:
                    pos = cursor.positions[shard]
                    upper = str(pos.last_seen_at_ms - 1)

                if plan.lower is not None:
                    entries_with_scores = await store.zrevrange_by_score(
                        redis, shard, lower=plan.lower, upper=upper, limit=remaining
                    )
                else:
                    entries_with_scores = await store.zrevrange_by_score(
                        redis, shard, lower="-inf", upper=upper, limit=remaining
                    )

                if not entries_with_scores:
                    continue

                aics_in_page = [a for a, _ in entries_with_scores]
                batch = await store.mget_latest(redis, shard, aics_in_page)
                for entry in batch:
                    if entry is not None:
                        items.append(_build_view(entry, now_ms))
                        collected += 1

                last_a, last_score = entries_with_scores[-1]
                new_positions[shard] = ShardPosition(last_seen_at_ms=last_score, last_aic=last_a)

            if new_positions:
                new_cursor = LivenessQueryCursor(positions=new_positions, fingerprint=fingerprint)
                next_cursor = encode_cursor(new_cursor)

        fresh = await evaluate_freshness(redis, partitions=None, now_ms=now_ms)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta = _build_meta(now_ms, fresh, next_cursor=next_cursor, elapsed_ms=elapsed_ms)
    return items, meta


async def silence_top(
    redis: Redis,
    request: HeartbeatSilenceTopRequest,
) -> tuple[list[HeartbeatSilenceRankItem], HeartbeatResponseMetaExt]:
    """返回静默时间最长的 top N AIC（POST /silence/top）。

    Args:
        redis: Redis 客户端。
        request: silence/top 请求体。

    Returns:
        (items, meta) 元组。

    Raises:
        ReadModelLaggingError: Redis 连接异常（503，P1-4）。
    """
    try:
        now_ms = await store.redis_now_ms(redis)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    t0 = time.monotonic()

    top_n = request.top_n or settings.heartbeat_silence_top_default_n
    top_n = min(top_n, settings.heartbeat_silence_top_max_n)

    silence_threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000
    evict_ms = settings.heartbeat_evict_after_seconds * 1000

    try:
        # 按 silence 时长降序：last_seen_at_ms 最小的排最前
        min_silence_ms = (request.min_silence_seconds or 0) * 1000
        max_silence_ms = (request.max_silence_seconds * 1000) if request.max_silence_seconds else evict_ms

        # ZSet score 范围：silence_ms = now - score → score range
        upper = str(now_ms - min_silence_ms)  # score <= now - min_silence (older = more silent)
        lower = str(now_ms - max_silence_ms)  # score >= now - max_silence

        shards = all_shard_ids()
        candidates: list[tuple[str, int]] = []  # (aic, last_seen_at_ms)

        fetch_size = settings.heartbeat_silence_top_shard_fetch_size
        for shard in shards:
            rows = await store.zrange_by_score(
                redis, shard, lower=lower, upper=upper, limit=fetch_size, with_scores=True
            )
            candidates.extend(rows)

        # 全局按 score 升序（最小 = 最沉默）→ 取 top_n
        candidates.sort(key=lambda x: x[1])
        top = candidates[:top_n]

        # 过滤 only_silent
        items: list[HeartbeatSilenceRankItem] = []
        for aic, last_ms in top:
            silence_s = max(0, int((now_ms - last_ms) / 1000))
            if request.only_silent:
                silence_ms_val = now_ms - last_ms
                if silence_ms_val <= silence_threshold_ms:
                    continue
            items.append(
                HeartbeatSilenceRankItem(
                    aic=aic,
                    last_seen_at=_ms_to_iso(last_ms),
                    silence_duration_seconds=silence_s,
                )
            )

        fresh = await evaluate_freshness(redis, partitions=None, now_ms=now_ms)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta = _build_meta(now_ms, fresh, approximate_total=len(candidates), elapsed_ms=elapsed_ms)
    return items, meta


async def get_summary(
    redis: Redis,
    *,
    allowed_aics: list[str] | None = None,
) -> tuple[HeartbeatSummaryView, HeartbeatResponseMetaExt]:
    """全局 liveness 汇总统计（GET /summary）。

    Args:
        redis: Redis 客户端。

    Returns:
        (HeartbeatSummaryView, HeartbeatResponseMetaExt) 元组。

    Raises:
        ReadModelLaggingError: Redis 连接异常（503，P1-4）。
    """
    try:
        now_ms = await store.redis_now_ms(redis)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    t0 = time.monotonic()
    silence_threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000

    try:
        shards = all_shard_ids()
        total = 0
        alive = 0

        if allowed_aics is None:
            for shard in shards:
                total += await store.zcard(redis, shard)
                alive += await store.zcount_score_at_least(redis, shard, now_ms - silence_threshold_ms)
        else:
            shard_map: dict[str, list[str]] = {}
            for aic in allowed_aics:
                shard_map.setdefault(shard_id_for_aic(aic), []).append(aic)
            for shard, aics in shard_map.items():
                rows = await store.mget_latest(redis, shard, aics)
                for row in rows:
                    if row is None:
                        continue
                    total += 1
                    if now_ms - row.last_seen_at_ms <= silence_threshold_ms:
                        alive += 1

        fresh = await evaluate_freshness(redis, partitions=None, now_ms=now_ms)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise ReadModelLaggingError(detail=str(exc)) from exc

    silent = max(0, total - alive)
    shard_count = len(shards)

    summary = HeartbeatSummaryView(
        total_known=total,
        alive_count=alive,
        silent_count=silent,
        responded_shard_count=shard_count,
        total_shard_count=shard_count,
        partial=fresh.lagging_partition_count > 0,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta = _build_meta(now_ms, fresh, approximate_total=total, elapsed_ms=elapsed_ms)

    return summary, meta
