from __future__ import annotations
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_identity import SenderIdentityMismatchError
from acps_sdk.aip.aip_peer_cert import AipPeerCertificateMiddleware
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_server import StreamHandlers, StreamHub, add_aip_stream_router

from ._tls_test_utils import (
    LEADER_AIC,
    LEADER_CLIENT_CERT,
    LEADER_CLIENT_KEY,
    LEADER_TRUST_BUNDLE,
    PARTNER_AIC,
    PARTNER_SERVER_CERT,
    PARTNER_SERVER_KEY,
    PARTNER_TRUST_BUNDLE,
    build_client_ssl_context,
    build_server_ssl_context,
    run_tls_app,
)

OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"


def _build_task_result(task_id: str, *, sender_id: str = PARTNER_AIC) -> TaskResult:
    now = datetime.now(timezone.utc).isoformat()
    return TaskResult(
        id="result-1",
        sentAt=now,
        senderRole="partner",
        senderId=sender_id,
        taskId=task_id,
        sessionId="sess-1",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=now),
    )


def _build_stream_app(
    *,
    event_sender_id: str = PARTNER_AIC,
    local_aic: str = PARTNER_AIC,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    hub = StreamHub()

    async def _on_start(command) -> None:
        await hub.publish_task_result(
            command.taskId,
            _build_task_result(command.taskId, sender_id=event_sender_id),
        )

    add_aip_stream_router(
        app,
        "/stream",
        hub,
        StreamHandlers(on_stream_start=_on_start),
        local_aic=local_aic,
        identity_binding_enabled=True,
    )
    return app


@pytest.mark.asyncio
async def test_stream_mtls_identity_binding_accepts_matching_request_and_event() -> None:
    server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=True,
    )
    client_ssl = build_client_ssl_context(
        cert_file=LEADER_CLIENT_CERT,
        key_file=LEADER_CLIENT_KEY,
        ca_file=LEADER_TRUST_BUNDLE,
    )

    with run_tls_app(_build_stream_app(), ssl_context=server_ssl) as base_url:
        client = AipStreamClient(
            partner_stream_url=f"{base_url}/stream",
            partner_rpc_url=f"{base_url}/rpc",
            leader_id=LEADER_AIC,
            ssl_context=client_ssl,
            expected_partner_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        try:
            events = []
            async for item in client.start_stream(
                session_id="sess-1",
                task_id="task-1",
                text_content="hello",
            ):
                events.append(item)
        finally:
            await client.close()

    assert len(events) == 1
    assert events[0].result is not None
    assert events[0].result.eventData.senderId == PARTNER_AIC


@pytest.mark.asyncio
async def test_stream_mtls_identity_binding_rejects_forged_request_sender() -> None:
    app = _build_stream_app()
    server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=True,
    )
    client_ssl = build_client_ssl_context(
        cert_file=LEADER_CLIENT_CERT,
        key_file=LEADER_CLIENT_KEY,
        ca_file=LEADER_TRUST_BUNDLE,
    )

    request_body = {
        "jsonrpc": "2.0",
        "id": "stream-1",
        "method": "stream",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": datetime.now(timezone.utc).isoformat(),
                "senderRole": "leader",
                "senderId": OTHER_AIC,
                "taskId": "task-1",
                "sessionId": "sess-1",
                "command": "start",
                "dataItems": [{"type": "text", "text": "hello"}],
            }
        },
    }

    with run_tls_app(app, ssl_context=server_ssl) as base_url:
        async with httpx.AsyncClient(verify=client_ssl, timeout=10.0) as client:
            response = await client.post(f"{base_url}/stream", json=request_body)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == -32009


@pytest.mark.asyncio
async def test_stream_mtls_identity_binding_rejects_forged_event_sender() -> None:
    server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=True,
    )
    client_ssl = build_client_ssl_context(
        cert_file=LEADER_CLIENT_CERT,
        key_file=LEADER_CLIENT_KEY,
        ca_file=LEADER_TRUST_BUNDLE,
    )

    with run_tls_app(
        _build_stream_app(event_sender_id=OTHER_AIC, local_aic=OTHER_AIC),
        ssl_context=server_ssl,
    ) as base_url:
        client = AipStreamClient(
            partner_stream_url=f"{base_url}/stream",
            partner_rpc_url=f"{base_url}/rpc",
            leader_id=LEADER_AIC,
            ssl_context=client_ssl,
            expected_partner_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        try:
            with pytest.raises(SenderIdentityMismatchError):
                async for _ in client.start_stream(
                    session_id="sess-1",
                    task_id="task-1",
                    text_content="hello",
                ):
                    pass
        finally:
            await client.close()
