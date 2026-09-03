from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from app.sync import api as sync_api_module
from app.sync import service as sync_service_module
from app.sync.model import DSPState, RegistryInfo, WebhookNotification, WebhookResponse

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.core.dependencies import ServiceRuntime

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_dsp_client(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    client = SimpleNamespace(
        is_running=False,
        sync_interval=60,
        registry_base_url="https://registry.example.com/acps-dsp-v2",
        state=DSPState(
            last_seq=42,
            object_versions={"acs": {"agent.1": 1, "agent.2": 1}},
            last_sync_time=datetime.now(UTC),
            needs_snapshot=False,
        ),
        sync_once_calls=0,
        trigger_sync_once_calls=0,
        manual_sync_in_progress=False,
        manual_sync_error_text=None,
    )

    async def start_background_sync() -> None:
        await asyncio.sleep(0)
        client.is_running = True

    async def stop_background_sync() -> None:
        await asyncio.sleep(0)
        client.is_running = False

    async def sync_once() -> None:
        await asyncio.sleep(0)
        client.sync_once_calls += 1

    def trigger_sync_once() -> bool:
        client.trigger_sync_once_calls += 1
        return True

    def sync_task_in_progress() -> bool:
        return bool(client.manual_sync_in_progress)

    def manual_sync_error() -> str | None:
        return cast("str | None", client.manual_sync_error_text)

    async def get_registry_info() -> RegistryInfo:
        await asyncio.sleep(0)
        return RegistryInfo(
            service="registry-server",
            version="2.2.0",
            status="ok",
            supported_types=["acs"],
        )

    client.start_background_sync = start_background_sync
    client.stop_background_sync = stop_background_sync
    client.sync_once = sync_once
    client.trigger_sync_once = trigger_sync_once
    client.sync_task_in_progress = sync_task_in_progress
    client.manual_sync_error = manual_sync_error
    client.get_registry_info = get_registry_info

    monkeypatch.setattr(sync_api_module, "get_dsp_client", lambda: client)
    monkeypatch.setattr(sync_service_module, "get_dsp_client", lambda: client)
    return client


async def test_dsp_control_endpoints_update_runtime_state(
    client: AsyncClient,
    fake_dsp_client: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_hard_reset_sync_state(*, session: object | None = None, runtime: object | None = None) -> int:
        del session, runtime
        await asyncio.sleep(0)
        fake_dsp_client.state.last_seq = None
        fake_dsp_client.state.object_versions = {}
        fake_dsp_client.state.needs_snapshot = True
        return 5

    monkeypatch.setattr(sync_api_module, "hard_reset_sync_state", fake_hard_reset_sync_state)

    status_response = await client.get("/admin/dsp/status")
    start_response = await client.post("/admin/dsp/start")
    started_status_response = await client.get("/admin/dsp/status")
    sync_response = await client.post("/admin/dsp/sync")
    stop_response = await client.post("/admin/dsp/stop")
    stopped_status_response = await client.get("/admin/dsp/status")
    reset_response = await client.post("/admin/dsp/reset")
    hard_reset_response = await client.post("/admin/dsp/hard-reset")

    assert status_response.status_code == 200
    assert status_response.json()["last_seq"] == 42
    assert status_response.json()["is_running"] is False
    assert status_response.json()["manual_sync_in_progress"] is False

    assert start_response.status_code == 200
    assert start_response.json()["message"] == "DSP 同步启动成功"
    assert started_status_response.status_code == 200
    assert started_status_response.json()["is_running"] is True

    assert sync_response.status_code == 200
    assert sync_response.json()["message"] == "手动同步已触发"
    assert fake_dsp_client.trigger_sync_once_calls == 1
    assert fake_dsp_client.sync_once_calls == 0

    assert stop_response.status_code == 200
    assert stop_response.json()["message"] == "DSP 同步停止成功"
    assert stopped_status_response.status_code == 200
    assert stopped_status_response.json()["is_running"] is False

    assert reset_response.status_code == 200
    assert reset_response.json()["message"] == "DSP 同步状态重置成功"
    assert fake_dsp_client.state.last_seq is None
    assert fake_dsp_client.state.needs_snapshot is True

    assert hard_reset_response.status_code == 200
    assert "已清空 5 条Agent记录" in hard_reset_response.json()["message"]


async def test_dsp_status_distinguishes_background_runtime_from_manual_sync(
    client: AsyncClient,
    fake_dsp_client: SimpleNamespace,
) -> None:
    fake_dsp_client.is_running = True
    fake_dsp_client.manual_sync_in_progress = True
    fake_dsp_client.manual_sync_error_text = "registry unavailable"

    status_response = await client.get("/admin/dsp/status")

    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["is_running"] is True
    assert payload["manual_sync_in_progress"] is True
    assert payload["manual_sync_error"] == "registry unavailable"


async def test_registry_info_and_register_webhook_endpoints(
    client: AsyncClient,
    fake_dsp_client: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_dsp_client

    async def fake_register_webhook_with_registry(
        webhook_data: object,
        *,
        runtime: object | None = None,
        authorization_header: str | None = None,
    ) -> WebhookResponse:
        del webhook_data, runtime
        await asyncio.sleep(0)
        assert authorization_header == "Bearer admin-token"
        return WebhookResponse(
            id="wh_123",
            url="https://discovery.example.com/admin/dsp/webhooks/receive",
            types=["acs"],
            events=["data_change"],
            description="integration webhook",
            status="active",
            failure_count=0,
            last_triggered_at=None,
            last_success_at=None,
            last_failure_at=None,
            next_retry_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(sync_api_module, "register_webhook_with_registry", fake_register_webhook_with_registry)

    registry_info_response = await client.get("/admin/dsp/registry-info")
    register_response = await client.post(
        "/admin/dsp/webhooks/register",
        json={
            "url": "https://unused.example.com",
            "secret": "integration" + "-secret",
            "types": ["acs"],
            "events": ["data_change"],
        },
        headers={"Authorization": "Bearer admin-token"},
    )

    assert registry_info_response.status_code == 200
    assert registry_info_response.json()["service"] == "registry-server"

    assert register_response.status_code == 200
    assert register_response.json()["id"] == "wh_123"
    assert register_response.json()["status"] == "active"


async def test_receive_webhook_endpoint_validates_signature(
    client: AsyncClient,
    service_runtime: ServiceRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_notification: dict[str, object] = {}
    body = json.dumps(
        {
            "webhook_id": "wh_456",
            "event": "data_change",
            "timestamp": "2026-04-30T10:00:00Z",
            "data": {"type": "acs", "id": "agent.1"},
        }
    )
    signature = hmac.new(
        service_runtime.settings.DSP_WEBHOOK_SECRET.encode("utf-8"),
        f"1746007200.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    async def fake_process_webhook_notification(notification: WebhookNotification) -> None:
        await asyncio.sleep(0)
        captured_notification.update(notification.model_dump())

    monkeypatch.setattr(sync_api_module, "process_webhook_notification", fake_process_webhook_notification)

    success_response = await client.post(
        "/admin/dsp/webhooks/receive",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Webhook-ID": "wh_456",
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Timestamp": "1746007200",
        },
    )
    failure_response = await client.post(
        "/admin/dsp/webhooks/receive",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Webhook-ID": "wh_456",
            "X-Webhook-Signature": "sha256=invalid",
            "X-Webhook-Timestamp": "1746007200",
        },
    )

    assert success_response.status_code == 200
    assert success_response.json()["status"] == "acknowledged"
    assert captured_notification["webhook_id"] == "wh_456"

    assert failure_response.status_code == 401
    assert failure_response.json()["detail"] == "Invalid signature"
