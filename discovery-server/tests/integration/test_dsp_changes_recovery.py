"""Exercise DSP page replay across a PostgreSQL transaction failure."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import select, text

from app.sync import client as sync_client_module
from app.sync.exception import SyncError, SyncOperationError
from app.sync.model import Agent, DSPState, Envelope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_retry_changes_page_after_database_rejects_second_row(
    test_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, third = sorted(("dsp-retry-1", "dsp-retry-2", "dsp-retry-3"))
    envelopes = [Envelope(seq=seq, type="acs", id=aic, version=1) for seq, aic in enumerate((first, second, third), 41)]
    requested: list[int] = []

    def registry(request: httpx.Request) -> httpx.Response:
        seq = int(request.url.params["seq"])
        requested.append(seq)
        if seq == 40:
            return httpx.Response(
                200, text="\n".join(item.model_dump_json() for item in envelopes), headers={"X-Next-Seq": "43"}
            )
        assert seq == 43
        return httpx.Response(204, headers={"X-Next-Seq": "45"})

    @asynccontextmanager
    async def session_context() -> AsyncIterator[AsyncSession]:
        async with test_session_factory() as session:
            yield session

    async def persisted_ids() -> list[str]:
        async with session_context() as session:
            result = await session.execute(
                select(Agent.aic).where(Agent.aic.in_((first, second, third))).order_by(Agent.aic)
            )
            return list(result.scalars())

    monkeypatch.setattr(sync_client_module, "get_async_session_context", session_context)
    client = sync_client_module.DSPClient("http://registry.example/acps-dsp-v2")
    client.state = DSPState(last_seq=40, needs_snapshot=False)

    async with httpx.AsyncClient(transport=httpx.MockTransport(registry)) as http:
        monkeypatch.setattr(client, "_get_http_client", lambda: http)
        async with test_session_factory.begin() as session:
            await session.execute(
                text("ALTER TABLE agents ADD CONSTRAINT dsp_replay_test CHECK (aic <> 'dsp-retry-2')")
            )
        try:
            with pytest.raises(SyncOperationError) as exc:
                await client.sync_once()
            assert exc.value.error_name == SyncError.DATABASE_ERROR
            assert await persisted_ids() == [first]
            assert client.state.last_seq == 40
        finally:
            async with test_session_factory.begin() as session:
                await session.execute(text("ALTER TABLE agents DROP CONSTRAINT dsp_replay_test"))

        await client.sync_once()

    assert await persisted_ids() == [first, second, third]
    assert client.state.last_seq == 45
    assert requested == [40, 40, 43]
