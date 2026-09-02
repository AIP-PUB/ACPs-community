"""AMP alive-sync 存储抽象层。

定义了存储层的数据类与 Protocol，使引擎（engine.py）完全不依赖任何具体存储实现。
宿主项目（如 discovery-server）提供 AliveSyncStore 的具体实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterable, Protocol, Sequence, runtime_checkable

from acps_sdk.amp.alive_sync.models import AliveView


@dataclass(frozen=True)
class AliveRecord:
    """写入存储的 alive 状态行（来自 snapshot 行或 delta 事件）。"""

    aic: str
    alive: bool
    last_seen_at: str | None
    version: int
    shard: str


@dataclass(frozen=True)
class ShardCheckpoint:
    """per-shard 持久化 checkpoint，用于续跑与自举。"""

    shard: str
    last_seen_seq: int
    cutover_seq: int
    kafka_next_offset: int | None
    snapshot_generated_at: str | None


@runtime_checkable
class AliveReader(Protocol):
    """只读接口：按 AIC 批量查询 alive 视图。

    供查询侧（holder → enrichment）使用，无写能力。
    """

    async def load_alive_views(self, aics: Sequence[str]) -> dict[str, AliveView]:
        """批量查询指定 AIC 的 alive 视图。

        不存在于存储中的 AIC 不出现在结果字典中（键缺失 = 未知，非 not-alive）。
        """
        ...


@runtime_checkable
class AliveSyncStore(AliveReader, Protocol):
    """读写接口：引擎用于持久化 alive 状态与 checkpoint。

    继承 AliveReader，实现方只需实现一次 load_alive_views。
    """

    async def replace_alive_set(
        self,
        records: AsyncIterable[AliveRecord],
        checkpoints: list[ShardCheckpoint],
    ) -> None:
        """原子替换全量 alive 集合（snapshot 应用）。

        须在单事务内：先清空两张表，再流式写入 records 与 checkpoints。
        """
        ...

    async def apply_upsert(
        self,
        record: AliveRecord,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
    ) -> None:
        """应用 enter_alive / refresh_alive delta：写入 alive 行并推进 checkpoint。

        alive 行与 checkpoint 须在同一事务内提交（C-SYNC-6 §9.3）。
        """
        ...

    async def apply_delete(
        self,
        aic: str,
        shard: str,
        last_seen_seq: int,
        kafka_next_offset: int | None,
        version: int,
    ) -> None:
        """应用 leave_alive delta：置 alive=False（保留行与 version），推进 checkpoint。

        alive 行与 checkpoint 须在同一事务内提交（C-SYNC-5/6）。
        """
        ...

    async def load_checkpoints(self) -> list[ShardCheckpoint]:
        """读取所有 shard 的持久化 checkpoint（用于 hydrate 续跑）。"""
        ...

    async def load_local_versions(self) -> dict[str, int]:
        """读取所有 alive 行的 {aic: version} 映射（用于 hydrate 内存 localVersion）。"""
        ...

    async def reset(self) -> None:
        """单事务清空两张表（agent_alive_status + alive_sync_shard_state）。"""
        ...
