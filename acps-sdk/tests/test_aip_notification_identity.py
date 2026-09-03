from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_identity import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHORIZATION_FAILED_CODE,
    SenderIdentityMismatchError,
)
from acps_sdk.aip.aip_notification_client import AipNotificationClient, NotificationReceiver
from acps_sdk.aip.aip_notification_model import NOTIFICATION_TOKEN_HEADER, NotificationConfig
from acps_sdk.aip.aip_notification_server import (
    NotificationService,
    NotificationSubscription,
    add_aip_notification_router,
)

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"
NOW = "2026-06-24T00:00:00Z"


class _FixedStatusTransport(httpx.AsyncBaseTransport):
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return httpx.Response(self.status_code, request=request, content=b"{}")


class _FakeSslObject:
    def __init__(self, cert: dict | None) -> None:
        self._cert = cert

    def getpeercert(self) -> dict | None:
        return self._cert


class _FakeNetworkStream:
    def __init__(self, cert: dict | None) -> None:
        self._ssl_object = _FakeSslObject(cert) if cert is not None else None

    def get_extra_info(self, name: str):
        if name == "ssl_object":
            return self._ssl_object
        return None


def _cert(aic: str) -> dict:
    return {
        "subject": ((("commonName", aic),),),
        "subjectAltName": (("URI", f"acps://{aic}"),),
    }


class _NotifTransport(httpx.AsyncBaseTransport):
    def __init__(self, response_body: dict, *, cert_aic: str | None) -> None:
        self._response_body = response_body
        self._cert_aic = cert_aic

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content)
        resp = {**self._response_body, "id": req_body.get("id")}
        extensions = {}
        if self._cert_aic is not None:
            extensions["network_stream"] = _FakeNetworkStream(_cert(self._cert_aic))
        return httpx.Response(
            200,
            request=request,
            content=json.dumps(resp).encode(),
            headers={"Content-Type": "application/json"},
            extensions=extensions,
        )


def _task_result(sender_id: str = PARTNER_AIC) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId=sender_id,
        taskId="task-1",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=NOW),
    )


def _app_with_identity(service: NotificationService) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_peer(request: Request, call_next):
        peer_aic = request.headers.get("x-peer-aic")
        request.state.peer_aic = peer_aic
        request.state.peer_identity = None
        request.state.peer_identity_error = None
        return await call_next(request)

    add_aip_notification_router(app, service)
    return app


@pytest.mark.asyncio
async def test_notification_set_requires_peer_aic_when_binding_enabled() -> None:
    service = NotificationService(
        local_aic=PARTNER_AIC,
        transport=_FixedStatusTransport(),
        identity_binding_enabled=True,
    )
    app = _app_with_identity(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/notification/set",
            json={
                "jsonrpc": "2.0",
                "method": "notification/set",
                "id": "1",
                "params": {"url": "http://cb", "token": "tok", "taskId": "task-1"},
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == AUTHENTICATION_REQUIRED_CODE


@pytest.mark.asyncio
async def test_notification_configs_are_owner_scoped() -> None:
    service = NotificationService(
        local_aic=PARTNER_AIC,
        transport=_FixedStatusTransport(),
        identity_binding_enabled=True,
    )
    app = _app_with_identity(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        set_resp = await client.post(
            "/notification/set",
            headers={"x-peer-aic": LEADER_AIC},
            json={
                "jsonrpc": "2.0",
                "method": "notification/set",
                "id": "1",
                "params": {"url": "http://cb", "token": "tok", "taskId": "task-1"},
            },
        )
        config_id = set_resp.json()["result"]["id"]
        get_resp = await client.post(
            "/notification/get",
            headers={"x-peer-aic": OTHER_AIC},
            json={
                "jsonrpc": "2.0",
                "method": "notification/get",
                "id": "2",
                "params": {"taskId": "task-1", "notificationConfigId": config_id},
            },
        )
    assert get_resp.status_code == 200
    assert get_resp.json()["result"] == []


@pytest.mark.asyncio
async def test_notification_start_requires_sender_match_peer() -> None:
    service = NotificationService(
        local_aic=PARTNER_AIC,
        transport=_FixedStatusTransport(),
        identity_binding_enabled=True,
    )
    app = _app_with_identity(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        set_resp = await client.post(
            "/notification/set",
            headers={"x-peer-aic": LEADER_AIC},
            json={
                "jsonrpc": "2.0",
                "method": "notification/set",
                "id": "1",
                "params": {"url": "http://cb", "token": "tok", "taskId": "task-1"},
            },
        )
        config_id = set_resp.json()["result"]["id"]
        start_resp = await client.post(
            "/notification/start",
            headers={"x-peer-aic": LEADER_AIC},
            json={
                "jsonrpc": "2.0",
                "method": "notification/start",
                "id": "2",
                "params": {
                    "message": {
                        "type": "task-command",
                        "id": "cmd-1",
                        "sentAt": NOW,
                        "senderRole": "leader",
                        "senderId": OTHER_AIC,
                        "command": "start",
                        "taskId": "task-1",
                        "commandParams": {"notificationConfigId": config_id},
                    }
                },
            },
        )
    assert start_resp.status_code == 403
    assert start_resp.json()["detail"]["code"] == AUTHORIZATION_FAILED_CODE


@pytest.mark.asyncio
async def test_notification_dispatch_rejects_outbound_sender_mismatch() -> None:
    transport = _FixedStatusTransport()
    service = NotificationService(
        local_aic=PARTNER_AIC,
        transport=transport,
        identity_binding_enabled=True,
    )
    service.store.set(
        NotificationConfig(
            id="cfg-1",
            url="http://cb",
            token="tok",
            taskId="task-1",
        ),
        owner_aic=LEADER_AIC,
    )
    service.registry.add(
        NotificationSubscription(
            task_id="task-1",
            config_id="cfg-1",
            owner_aic=LEADER_AIC,
        )
    )

    with pytest.raises(SenderIdentityMismatchError):
        await service.dispatch(_task_result(sender_id=OTHER_AIC))
    assert transport.call_count == 0


@pytest.mark.asyncio
async def test_notification_client_validates_tls_server_identity() -> None:
    client = AipNotificationClient(
        partner_url="https://partner.local",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_NotifTransport(
            {
                "jsonrpc": "2.0",
                "result": {
                    "id": "cfg-1",
                    "url": "http://cb",
                    "token": "tok",
                    "taskId": "task-1",
                },
            },
            cert_aic=PARTNER_AIC,
        ),
    )
    result = await client.set_notification(
        task_id="task-1",
        callback_url="http://cb",
        token="tok",
    )
    assert result.id == "cfg-1"
    await client.close()


@pytest.mark.asyncio
async def test_notification_client_rejects_tls_server_mismatch() -> None:
    client = AipNotificationClient(
        partner_url="https://partner.local",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_NotifTransport(
            {"jsonrpc": "2.0", "result": True},
            cert_aic=OTHER_AIC,
        ),
    )
    with pytest.raises(SenderIdentityMismatchError):
        await client.delete_notification(task_id="task-1", config_id="cfg-1")
    await client.close()


@pytest.mark.asyncio
async def test_notification_receiver_validates_callback_sender_peer() -> None:
    received: list[TaskResult] = []

    async def handler(task_result: TaskResult) -> None:
        received.append(task_result)

    receiver = NotificationReceiver(
        token="tok",
        handler=handler,
        identity_binding_enabled=True,
    )
    app = FastAPI()

    @app.middleware("http")
    async def _inject_peer(request: Request, call_next):
        request.state.peer_aic = request.headers.get("x-peer-aic")
        request.state.peer_identity = None
        request.state.peer_identity_error = None
        return await call_next(request)

    receiver.mount(app, "/callback")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/callback",
            headers={
                "x-peer-aic": PARTNER_AIC,
                "Content-Type": "application/json",
                NOTIFICATION_TOKEN_HEADER: "tok",
            },
            content=_task_result().model_dump_json(exclude_none=True).encode(),
        )
    assert resp.status_code == 200
    await asyncio.sleep(0)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_notification_receiver_rejects_missing_peer_when_binding_enabled() -> None:
    async def handler(task_result: TaskResult) -> None:
        raise AssertionError("handler should not run")

    receiver = NotificationReceiver(
        token="tok",
        handler=handler,
        identity_binding_enabled=True,
    )
    app = FastAPI()
    receiver.mount(app, "/callback")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/callback",
            headers={
                "Content-Type": "application/json",
                NOTIFICATION_TOKEN_HEADER: "tok",
            },
            content=_task_result().model_dump_json(exclude_none=True).encode(),
        )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == AUTHENTICATION_REQUIRED_CODE


@pytest.mark.asyncio
async def test_notification_client_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")
    client = AipNotificationClient(
        partner_url="https://partner.local",
        leader_id=LEADER_AIC,
        identity_binding_enabled=False,
        transport=_NotifTransport({"jsonrpc": "2.0", "result": True}, cert_aic=None),
    )
    try:
        assert "identity binding disabled for notification client" in caplog.text.lower()
    finally:
        await client.close()


def test_notification_receiver_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")

    async def handler(task_result: TaskResult) -> None:
        raise AssertionError(f"handler should not run: {task_result.taskId}")

    NotificationReceiver(
        token="tok",
        handler=handler,
        identity_binding_enabled=False,
    )
    assert "identity binding disabled for notification receiver" in caplog.text.lower()
