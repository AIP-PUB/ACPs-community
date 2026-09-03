"""tests: PostgresAliveSyncStore unit tests（in-memory SQLite）。

使用 aiosqlite 内存数据库避免依赖真实 PostgreSQL，验证 store 的
replace_alive_set / apply_upsert / apply_delete / reset / load_* 语义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from acps_sdk.amp.alive_sync.store import AliveRecord, ShardCheckpoint
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.heartbeat_sync.store as store_module

# 导入 model 确保表注册，不调用 SQLModel.metadata.create_all（避免 JSONB 不兼容 SQLite）
from app.heartbeat_sync import model as _alive_model  # noqa: F401
from app.heartbeat_sync.store import PostgresAliveSyncStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

_TEST_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def store(monkeypatch: pytest.MonkeyPatch) -> Any:
    """返回一个连接内存 SQLite 的 PostgresAliveSyncStore 实例。"""
    from app.heartbeat_sync.model import AgentAliveStatus, AliveSyncShardState

    engine = create_async_engine(_TEST_URL, echo=False)
    async with engine.begin() as conn:
        # 只创建 alive sync 两张表，不整体 create_all（避免 JSONB 不兼容 SQLite）
        await conn.run_sync(AgentAliveStatus.__table__.create)  # type: ignore[attr-defined]
        await conn.run_sync(AliveSyncShardState.__table__.create)  # type: ignore[attr-defined]

    test_session = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store_module, "AsyncSessionLocal", test_session)
    s = PostgresAliveSyncStore()
    yield s
    async with engine.begin() as conn:
        await conn.run_sync(AliveSyncShardState.__table__.drop)  # type: ignore[attr-defined]
        await conn.run_sync(AgentAliveStatus.__table__.drop)  # type: ignore[attr-defined]
    await engine.dispose()


async def _make_records(*aics: str, shard: str = "hb-000") -> AsyncIterable[AliveRecord]:
    for seq, aic in enumerate(aics, start=1):
        yield AliveRecord(aic=aic, alive=True, last_seen_at="2026-06-13T01:00:00Z", version=seq, shard=shard)


class TestReplaceAliveSet:
    @pytest.mark.asyncio
    async def test_inserts_records_and_checkpoints(self, store) -> None:
        checkpoints = [
            ShardCheckpoint(
                shard="hb-000", last_seen_seq=3, cutover_seq=3, kafka_next_offset=None, snapshot_generated_at=None
            )
        ]
        await store.replace_alive_set(_make_records("AIC-001", "AIC-002", "AIC-003"), checkpoints)
        views = await store.load_alive_views(["AIC-001", "AIC-002"])
        assert len(views) == 2
        assert views["AIC-001"].alive is True

    @pytest.mark.asyncio
    async def test_replaces_existing_data(self, store) -> None:
        cp = [
            ShardCheckpoint(
                shard="hb-000", last_seen_seq=1, cutover_seq=1, kafka_next_offset=None, snapshot_generated_at=None
            )
        ]
        await store.replace_alive_set(_make_records("AIC-OLD"), cp)
        cp2 = [
            ShardCheckpoint(
                shard="hb-000", last_seen_seq=5, cutover_seq=5, kafka_next_offset=None, snapshot_generated_at=None
            )
        ]
        await store.replace_alive_set(_make_records("AIC-NEW"), cp2)
        views = await store.load_alive_views(["AIC-OLD", "AIC-NEW"])
        assert "AIC-OLD" not in views
        assert "AIC-NEW" in views

    @pytest.mark.asyncio
    async def test_checkpoints_loaded_after_replace(self, store) -> None:
        cps = [
            ShardCheckpoint(
                shard="hb-000",
                last_seen_seq=10,
                cutover_seq=10,
                kafka_next_offset=55,
                snapshot_generated_at="2026-06-13T01:00:00Z",
            )
        ]
        await store.replace_alive_set(_make_records("AIC-001"), cps)
        loaded = await store.load_checkpoints()
        assert len(loaded) == 1
        assert loaded[0].last_seen_seq == 10
        assert loaded[0].kafka_next_offset == 55


class TestApplyUpsert:
    @pytest.mark.asyncio
    async def test_inserts_new_record(self, store) -> None:
        record = AliveRecord(aic="AIC-001", alive=True, last_seen_at="2026-06-13T01:00:00Z", version=1, shard="hb-000")
        await store.apply_upsert(record=record, shard="hb-000", last_seen_seq=1, kafka_next_offset=None)
        views = await store.load_alive_views(["AIC-001"])
        assert views["AIC-001"].alive is True

    @pytest.mark.asyncio
    async def test_updates_existing_record(self, store) -> None:
        r1 = AliveRecord(aic="AIC-001", alive=True, last_seen_at="2026-06-13T01:00:00Z", version=1, shard="hb-000")
        await store.apply_upsert(record=r1, shard="hb-000", last_seen_seq=1, kafka_next_offset=None)
        r2 = AliveRecord(aic="AIC-001", alive=True, last_seen_at="2026-06-13T02:00:00Z", version=5, shard="hb-000")
        await store.apply_upsert(record=r2, shard="hb-000", last_seen_seq=5, kafka_next_offset=None)
        views = await store.load_alive_views(["AIC-001"])
        assert views["AIC-001"].last_seen_at == "2026-06-13T02:00:00Z"
        cps = await store.load_checkpoints()
        assert cps[0].last_seen_seq == 5


class TestApplyDelete:
    @pytest.mark.asyncio
    async def test_marks_alive_false(self, store) -> None:
        r = AliveRecord(aic="AIC-001", alive=True, last_seen_at=None, version=3, shard="hb-000")
        await store.apply_upsert(record=r, shard="hb-000", last_seen_seq=3, kafka_next_offset=None)
        await store.apply_delete(aic="AIC-001", shard="hb-000", last_seen_seq=4, kafka_next_offset=None, version=4)
        views = await store.load_alive_views(["AIC-001"])
        assert views["AIC-001"].alive is False

    @pytest.mark.asyncio
    async def test_preserves_row_not_removed(self, store) -> None:
        r = AliveRecord(aic="AIC-001", alive=True, last_seen_at=None, version=3, shard="hb-000")
        await store.apply_upsert(record=r, shard="hb-000", last_seen_seq=3, kafka_next_offset=None)
        await store.apply_delete(aic="AIC-001", shard="hb-000", last_seen_seq=4, kafka_next_offset=None, version=4)
        # 行保留，alive=False
        lv = await store.load_local_versions()
        assert "AIC-001" in lv
        assert lv["AIC-001"] == 4


class TestReset:
    @pytest.mark.asyncio
    async def test_clears_both_tables_in_single_transaction(self, store) -> None:
        cps = [
            ShardCheckpoint(
                shard="hb-000", last_seen_seq=5, cutover_seq=5, kafka_next_offset=None, snapshot_generated_at=None
            )
        ]
        await store.replace_alive_set(_make_records("AIC-001", "AIC-002"), cps)
        await store.reset()
        views = await store.load_alive_views(["AIC-001", "AIC-002"])
        checkpoints = await store.load_checkpoints()
        assert len(views) == 0
        assert len(checkpoints) == 0
