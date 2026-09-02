from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import httpx
import pytest

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_identity import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHORIZATION_FAILED_CODE,
    PeerAicMissingError,
    SenderIdentityMismatchError,
)
from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_rpc_model import RpcResponse
from acps_sdk.aip.aip_rpc_server import CommandHandlers, TaskManager, handle_rpc_request

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"


def _task_command(sender_id: str = LEADER_AIC) -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="leader",
        senderId=sender_id,
        sessionId="session-1",
        command=TaskCommandType.Start,
        taskId="task-1",
    )


def _task_result(sender_id: str = PARTNER_AIC) -> TaskResult:
    return TaskResult(
        id="result-1",
        sentAt="2026-06-24T00:00:01Z",
        senderRole="partner",
        senderId=sender_id,
        taskId="task-1",
        sessionId="session-1",
        status=TaskStatus(state=TaskState.Working, stateChangedAt="2026-06-24T00:00:01Z"),
    )


def _rpc_body(command: TaskCommand) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "rpc",
        "id": "rpc-1",
        "params": {"command": json.loads(command.model_dump_json(exclude_none=True))},
    }


class _Request:
    def __init__(
        self,
        body: dict,
        *,
        peer_aic: str | None = LEADER_AIC,
        peer_identity_error=None,
    ) -> None:
        self._body = body
        self.state = SimpleNamespace(
            peer_aic=peer_aic,
            peer_identity=None,
            peer_identity_error=peer_identity_error,
        )

    async def json(self) -> dict:
        return self._body


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


def _rpc_success_response(
    request_id: str,
    *,
    sender_id: str = PARTNER_AIC,
    cert_aic: str | None = PARTNER_AIC,
) -> httpx.Response:
    request = httpx.Request("POST", "https://partner.local/rpc")
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": json.loads(_task_result(sender_id=sender_id).model_dump_json(exclude_none=True)),
    }
    extensions = {}
    if cert_aic is not None:
        extensions["network_stream"] = _FakeNetworkStream(_cert(cert_aic))
    return httpx.Response(200, json=body, extensions=extensions, request=request)


@pytest.mark.asyncio
async def test_rpc_server_accepts_matching_sender_and_peer() -> None:
    handlers = CommandHandlers(on_start=AsyncMock(return_value=_task_result()))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command())),
        handlers,
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is None
    assert response.result is not None
    handlers.on_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_rpc_server_rejects_missing_peer_aic() -> None:
    handlers = CommandHandlers(on_start=AsyncMock(return_value=_task_result()))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command()), peer_aic=None),
        handlers,
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is not None
    assert response.error.code == AUTHENTICATION_REQUIRED_CODE
    handlers.on_start.assert_not_called()


@pytest.mark.asyncio
async def test_rpc_server_rejects_sender_mismatch() -> None:
    handlers = CommandHandlers(on_start=AsyncMock(return_value=_task_result()))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command(sender_id=OTHER_AIC))),
        handlers,
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is not None
    assert response.error.code == AUTHORIZATION_FAILED_CODE
    handlers.on_start.assert_not_called()


@pytest.mark.asyncio
async def test_rpc_server_rejects_outbound_sender_mismatch() -> None:
    handlers = CommandHandlers(on_start=AsyncMock(return_value=_task_result(sender_id=OTHER_AIC)))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command())),
        handlers,
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    assert response.error is not None
    assert response.error.code == AUTHORIZATION_FAILED_CODE


@pytest.mark.asyncio
async def test_rpc_server_custom_dispatch_keeps_identity_binding() -> None:
    custom_dispatch = AsyncMock(return_value=RpcResponse(id="rpc-1", result=_task_result()))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command())),
        CommandHandlers(),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        dispatch_request=custom_dispatch,
    )
    assert response.error is None
    assert response.result is not None
    custom_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_rpc_server_custom_dispatch_rejects_mismatched_result_sender() -> None:
    custom_dispatch = AsyncMock(return_value=RpcResponse(id="rpc-1", result=_task_result(sender_id=OTHER_AIC)))
    response = await handle_rpc_request(
        _Request(_rpc_body(_task_command())),
        CommandHandlers(),
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        dispatch_request=custom_dispatch,
    )
    assert response.error is not None
    assert response.error.code == AUTHORIZATION_FAILED_CODE


@pytest.mark.asyncio
async def test_rpc_server_default_handlers_backfill_local_sender_id() -> None:
    TaskManager._tasks.clear()
    try:
        response = await handle_rpc_request(
            _Request(_rpc_body(_task_command())),
            CommandHandlers(),
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        assert response.error is None
        assert response.result is not None
        assert response.result.senderId == PARTNER_AIC
    finally:
        TaskManager._tasks.clear()


@pytest.mark.asyncio
async def test_rpc_server_default_get_backfills_placeholder_sender_id() -> None:
    TaskManager._tasks.clear()
    try:
        placeholder = TaskManager.create_task(_task_command())
        assert placeholder.senderId == "server"

        get_command = _task_command()
        get_command.command = TaskCommandType.Get

        response = await handle_rpc_request(
            _Request(_rpc_body(get_command)),
            CommandHandlers(),
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        assert response.error is None
        assert response.result is not None
        assert response.result.senderId == PARTNER_AIC
    finally:
        TaskManager._tasks.clear()


@pytest.mark.asyncio
async def test_rpc_client_validates_outbound_sender_before_request() -> None:
    client = AipRpcClient(
        partner_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    client.http_client.post = AsyncMock()
    with pytest.raises(SenderIdentityMismatchError):
        await client._send_request(_task_command(sender_id=OTHER_AIC))
    client.http_client.post.assert_not_called()
    await client.close()


def test_rpc_client_requires_expected_partner_aic_when_enabled() -> None:
    with pytest.raises(ValueError):
        AipRpcClient(
            partner_url="https://partner.local/rpc",
            leader_id=LEADER_AIC,
            identity_binding_enabled=True,
        )


@pytest.mark.asyncio
async def test_rpc_client_accepts_matching_tls_server_and_result_sender() -> None:
    client = AipRpcClient(
        partner_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    client.http_client.post = AsyncMock(return_value=_rpc_success_response("req-1"))
    with patch("uuid.uuid4", return_value="req-1"):
        response = await client._send_request(_task_command())
    assert isinstance(response, RpcResponse)
    assert response.result is not None
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_rejects_missing_tls_server_aic() -> None:
    client = AipRpcClient(
        partner_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    client.http_client.post = AsyncMock(
        return_value=_rpc_success_response("req-1", cert_aic=None)
    )
    with pytest.raises(PeerAicMissingError):
        await client._send_request(_task_command())
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_rejects_tls_server_aic_mismatch() -> None:
    client = AipRpcClient(
        partner_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    client.http_client.post = AsyncMock(
        return_value=_rpc_success_response("req-1", cert_aic=OTHER_AIC)
    )
    with pytest.raises(SenderIdentityMismatchError):
        await client._send_request(_task_command())
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_validates_tls_server_before_jsonrpc_error() -> None:
    client = AipRpcClient(
        partner_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    body = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": -32000, "message": "boom"},
    }
    request = httpx.Request("POST", "https://partner.local/rpc")
    response = httpx.Response(
        200,
        json=body,
        extensions={"network_stream": _FakeNetworkStream(_cert(OTHER_AIC))},
        request=request,
    )
    client.http_client.post = AsyncMock(return_value=response)
    with pytest.raises(SenderIdentityMismatchError):
        await client._send_request(_task_command())
    await client.close()


def test_rpc_client_logs_warning_when_identity_binding_disabled(caplog) -> None:
    with caplog.at_level("WARNING"):
        client = AipRpcClient(
            partner_url="http://partner.local/rpc",
            leader_id=LEADER_AIC,
            identity_binding_enabled=False,
        )
    assert "identity binding disabled" in caplog.text.lower()
    import asyncio

    asyncio.run(client.close())
