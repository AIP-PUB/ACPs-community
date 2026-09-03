"""AMP alive-sync Consumer 引擎核心。

本模块分三层：
  1. 纯判定函数（无状态、无 I/O）—— 最易单测
  2. DeltaDecision / classify_delta —— 纯决策核
  3. AliveSyncEngine —— 有状态编排（持 in-memory 水位），持久化委托 AliveSyncStore
"""
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, AsyncIterable

from acps_sdk.amp.heartbeat_sync import (
    AliveDeltaEnvelope,
    AliveSnapshotMeta,
    aic_from_object_id,
    seq_from_str,
)

from acps_sdk.amp.alive_sync.errors import GapDetectedError, ResyncRequired
from acps_sdk.amp.alive_sync.store import AliveRecord, AliveSyncStore, ShardCheckpoint

if TYPE_CHECKING:
    pass


# ── 1. 纯判定函数 ────────────────────────────────────────────────────────────


def passes_seq_gate(seq: int, last_seen_seq: int) -> bool:
    """seq 闸门：seq > lastSeenSeq 才放行（§7.5 第 7 条第一道去重）。"""
    return seq > last_seen_seq


def is_gap(seq: int, last_seen_seq: int) -> bool:
    """缺口检测：seq > lastSeenSeq + 1（§7.5 第 7 条；调用前已过 seq 闸门）。"""
    return seq > last_seen_seq + 1


def passes_version(version: int, local_version: int | None) -> bool:
    """版本幂等检查（第二道去重）：localVersion 缺失或 version 更大才应用（§7.5 第 2 条）。"""
    return local_version is None or version > local_version


# ── 2. DeltaDecision + classify_delta ───────────────────────────────────────


class DeltaDecision(Enum):
    """引擎对单条 delta 的处置决策。"""

    APPLY_UPSERT = auto()
    APPLY_DELETE = auto()
    SKIP_SEQ_GATE = auto()  # seq ≤ lastSeenSeq，重复投递
    SKIP_VERSION = auto()   # 通过闸门但 version ≤ localVersion（snapshot 行更新）
    GAP = auto()            # seq > lastSeenSeq + 1，需触发重同步


def classify_delta(
    env: AliveDeltaEnvelope,
    last_seen_seq: int,
    local_version: int | None,
) -> DeltaDecision:
    """纯决策核：根据信封内容与当前水位返回处置决策。

    决策流程（§7.5 伪代码落地）：
      1. seq_from_str(env.seq) → seq（数值化，C-SYNC 第 5 条禁止字典序）
      2. passes_seq_gate → False → SKIP_SEQ_GATE
      3. is_gap → True → GAP
      4. passes_version → False → SKIP_VERSION
      5. env.op == "upsert" → APPLY_UPSERT，否则 APPLY_DELETE

    所有 seq / version 比较均经 seq_from_str 数值化，禁止字符串字典序。
    """
    seq = seq_from_str(env.seq)
    version = seq_from_str(env.version)

    if not passes_seq_gate(seq, last_seen_seq):
        return DeltaDecision.SKIP_SEQ_GATE

    if is_gap(seq, last_seen_seq):
        return DeltaDecision.GAP

    if not passes_version(version, local_version):
        return DeltaDecision.SKIP_VERSION

    return DeltaDecision.APPLY_UPSERT if env.op == "upsert" else DeltaDecision.APPLY_DELETE


# ── 3. AliveSyncEngine ───────────────────────────────────────────────────────


class AliveSyncEngine:
    """有状态 alive-sync Consumer 引擎。

    内存维护 per-shard `_last_seen_seq` 与 per-aic `_local_version`，持久化委托 store。
    引擎本身不做 HTTP / Kafka I/O，恢复策略由上层 AliveSyncService 决定。
    """

    def __init__(self, store: AliveSyncStore) -> None:
        self._store = store
        self._last_seen_seq: dict[str, int] = {}
        self._local_version: dict[str, int] = {}
        self._cutover_seq: dict[str, int] = {}

    async def hydrate(self) -> None:
        """启动续跑：从 store 读取 checkpoint 与 localVersion 恢复内存水位。

        若 checkpoint 不存在或无法证明连续性则抛 ResyncRequired。
        """
        checkpoints = await self._store.load_checkpoints()
        if not checkpoints:
            raise ResyncRequired(reason="no_checkpoints")

        self._last_seen_seq = {cp.shard: cp.last_seen_seq for cp in checkpoints}
        self._cutover_seq = {cp.shard: cp.cutover_seq for cp in checkpoints}
        self._local_version = await self._store.load_local_versions()

    async def apply_snapshot(
        self,
        meta: AliveSnapshotMeta,
        rows: AsyncIterable[AliveDeltaEnvelope],
    ) -> None:
        """原子应用全量 snapshot。

        流程：
          1. 把 rows 包装成「边转 AliveRecord 边旁记 version」的异步生成器
          2. 构造各 shard ShardCheckpoint（last_seen_seq=cutover_seq=cutover）
          3. 调 store.replace_alive_set(gen, checkpoints)：单事务原子替换
          4. 仅事务提交成功后才重置内存 _last_seen_seq 与 _local_version
          （中途异常则内存维持原状，service 下一轮重新 bootstrap）
        """
        cutover_by_shard = {
            shard: seq_from_str(seq_str)
            for shard, seq_str in meta.cutover_seq_by_shard.items()
        }

        new_local_versions: dict[str, int] = {}

        async def _record_gen() -> AsyncIterable[AliveRecord]:
            async for env in rows:
                aic = aic_from_object_id(env.id)
                version = seq_from_str(env.version)
                new_local_versions[aic] = version
                yield AliveRecord(
                    aic=aic,
                    alive=True,
                    last_seen_at=env.payload.last_seen_at if env.payload else None,
                    version=version,
                    shard=env.shard,
                )

        checkpoints = [
            ShardCheckpoint(
                shard=shard,
                last_seen_seq=cutover,
                cutover_seq=cutover,
                kafka_next_offset=None,
                snapshot_generated_at=meta.generated_at,
            )
            for shard, cutover in cutover_by_shard.items()
        ]

        await self._store.replace_alive_set(_record_gen(), checkpoints)

        # 仅事务提交后才更新内存水位
        self._last_seen_seq = dict(cutover_by_shard)
        self._cutover_seq = dict(cutover_by_shard)
        self._local_version = new_local_versions

    async def apply_delta(
        self,
        env: AliveDeltaEnvelope,
        *,
        kafka_next_offset: int | None = None,
    ) -> DeltaDecision:
        """应用单条 delta 事件并返回决策。

        - GAP → 抛 GapDetectedError（由 AliveSyncService 决定恢复策略）
        - APPLY_UPSERT/APPLY_DELETE → 调 store 原子持久化，然后推进内存水位
        - SKIP_* → 仅推进内存 _last_seen_seq（不写库，持久化滞后是安全方向）
        """
        shard = env.shard
        last_seen = self._last_seen_seq.get(shard, 0)
        aic = aic_from_object_id(env.id)
        local_ver = self._local_version.get(aic)

        decision = classify_delta(env, last_seen, local_ver)
        seq = seq_from_str(env.seq)
        version = seq_from_str(env.version)

        if decision is DeltaDecision.GAP:
            raise GapDetectedError(
                shard=shard,
                expected_seq=last_seen + 1,
                got_seq=seq,
            )

        if decision is DeltaDecision.APPLY_UPSERT:
            record = AliveRecord(
                aic=aic,
                alive=True,
                last_seen_at=env.payload.last_seen_at if env.payload else None,
                version=version,
                shard=shard,
            )
            await self._store.apply_upsert(
                record=record,
                shard=shard,
                last_seen_seq=seq,
                kafka_next_offset=kafka_next_offset,
            )
            self._last_seen_seq[shard] = seq
            self._local_version[aic] = version

        elif decision is DeltaDecision.APPLY_DELETE:
            await self._store.apply_delete(
                aic=aic,
                shard=shard,
                last_seen_seq=seq,
                kafka_next_offset=kafka_next_offset,
                version=version,
            )
            self._last_seen_seq[shard] = seq
            # 保留 _local_version[aic] = version（「保留+自然有界」方案，§5.4）
            self._local_version[aic] = version

        else:
            # SKIP_SEQ_GATE / SKIP_VERSION：仅推进内存 seq（不写库）
            self._last_seen_seq[shard] = max(self._last_seen_seq.get(shard, 0), seq)

        return decision

    def cutover_seq_by_shard(self) -> dict[str, int]:
        """暴露当前 cutover seq 映射，供 Kafka 自举定位 offset（§7.5 第 8 条）。"""
        return dict(self._cutover_seq)
