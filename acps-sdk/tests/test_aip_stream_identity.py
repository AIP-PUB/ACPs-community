from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi import HTTPException

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_identity import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHORIZATION_FAILED_CODE,
    PeerAicMissingError,
    SenderIdentityMismatchError,
)
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_model import StreamEventData, StreamResponse
from acps_sdk.aip.aip_stream_server import StreamHub, _sse_event_generator, add_aip_stream_router, handle_stream_request

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"
NOW = "2026-06-24T00:00:00Z"


def _task_command(
    sender_id: str = LEADER_AIC,
    command: TaskCommandType = TaskCommandType.Start,
) -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=NOW,
        senderRole="leader",
        senderId=sender_id,
        sessionId="session-1",
        command=command,
        taskId="task-1",
    )


def _task_result(sender_id: str = PARTNER_AIC) -> TaskResult:
    return TaskResult(
        id="result-1",
        sentAt=NOW,
        senderRole="partner",
        senderId=sender_id,
        taskId="task-1",
        sessionId="session-1",
        status=TaskStatus(state=TaskState.Working, stateChangedAt=NOW),
    )


def _stream_body(command: TaskCommand) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "stream",
        "id": "stream-1",
        "params": {"message": json.loads(command.model_dump_json(exclude_none=True))},
    }


class _Request:
    def __init__(self, body: dict, *, peer_aic: str | None = LEADER_AIC) -> None:
        self._body = body
        self.state = SimpleNamespace(
            peer_aic=peer_aic,
            peer_identity=None,
            peer_identity_error=None,
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


class _SSETransport(httpx.AsyncBaseTransport):
    def __init__(self, events: list[StreamResponse], *, cert_aic: str | None) -> None:
        self._events = events
        self._cert_aic = cert_aic
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = "".join(
            f"data: {event.model_dump_json(exclude_none=True)}\n\n"
            for event in self._events
        ).encode()
        extensions = {}
        if self._cert_aic is not None:
            extensions["network_stream"] = _FakeNetworkStream(_cert(self._cert_aic))
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
            extensions=extensions,
        )


@pytest.mark.asyncio
async def test_stream_server_accepts_matching_sender_and_peer() -> None:
    hub = StreamHub()
    on_start = AsyncMock()
    response = await handle_stream_request(
        _Request(_stream_body(_task_command())),
        hub,
        on_start,
        local_aic=PARTNER_AIC,
        identity_binding_enabled=True,
    )
    await asyncio.sleep(0)
    assert response.media_type == "text/event-stream"
    on_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_server_rejects_sender_mismatch() -> None:
    hub = StreamHub()
    on_start = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await handle_stream_request(
            _Request(_stream_body(_task_command(sender_id=OTHER_AIC))),
            hub,
            on_start,
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == AUTHORIZATION_FAILED_CODE
    on_start.assert_not_called()


@pytest.mark.asyncio
async def test_stream_server_rejects_missing_peer_aic() -> None:
    hub = StreamHub()
    on_start = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await handle_stream_request(
            _Request(_stream_body(_task_command()), peer_aic=None),
            hub,
            on_start,
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == AUTHENTICATION_REQUIRED_CODE


@pytest.mark.asyncio
async def test_sse_generator_rejects_outbound_sender_mismatch() -> None:
    hub = StreamHub()
    await hub.publish_task_result("task-1", _task_result(sender_id=OTHER_AIC))
    with pytest.raises(SenderIdentityMismatchError):
        async for _ in _sse_event_generator(
            hub=hub,
            task_id="task-1",
            rpc_id="stream-1",
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        ):
            pass


@pytest.mark.asyncio
async def test_stream_client_accepts_matching_tls_server_and_event_sender() -> None:
    event = StreamResponse(
        id="stream-1",
        result=StreamEventData(eventSeq=1, eventData=_task_result()),
    )
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_SSETransport([event], cert_aic=PARTNER_AIC),
    )
    events = []
    async for item in client.start_stream(session_id="session-1", task_id="task-1"):
        events.append(item)
    assert len(events) == 1
    await client.close()


@pytest.mark.asyncio
async def test_stream_client_rejects_missing_tls_server_aic() -> None:
    event = StreamResponse(
        id="stream-1",
        result=StreamEventData(eventSeq=1, eventData=_task_result()),
    )
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_SSETransport([event], cert_aic=None),
    )
    with pytest.raises(PeerAicMissingError):
        async for _ in client.start_stream(session_id="session-1", task_id="task-1"):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_stream_client_rejects_tls_server_aic_mismatch() -> None:
    event = StreamResponse(
        id="stream-1",
        result=StreamEventData(eventSeq=1, eventData=_task_result()),
    )
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_SSETransport([event], cert_aic=OTHER_AIC),
    )
    with pytest.raises(SenderIdentityMismatchError):
        async for _ in client.start_stream(session_id="session-1", task_id="task-1"):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_stream_client_rejects_event_sender_mismatch() -> None:
    event = StreamResponse(
        id="stream-1",
        result=StreamEventData(eventSeq=1, eventData=_task_result(sender_id=OTHER_AIC)),
    )
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_SSETransport([event], cert_aic=PARTNER_AIC),
    )
    with pytest.raises(SenderIdentityMismatchError):
        async for _ in client.start_stream(session_id="session-1", task_id="task-1"):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_stream_client_rejects_outbound_sender_before_request() -> None:
    event = StreamResponse(
        id="stream-1",
        result=StreamEventData(eventSeq=1, eventData=_task_result()),
    )
    transport = _SSETransport([event], cert_aic=PARTNER_AIC)
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=transport,
    )
    with pytest.raises(SenderIdentityMismatchError):
        async for _ in client.open_stream(_task_command(sender_id=OTHER_AIC)):
            pass
    assert transport.calls == 0
    await client.close()


def test_stream_client_passes_expected_partner_aic_to_internal_rpc_client() -> None:
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        expected_partner_aic=PARTNER_AIC,
        identity_binding_enabled=True,
        transport=_SSETransport([], cert_aic=PARTNER_AIC),
    )
    assert client._rpc_client._expected_partner_aic == PARTNER_AIC
    assert client._rpc_client._identity_binding_enabled is True


@pytest.mark.asyncio
async def test_stream_client_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")
    client = AipStreamClient(
        partner_stream_url="https://partner.local/stream",
        partner_rpc_url="https://partner.local/rpc",
        leader_id=LEADER_AIC,
        identity_binding_enabled=False,
        transport=_SSETransport([], cert_aic=None),
    )
    try:
        assert "identity binding disabled for stream client" in caplog.text.lower()
    finally:
        await client.close()


def test_add_aip_stream_router_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")
    app = FastAPI()
    add_aip_stream_router(
        app,
        "/stream",
        StreamHub(),
        AsyncMock(),
        identity_binding_enabled=False,
    )
    assert "identity binding disabled for stream server" in caplog.text.lower()
