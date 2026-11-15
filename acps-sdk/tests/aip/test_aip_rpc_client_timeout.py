from __future__ import annotations

import json

import httpx
import pytest

from acps_sdk.aip.aip_rpc_client import AipRpcClient

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.timeouts: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.timeouts.append(request.extensions.get("timeout"))
        body = json.loads(request.content)
        command = body["params"]["command"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "type": "task-result",
                    "id": "result-1",
                    "sentAt": "2026-01-01T00:00:00Z",
                    "senderRole": "partner",
                    "senderId": PARTNER_AIC,
                    "taskId": command["taskId"],
                    "sessionId": command["sessionId"],
                    "status": {"state": "working", "stateChangedAt": "2026-01-01T00:00:00Z"},
                },
            },
        )


def _client(transport: _CaptureTransport, **kwargs) -> AipRpcClient:
    return AipRpcClient(
        partner_url="http://partner.local/rpc",
        leader_id=LEADER_AIC,
        transport=transport,
        identity_binding_enabled=False,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_rpc_client_default_timeout_is_30_seconds() -> None:
    transport = _CaptureTransport()
    client = _client(transport)
    await client.start_task("session-1", "hello")
    assert transport.timeouts[-1]["read"] == 30.0
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout, expected",
    [
        (120.0, {"connect": 120.0, "read": 120.0, "write": 120.0, "pool": 120.0}),
        (None, {"connect": None, "read": None, "write": None, "pool": None}),
        (httpx.Timeout(5.0, read=300.0), {"connect": 5.0, "read": 300.0, "write": 5.0, "pool": 5.0}),
    ],
)
async def test_rpc_client_timeout_is_configurable(timeout, expected) -> None:
    transport = _CaptureTransport()
    client = _client(transport, timeout=timeout)
    await client.get_task("task-1", "session-1")
    assert transport.timeouts[-1] == expected
    await client.close()
