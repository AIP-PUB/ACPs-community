from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from acps_sdk.amp.alive_sync.store import AliveRecord, ShardCheckpoint
from sqlalchemy import text

import app.heartbeat_sync.store as store_module
from app.heartbeat_sync.store import PostgresAliveSyncStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _clear_alive_tables(test_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with test_session_factory() as session:
        await session.execute(text("DELETE FROM alive_sync_shard_state"))
        await session.execute(text("DELETE FROM agent_alive_status"))
        await session.commit()


@pytest_asyncio.fixture
async def alive_store(
    monkeypatch: pytest.MonkeyPatch,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[PostgresAliveSyncStore]:
    await _clear_alive_tables(test_session_factory)
    monkeypatch.setattr(store_module, "AsyncSessionLocal", test_session_factory)
    try:
        yield PostgresAliveSyncStore()
    finally:
        await _clear_alive_tables(test_session_factory)


async def _records(*aics: str, shard: str = "hb-000") -> AsyncIterable[AliveRecord]:
    for version, aic in enumerate(aics, start=1):
        yield AliveRecord(
            aic=aic,
            alive=True,
            last_seen_at="2026-06-13T01:00:00Z",
            version=version,
            shard=shard,
        )


async def test_replace_alive_set_replaces_rows_and_checkpoints(alive_store: PostgresAliveSyncStore) -> None:
    checkpoints = [
        ShardCheckpoint(
            shard="hb-000",
            last_seen_seq=3,
            cutover_seq=3,
            kafka_next_offset=10,
            snapshot_generated_at="2026-06-13T01:00:00Z",
        )
    ]
    await alive_store.replace_alive_set(_records("AIC-001", "AIC-002"), checkpoints)

    first_views = await alive_store.load_alive_views(["AIC-001", "AIC-002"])
    assert set(first_views.keys()) == {"AIC-001", "AIC-002"}
    assert first_views["AIC-001"].alive is True

    next_checkpoints = [
        ShardCheckpoint(
            shard="hb-000",
            last_seen_seq=5,
            cutover_seq=5,
            kafka_next_offset=20,
            snapshot_generated_at="2026-06-13T02:00:00Z",
        )
    ]
    await alive_store.replace_alive_set(_records("AIC-003"), next_checkpoints)

    second_views = await alive_store.load_alive_views(["AIC-001", "AIC-003"])
    assert "AIC-001" not in second_views
    assert "AIC-003" in second_views

    loaded = await alive_store.load_checkpoints()
    assert len(loaded) == 1
    assert loaded[0].last_seen_seq == 5
    assert loaded[0].kafka_next_offset == 20


async def test_apply_upsert_and_delete_update_checkpoint(alive_store: PostgresAliveSyncStore) -> None:
    await alive_store.apply_upsert(
        record=AliveRecord(
            aic="AIC-001",
            alive=True,
            last_seen_at="2026-06-13T01:00:00Z",
            version=1,
            shard="hb-000",
        ),
        shard="hb-000",
        last_seen_seq=1,
        kafka_next_offset=2,
    )
    await alive_store.apply_delete(
        aic="AIC-001",
        shard="hb-000",
        last_seen_seq=2,
        kafka_next_offset=3,
        version=2,
    )

    views = await alive_store.load_alive_views(["AIC-001"])
    assert views["AIC-001"].alive is False

    versions = await alive_store.load_local_versions()
    assert versions["AIC-001"] == 2

    checkpoints = await alive_store.load_checkpoints()
    assert len(checkpoints) == 1
    assert checkpoints[0].last_seen_seq == 2
    assert checkpoints[0].kafka_next_offset == 3


async def test_reset_clears_alive_tables(alive_store: PostgresAliveSyncStore) -> None:
    checkpoints = [
        ShardCheckpoint(
            shard="hb-000",
            last_seen_seq=1,
            cutover_seq=1,
            kafka_next_offset=None,
            snapshot_generated_at="2026-06-13T01:00:00Z",
        )
    ]
    await alive_store.replace_alive_set(_records("AIC-001"), checkpoints)
    await alive_store.reset()

    views = await alive_store.load_alive_views(["AIC-001"])
    loaded = await alive_store.load_checkpoints()
    assert views == {}
    assert loaded == []
