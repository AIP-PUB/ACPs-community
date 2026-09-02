"""测试夹具：FakeAliveSyncStore — 内存实现 AliveSyncStore Protocol，供引擎单测使用。"""
from __future__ import annotations

from typing import AsyncIterable, Sequence

import pytest

from acps_sdk.amp.alive_sync.models import AliveView
from acps_sdk.amp.alive_sync.store import AliveRecord, AliveSyncStore, ShardCheckpoint


class FakeAliveSyncStore:
    """基于字典的内存实现，满足 AliveSyncStore Protocol 的结构子类型。"""

    def __init__(self) -> None:
        self._rows: dict[str, AliveRecord] = {}
        self._checkpoints: dict[str, ShardCheckpoint] = {}

    async def load_alive_views(self, aics: Sequence[str]) -> dict[str, AliveView]:
        result: dict[str, AliveView] = {}
        for aic in aics:
            row = self._rows.get(aic)
            if row is not None:
                result[aic] = AliveView(
                    aic=row.aic,
                    alive=row.alive,
                    last_seen_at=row.last_seen_at,
                )
        return result

    async def replace_alive_set(
        self,
        records: AsyncIterable[AliveRecord],
        checkpoints: list[ShardCheckpoint],
    ) -> None:
        self._rows.clear()
        self._checkpoints.clear()
        async for record in records:
            self._rows[record.aic] = record
        for cp in checkpoints:
            self._checkpoints[cp.shard] = cp

    async def apply_upsert(
        self,
        record: AliveRecord,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
    ) -> None:
        self._rows[record.aic] = record
        old = self._checkpoints.get(shard)
        self._checkpoints[shard] = ShardCheckpoint(
            shard=shard,
            last_seen_seq=last_seen_seq,
            cutover_seq=old.cutover_seq if old else 0,
            kafka_next_offset=kafka_next_offset,
            snapshot_generated_at=old.snapshot_generated_at if old else None,
        )

    async def apply_delete(
        self,
        aic: str,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
        version: int,
    ) -> None:
        old_row = self._rows.get(aic)
        if old_row is not None:
            self._rows[aic] = AliveRecord(
                aic=old_row.aic,
                alive=False,
                last_seen_at=old_row.last_seen_at,
                version=version,
                shard=old_row.shard,
            )
        old = self._checkpoints.get(shard)
        self._checkpoints[shard] = ShardCheckpoint(
            shard=shard,
            last_seen_seq=last_seen_seq,
            cutover_seq=old.cutover_seq if old else 0,
            kafka_next_offset=kafka_next_offset,
            snapshot_generated_at=old.snapshot_generated_at if old else None,
        )

    async def load_checkpoints(self) -> list[ShardCheckpoint]:
        return list(self._checkpoints.values())

    async def load_local_versions(self) -> dict[str, int]:
        return {aic: row.version for aic, row in self._rows.items()}

    async def reset(self) -> None:
        self._rows.clear()
        self._checkpoints.clear()


# 验证 FakeAliveSyncStore 满足 AliveSyncStore 结构子类型
assert isinstance(FakeAliveSyncStore(), AliveSyncStore)


@pytest.fixture
def fake_store() -> FakeAliveSyncStore:
    return FakeAliveSyncStore()
