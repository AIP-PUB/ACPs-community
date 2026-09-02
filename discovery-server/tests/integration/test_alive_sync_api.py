from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_alive_sync_status_returns_not_running_when_service_absent(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.heartbeat_sync.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_service", None)

    response = await client.get("/admin/alive-sync/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert "message" in payload


async def test_alive_sync_status_returns_service_payload(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.heartbeat_sync.runtime as runtime_module

    fake_status = {
        "running": True,
        "aliveCount": 2,
        "checkpointCount": 1,
        "shards": {
            "hb-000": {
                "lastSeenSeq": 8,
                "cutoverSeq": 5,
                "kafkaNextOffset": 21,
                "snapshotGeneratedAt": "2026-06-13T01:00:00Z",
            }
        },
    }
    fake_service = SimpleNamespace(status=AsyncMock(return_value=fake_status))
    monkeypatch.setattr(runtime_module, "_service", fake_service)

    response = await client.get("/admin/alive-sync/status")

    assert response.status_code == 200
    assert response.json() == fake_status
    fake_service.status.assert_awaited_once_with()


async def test_alive_sync_resync_returns_503_when_service_absent(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.heartbeat_sync.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_service", None)

    response = await client.post("/admin/alive-sync/resync")

    assert response.status_code == 503


async def test_alive_sync_resync_calls_service(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.heartbeat_sync.runtime as runtime_module

    fake_service = SimpleNamespace(request_resync=AsyncMock(return_value=None))
    monkeypatch.setattr(runtime_module, "_service", fake_service)

    response = await client.post("/admin/alive-sync/resync")

    assert response.status_code == 200
    assert response.json()["message"] == "重同步已触发"
    fake_service.request_resync.assert_awaited_once_with("admin_manual_trigger")
