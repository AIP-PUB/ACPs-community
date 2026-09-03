"""discovery-server PostgreSQL 实现的 AliveSyncStore。

实现 SDK 定义的 AliveSyncStore Protocol，通过 SQLModel / AsyncSession 持久化
alive 状态与 checkpoint，所有写操作均在单事务内保证原子性（C-SYNC-6 / §9.3）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from acps_sdk.amp.alive_sync.models import AliveView
from acps_sdk.amp.alive_sync.store import AliveRecord, ShardCheckpoint
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal
from app.heartbeat_sync.model import AgentAliveStatus, AliveSyncShardState

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

SNAPSHOT_BATCH_ROWS = 500  # 流式写入 snapshot 行的批次大小（内部常量）


class PostgresAliveSyncStore:
    """PostgreSQL 实现，满足 AliveSyncStore Protocol 结构子类型。

    所有写方法均在 `AsyncSessionLocal()` 的单事务内完成，并在方法内 commit，
    保证 alive 状态与 checkpoint 不分离（C-SYNC-6）。
    """

    # ── 查询（AliveReader 接口）──────────────────────────────────────────────

    async def load_alive_views(self, aics: Sequence[str]) -> dict[str, AliveView]:
        """按 AIC 批量查询 alive 视图（查询 API 用）。"""
        if not aics:
            return {}
        async with AsyncSessionLocal() as session:
            stmt = select(AgentAliveStatus).where(AgentAliveStatus.aic.in_(aics))  # type: ignore[attr-defined]
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return {row.aic: AliveView(aic=row.aic, alive=row.alive, last_seen_at=row.last_seen_at) for row in rows}

    # ── checkpoint 读取（引擎 hydrate 用）────────────────────────────────────

    async def load_checkpoints(self) -> list[ShardCheckpoint]:
        """读取所有 shard 的持久化 checkpoint。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AliveSyncShardState))
            rows = result.scalars().all()
        return [
            ShardCheckpoint(
                shard=row.shard,
                last_seen_seq=row.last_seen_seq,
                cutover_seq=row.cutover_seq,
                kafka_next_offset=row.kafka_next_offset,
                snapshot_generated_at=row.snapshot_generated_at,
            )
            for row in rows
        ]

    async def load_local_versions(self) -> dict[str, int]:
        """读取所有 alive 行的 {aic: version} 映射。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AgentAliveStatus.aic, AgentAliveStatus.version))  # type: ignore[call-overload]
            rows = result.all()
        return {row[0]: row[1] for row in rows}

    # ── 全量替换（snapshot 应用）──────────────────────────────────────────────

    async def replace_alive_set(
        self,
        records: AsyncIterable[AliveRecord],
        checkpoints: list[ShardCheckpoint],
    ) -> None:
        """原子替换全量 alive 集合（snapshot 应用）。

        单事务内：先清空两张表，再流式分批写入 records 与 checkpoints。
        整体替换语义；提交前并发查询仍读到旧集合（快照隔离）。
        """
        async with AsyncSessionLocal() as session:
            # 1. 原子清空两张表
            await session.execute(delete(AgentAliveStatus))
            await session.execute(delete(AliveSyncShardState))

            # 2. 流式分批写入 alive 行
            batch: list[AgentAliveStatus] = []
            async for record in records:
                batch.append(
                    AgentAliveStatus(
                        aic=record.aic,
                        alive=record.alive,
                        last_seen_at=record.last_seen_at,
                        version=record.version,
                        shard=record.shard,
                    )
                )
                if len(batch) >= SNAPSHOT_BATCH_ROWS:
                    session.add_all(batch)
                    batch.clear()
            if batch:
                session.add_all(batch)

            # 3. 写入 checkpoint 行
            for cp in checkpoints:
                session.add(
                    AliveSyncShardState(
                        shard=cp.shard,
                        last_seen_seq=cp.last_seen_seq,
                        cutover_seq=cp.cutover_seq,
                        kafka_next_offset=cp.kafka_next_offset,
                        snapshot_generated_at=cp.snapshot_generated_at,
                    )
                )

            await session.commit()

    # ── delta 应用（enter_alive / refresh_alive）────────────────────────────

    async def apply_upsert(
        self,
        record: AliveRecord,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
    ) -> None:
        """应用 enter_alive / refresh_alive：upsert alive 行 + 推进 checkpoint（单事务）。"""
        async with AsyncSessionLocal() as session:
            # 按 aic 查找已有行
            stmt = select(AgentAliveStatus).where(AgentAliveStatus.aic == record.aic)  # type: ignore[arg-type]
            result = await session.execute(stmt)
            existing = result.scalars().first()

            if existing is None:
                session.add(
                    AgentAliveStatus(
                        aic=record.aic,
                        alive=record.alive,
                        last_seen_at=record.last_seen_at,
                        version=record.version,
                        shard=record.shard,
                    )
                )
            else:
                existing.alive = record.alive
                existing.last_seen_at = record.last_seen_at
                existing.version = record.version
                existing.shard = record.shard
                session.add(existing)

            # upsert checkpoint
            await _upsert_checkpoint(session, shard, last_seen_seq, kafka_next_offset)
            await session.commit()

    async def apply_delete(
        self,
        aic: str,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
        version: int,
    ) -> None:
        """应用 leave_alive：置 alive=False（保留行与 version），推进 checkpoint（单事务）。"""
        async with AsyncSessionLocal() as session:
            stmt = select(AgentAliveStatus).where(AgentAliveStatus.aic == aic)  # type: ignore[arg-type]
            result = await session.execute(stmt)
            row = result.scalars().first()
            if row is not None:
                row.alive = False
                row.version = version
                session.add(row)

            await _upsert_checkpoint(session, shard, last_seen_seq, kafka_next_offset)
            await session.commit()

    # ── 重同步前清空（reset）────────────────────────────────────────────────

    async def reset(self) -> None:
        """单事务清空两张表（agent_alive_status + alive_sync_shard_state）。"""
        async with AsyncSessionLocal() as session:
            await session.execute(delete(AgentAliveStatus))
            await session.execute(delete(AliveSyncShardState))
            await session.commit()


# ── 内部辅助 ─────────────────────────────────────────────────────────────────


async def _upsert_checkpoint(
    session: AsyncSession,
    shard: str,
    last_seen_seq: int,
    kafka_next_offset: int | None,
) -> None:
    """在已开启事务的 session 中 upsert 指定 shard 的 checkpoint。"""
    stmt = select(AliveSyncShardState).where(AliveSyncShardState.shard == shard)  # type: ignore[arg-type]
    result = await session.execute(stmt)
    cp_row = result.scalars().first()
    if cp_row is None:
        session.add(
            AliveSyncShardState(
                shard=shard,
                last_seen_seq=last_seen_seq,
                cutover_seq=0,
                kafka_next_offset=kafka_next_offset,
            )
        )
    else:
        cp_row.last_seen_seq = last_seen_seq
        if kafka_next_offset is not None:
            cp_row.kafka_next_offset = kafka_next_offset
        session.add(cp_row)
