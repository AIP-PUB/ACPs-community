"""Heartbeat 模块 — SnapshotExporter（全量快照生成器，§7.3 / C-SYNC-2 / C-SYNC-3）。

职责：
1. 以"cutover-then-enumerate"顺序生成全量 NDJSON 快照（C-SYNC-3）
2. 在 snapshot_share_window_seconds 内共享同一 MaterializedSnapshot（单例缓存）
3. tie-safe 枚举：末尾同 score 的条目完整并入（C-SYNC-2）
4. 过滤 left_alive 条目（弱快照不变式保障，8-5）
5. 超时截断（snapshot_max_enumeration_seconds）

P1-3：get_snapshot_exporter() 是模块级单例工厂，api.py 不直接实例化 SnapshotExporter。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_TYPE,
    AliveDeltaEnvelope,
    AliveSetEntry,
    AliveSnapshotMeta,
    alive_object_id,
    seq_to_str,
)
from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat import store
from app.heartbeat.sharding import all_shard_ids

logger = structlog.get_logger(__name__)

ENUM_TIMEOUT_METRIC = "amp_heartbeat_snapshot_enum_timeout_total"

# 简单模块级计数器，Step 10 由 HeartbeatMetrics 替换
_enum_timeout_count: int = 0

# B-3: stream() 每隔 N 行 yield asyncio.sleep(0)，让出 event loop（防大快照占满调度）
_STREAM_YIELD_INTERVAL = 50


def _ms_to_iso(ms: int) -> str:
    """epoch ms → ISO 8601 UTC 字符串。"""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


@dataclass(frozen=True)
class MaterializedSnapshot:
    """物化快照：NDJSON 预序列化行 + meta + 生成时间。

    lines[0] 是 AliveSnapshotMeta 序列化行（首行 recordType=snapshot-meta）。
    lines[1:] 是 AliveDeltaEnvelope 序列化行（snapshot 条目）。

    内存估算注释（§O-6）：
      10 万活跃 AIC × ~200 B/条 ≈ 20 MB；单次物化后整批缓存于内存直到 share window 过期。
    """

    meta: AliveSnapshotMeta
    lines: list[bytes]
    materialized_at_ms: int


# ── 单例 ──────────────────────────────────────────────────────────────────────

_exporter_instance: SnapshotExporter | None = None


def get_snapshot_exporter() -> SnapshotExporter:
    """模块级单例工厂（P1-3）。

    api.py 不直接实例化 SnapshotExporter，统一通过此函数获取。
    """
    global _exporter_instance
    if _exporter_instance is None:
        _exporter_instance = SnapshotExporter()
    return _exporter_instance


# ── SnapshotExporter ──────────────────────────────────────────────────────────


class SnapshotExporter:
    """全量快照生成器（P1-3：通过 get_snapshot_exporter() 获取单例）。

    公开接口：
      stream(redis)           → AsyncIterator[bytes]  （NDJSON 行）
      _get_or_materialize     → MaterializedSnapshot  （带缓存锁）
      _enumerate_shard_alive  → list[AliveDeltaEnvelope]（逐分片枚举）
    """

    def __init__(self) -> None:
        self._cached: MaterializedSnapshot | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def stream(self, redis: Redis) -> AsyncIterator[bytes]:
        """产出物化快照的 NDJSON 行字节流（B-3: 输出限流）。

        每 _STREAM_YIELD_INTERVAL 行调用一次 asyncio.sleep(0)，
        将 event loop 调度权还给其他协程，防止大快照长时间占用。

        Args:
            redis: Redis 客户端（用于枚举时读取 store）。

        Yields:
            每条 NDJSON 行的 bytes（以 \\n 结尾）。
        """
        snap = await self._get_or_materialize(redis)
        for i, line in enumerate(snap.lines):
            yield line
            if i % _STREAM_YIELD_INTERVAL == 0:
                await asyncio.sleep(0)

    async def _get_or_materialize(self, redis: Redis) -> MaterializedSnapshot:
        """取缓存快照或重新物化（带 asyncio.Lock，P1-3 短窗共享）。

        8-6: read_all_published_seq（cutover）先于枚举（C-SYNC-3）。

        Args:
            redis: Redis 客户端。

        Returns:
            MaterializedSnapshot（新建或缓存命中）。
        """
        async with self._lock:
            now_ms = await store.redis_now_ms(redis)
            share_window_ms = settings.heartbeat_snapshot_share_window_seconds * 1000

            if self._cached is not None and (now_ms - self._cached.materialized_at_ms) < share_window_ms:
                return self._cached

            # C-SYNC-3: cutover 先于枚举
            cutover = await store.read_all_published_seq(redis)

            envelopes: list[AliveDeltaEnvelope] = []
            for shard in all_shard_ids():
                shard_cutover_seq = cutover.get(shard, 0)
                shard_envs = await self._enumerate_shard_alive(
                    redis, shard, now_ms=now_ms, cutover_seq=shard_cutover_seq
                )
                envelopes.extend(shard_envs)

            generated_at = _ms_to_iso(now_ms)
            meta = AliveSnapshotMeta(
                record_type="snapshot-meta",
                type=ALIVE_DELTA_TYPE,
                cutover_seq_by_shard={s: seq_to_str(v) for s, v in cutover.items()},
                generated_at=generated_at,
            )

            # 8-9: 首行 meta（recordType=snapshot-meta），后续行 AliveDeltaEnvelope
            meta_bytes = meta.model_dump_json(by_alias=True).encode() + b"\n"
            lines: list[bytes] = [meta_bytes]
            for env in envelopes:
                lines.append(env.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n")

            snap = MaterializedSnapshot(meta=meta, lines=lines, materialized_at_ms=now_ms)
            self._cached = snap
            logger.info(
                "snapshot materialized",
                alive_count=len(envelopes),
                shard_count=len(all_shard_ids()),
                now_ms=now_ms,
            )
            return snap

    async def _enumerate_shard_alive(
        self,
        redis: Redis,
        shard: str,
        *,
        now_ms: int,
        cutover_seq: int,
    ) -> list[AliveDeltaEnvelope]:
        """枚举单分片内所有 alive 的 AIC 快照条目（C-SYNC-2 tie-safe）。

        算法（score 组原子读取法）：
        1. 从 score >= now_ms - silence_threshold_ms 升序扫描 liveness_zset
        2. 每页读完后，对末尾 score S 做 tie-safe 补读（zrange_score_group）
        3. 8-3: is_last_page = len(chunk_before_tie_read) < chunk_size
        4. 8-4: is_last_page=False 时补读后继续（不截断）
        5. 超过 snapshot_max_enumeration_seconds 则截断（ENUM_TIMEOUT_METRIC）
        6. 8-5: alive_membership_state == "left_alive" 的条目被跳过

        Args:
            redis: Redis 客户端。
            shard: 分片 id。
            now_ms: 当前时间（epoch ms）。
            cutover_seq: 该分片的 published_seq（last_delta_seq=None 时退回）。

        Returns:
            该分片内 alive AIC 的 AliveDeltaEnvelope 列表。
        """
        global _enum_timeout_count

        chunk_size = settings.heartbeat_snapshot_chunk_size
        max_enum_s = settings.heartbeat_snapshot_max_enumeration_seconds
        silence_threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000

        start_score = now_ms - silence_threshold_ms
        cursor_ms = start_score

        # (aic, score) 元组列表，tie-safe 后合并
        all_tuples: list[tuple[str, int]] = []

        t_start = time.monotonic()

        while True:
            # 超时检查（8-3 前：不影响 is_last_page 判定）
            elapsed = time.monotonic() - t_start
            if elapsed >= max_enum_s:
                _enum_timeout_count += 1
                logger.warning(
                    "snapshot enumeration timeout — truncating",
                    shard=shard,
                    elapsed_s=elapsed,
                    collected=len(all_tuples),
                    metric=ENUM_TIMEOUT_METRIC,
                )
                break

            chunk = await store.zrange_by_score(
                redis,
                shard,
                lower=str(cursor_ms),
                upper="+inf",
                limit=chunk_size,
            )

            if not chunk:
                break

            # 8-3: termination check BEFORE tie-safe read
            is_last_page = len(chunk) < chunk_size
            last_score = chunk[-1][1]

            # C-SYNC-2: tie-safe 补读同 score 整组
            tie_group = await store.zrange_score_group(redis, shard, last_score)
            already = {a for a, _ in chunk}
            merged = list(chunk)
            for aic, score in tie_group:
                if aic not in already:
                    merged.append((aic, score))

            all_tuples.extend(merged)

            if is_last_page:
                break

            # 8-4: 非末页：继续读（cursor 推进至 last_score + 1，跳过已读 score 组）
            cursor_ms = last_score + 1

        if not all_tuples:
            return []

        # 批量取快照字段（pipeline HMGET）
        aics = [a for a, _ in all_tuples]
        rows = await store.mget_snapshot_fields(redis, shard, aics)

        envelopes: list[AliveDeltaEnvelope] = []
        for row in rows:
            if row is None:
                continue
            # 8-5: 过滤 left_alive（弱快照不变式）
            if row.alive_membership_state == "left_alive":
                continue

            seq_val = row.last_delta_seq if row.last_delta_seq is not None else cutover_seq
            seq_str = seq_to_str(seq_val)

            source_ts: str | None = None
            if row.source_timestamp_ms is not None:
                source_ts = _ms_to_iso(row.source_timestamp_ms)

            payload = AliveSetEntry(
                aic=row.aic,
                last_seen_at=row.last_seen_at or "",
                source_timestamp=source_ts,
            )
            env = AliveDeltaEnvelope(
                shard=shard,
                seq=seq_str,
                type=ALIVE_DELTA_TYPE,
                id=alive_object_id(row.aic),
                version=seq_str,
                op="upsert",
                kind="snapshot",
                payload=payload,
            )
            envelopes.append(env)

        return envelopes
