from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_notification_client import NotificationReceiver
from acps_sdk.aip.aip_notification_model import NOTIFICATION_TOKEN_HEADER, NotificationConfig
from acps_sdk.aip.aip_notification_server import (
    NotificationService,
    NotificationSubscription,
)
from acps_sdk.aip.aip_peer_cert import AipPeerCertificateMiddleware

from ._tls_test_utils import (
    LEADER_AIC,
    PARTNER_AIC,
    PARTNER_CLIENT_CERT,
    PARTNER_CLIENT_KEY,
    PARTNER_SERVER_CERT,
    PARTNER_SERVER_KEY,
    PARTNER_TRUST_BUNDLE,
    build_client_ssl_context,
    build_server_ssl_context,
    run_tls_app,
)


def _build_task_result() -> TaskResult:
    now = datetime.now(timezone.utc).isoformat()
    return TaskResult(
        id="task-result-1",
        sentAt=now,
        senderRole="partner",
        senderId=PARTNER_AIC,
        taskId="task-1",
        sessionId="sess-1",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=now),
    )


@pytest.mark.asyncio
async def test_notification_callback_uses_partner_client_cert_identity() -> None:
    received: asyncio.Queue[TaskResult] = asyncio.Queue()

    async def _handler(task_result: TaskResult) -> None:
        await received.put(task_result)

    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    receiver = NotificationReceiver(
        token="token-1",
        handler=_handler,
        identity_binding_enabled=True,
    )
    receiver.mount(app, "/callbacks/{task_id}")

    callback_server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=True,
    )
    callback_client_ssl = build_client_ssl_context(
        cert_file=PARTNER_CLIENT_CERT,
        key_file=PARTNER_CLIENT_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
    )

    service = NotificationService(
        local_aic=PARTNER_AIC,
        callback_ssl_context=callback_client_ssl,
        identity_binding_enabled=True,
    )
    service.store.set(
        NotificationConfig(
            id="cfg-1",
            taskId="task-1",
            url="https://127.0.0.1:1/callbacks/task-1",
            token="token-1",
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

    with run_tls_app(app, ssl_context=callback_server_ssl) as base_url:
        service.store.set(
            NotificationConfig(
                id="cfg-1",
                taskId="task-1",
                url=f"{base_url}/callbacks/task-1",
                token="token-1",
            ),
            owner_aic=LEADER_AIC,
        )
        await service.dispatch(_build_task_result())
        received_result = await asyncio.wait_for(received.get(), timeout=5.0)

    await service.close()

    assert received_result.senderId == PARTNER_AIC
    assert received_result.taskId == "task-1"


@pytest.mark.asyncio
async def test_notification_callback_rejects_forged_sender_body_under_real_mtls() -> None:
    received: asyncio.Queue[TaskResult] = asyncio.Queue()

    async def _handler(task_result: TaskResult) -> None:
        await received.put(task_result)

    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    receiver = NotificationReceiver(
        token="token-1",
        handler=_handler,
        identity_binding_enabled=True,
    )
    receiver.mount(app, "/callbacks/{task_id}")

    callback_server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=True,
    )
    callback_client_ssl = build_client_ssl_context(
        cert_file=PARTNER_CLIENT_CERT,
        key_file=PARTNER_CLIENT_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
    )

    forged = _build_task_result().model_copy(update={"senderId": LEADER_AIC})

    with run_tls_app(app, ssl_context=callback_server_ssl) as base_url:
        async with httpx.AsyncClient(verify=callback_client_ssl, timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/callbacks/task-1",
                content=json.dumps(forged.model_dump(exclude_none=True)).encode(),
                headers={
                    "Content-Type": "application/json",
                    NOTIFICATION_TOKEN_HEADER: "token-1",
                },
            )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == -32009
    assert received.empty()
