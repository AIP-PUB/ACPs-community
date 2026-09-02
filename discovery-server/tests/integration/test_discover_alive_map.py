from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

import app.discovery.discovery_api as discovery_api_module
import app.heartbeat_sync.holder as holder_module
import app.heartbeat_sync.store as alive_store_module
from app.discovery.schema import (
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
    DiscoveryRequest,
    DiscoveryResponse,
    DiscoveryResult,
)
from app.heartbeat_sync.store import PostgresAliveSyncStore

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _seed_alive_rows(test_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with test_session_factory() as session:
        await session.execute(text("DELETE FROM alive_sync_shard_state"))
        await session.execute(text("DELETE FROM agent_alive_status"))
        await session.execute(
            text(
                """
                INSERT INTO agent_alive_status (aic, alive, last_seen_at, version, shard)
                VALUES
                    (:aic1, :alive1, :seen1, :version1, :shard1),
                    (:aic2, :alive2, :seen2, :version2, :shard2)
                """
            ),
            {
                "aic1": "AIC-ALIVE-1",
                "alive1": True,
                "seen1": "2026-06-13T01:20:00Z",
                "version1": 10,
                "shard1": "hb-000",
                "aic2": "AIC-LEAVE-1",
                "alive2": False,
                "seen2": None,
                "version2": 11,
                "shard2": "hb-000",
            },
        )
        await session.commit()


def _build_local_discovery_response() -> DiscoveryResponse:
    result = DiscoveryResult(
        acsMap={
            "AIC-ALIVE-1": {"aic": "AIC-ALIVE-1"},
            "AIC-LEAVE-1": {"aic": "AIC-LEAVE-1"},
            "AIC-UNKNOWN-1": {"aic": "AIC-UNKNOWN-1"},
        },
        agents=[
            DiscoveryAgentGroup(
                group="default",
                agent_skills=[
                    DiscoveryAgentSkill(aic="AIC-ALIVE-1", skill_id="s1", ranking=1),
                    DiscoveryAgentSkill(aic="AIC-LEAVE-1", skill_id="s2", ranking=2),
                    DiscoveryAgentSkill(aic="AIC-UNKNOWN-1", skill_id="s3", ranking=3),
                ],
            )
        ],
        routes=[],
    )
    result._alive_enrichable = True
    return DiscoveryResponse.success(result=result)


async def test_discover_injects_alive_map_from_local_store(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_alive_rows(test_session_factory)

    async def fake_discover_request(
        request: DiscoveryRequest,
        *,
        runtime: object | None = None,
    ) -> DiscoveryResponse:
        del request, runtime
        return _build_local_discovery_response()

    monkeypatch.setattr(discovery_api_module, "discover_request", fake_discover_request)
    monkeypatch.setattr(alive_store_module, "AsyncSessionLocal", test_session_factory)
    holder_module.set_alive_reader(PostgresAliveSyncStore())
    try:
        response = await client.post("/acps-adp-v2/discover", json={"type": "trending", "limit": 3})
        assert response.status_code == 200
        payload = response.json()["result"]

        alive_map = payload.get("aliveMap")
        assert isinstance(alive_map, dict)
        assert alive_map["AIC-ALIVE-1"]["alive"] is True
        assert alive_map["AIC-LEAVE-1"]["alive"] is False
        assert "aliveLastSeenAt" in alive_map["AIC-LEAVE-1"]
        assert alive_map["AIC-LEAVE-1"]["aliveLastSeenAt"] is None
        assert "AIC-UNKNOWN-1" not in alive_map
    finally:
        holder_module.clear_alive_reader()


async def test_discover_without_alive_reader_has_no_alive_map(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_discover_request(
        request: DiscoveryRequest,
        *,
        runtime: object | None = None,
    ) -> DiscoveryResponse:
        del request, runtime
        return _build_local_discovery_response()

    monkeypatch.setattr(discovery_api_module, "discover_request", fake_discover_request)
    holder_module.clear_alive_reader()

    response = await client.post("/acps-adp-v2/discover", json={"type": "trending", "limit": 3})

    assert response.status_code == 200
    payload = response.json()["result"]
    assert "aliveMap" not in payload
