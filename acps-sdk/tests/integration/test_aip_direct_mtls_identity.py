from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_peer_cert import AipPeerCertificateMiddleware
from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_rpc_server import CommandHandlers, add_aip_rpc_router

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


def _build_task_result(task_id: str) -> TaskResult:
    now = datetime.now(timezone.utc).isoformat()
    return TaskResult(
        id="result-1",
        sentAt=now,
        senderRole="partner",
        senderId=PARTNER_AIC,
        taskId=task_id,
        sessionId="sess-1",
        status=TaskStatus(state=TaskState.Accepted, stateChangedAt=now),
    )


@pytest.mark.asyncio
async def test_direct_rpc_mtls_identity_binding_accepts_matching_sender() -> None:
    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    add_aip_rpc_router(
        app,
        "/rpc",
        CommandHandlers(on_start=lambda command, task: _return_task_result(command.taskId)),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )

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

    with run_tls_app(app, ssl_context=server_ssl) as base_url:
        client = AipRpcClient(
            partner_url=f"{base_url}/rpc",
            leader_id=LEADER_AIC,
            ssl_context=client_ssl,
            expected_partner_aic=PARTNER_AIC,
        )
        try:
            result = await client.start_task(
                session_id="sess-1",
                user_input="hello",
                task_id="task-1",
            )
        finally:
            await client.close()

    assert result.senderId == PARTNER_AIC
    assert result.taskId == "task-1"


@pytest.mark.asyncio
async def test_direct_rpc_mtls_identity_binding_rejects_forged_sender() -> None:
    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    add_aip_rpc_router(
        app,
        "/rpc",
        CommandHandlers(on_start=lambda command, task: _return_task_result(command.taskId)),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )

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
        "id": "rpc-1",
        "method": "rpc",
        "params": {
            "command": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": datetime.now(timezone.utc).isoformat(),
                "senderRole": "leader",
                "senderId": "1.2.156.3088.1.1.FAKEID.FORGED.1.XXXX",
                "taskId": "task-1",
                "sessionId": "sess-1",
                "command": "start",
                "dataItems": [{"type": "text", "text": "hello"}],
            }
        },
    }

    with run_tls_app(app, ssl_context=server_ssl) as base_url:
        async with httpx.AsyncClient(verify=client_ssl, timeout=10.0) as client:
            response = await client.post(f"{base_url}/rpc", json=request_body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32009


@pytest.mark.asyncio
async def test_direct_rpc_identity_binding_rejects_when_peer_certificate_is_unavailable() -> None:
    app = FastAPI()
    add_aip_rpc_router(
        app,
        "/rpc",
        CommandHandlers(on_start=lambda command, task: _return_task_result(command.taskId)),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )

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
        "id": "rpc-2",
        "method": "rpc",
        "params": {
            "command": {
                "type": "task-command",
                "id": "cmd-2",
                "sentAt": datetime.now(timezone.utc).isoformat(),
                "senderRole": "leader",
                "senderId": LEADER_AIC,
                "taskId": "task-2",
                "sessionId": "sess-2",
                "command": "start",
                "dataItems": [{"type": "text", "text": "hello"}],
            }
        },
    }

    with run_tls_app(app, ssl_context=server_ssl) as base_url:
        async with httpx.AsyncClient(verify=client_ssl, timeout=10.0) as client:
            response = await client.post(f"{base_url}/rpc", json=request_body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32008


async def _return_task_result(task_id: str | None) -> TaskResult:
    assert task_id is not None
    return _build_task_result(task_id)
