from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from acps_sdk.aip.aip_base_model import TaskResult
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_model import TaskStatusUpdateEvent

from tests.e2e.conftest import (
    _TEST_CLIENT_SSL_CONTEXT,
    FORGED_SENDER_AIC,
    PROJECT_ROOT,
    TEST_SENDER_AIC,
    create_rpc_request,
    create_task_command,
)

CHINA_TRANSPORT_AGENT = "china_transport"
CHINA_TRANSPORT_DIR = PROJECT_ROOT / "partners" / "online" / CHINA_TRANSPORT_AGENT


def _load_partner_aic(agent_dir: Path) -> str:
    with (agent_dir / "acs.json").open(encoding="utf-8") as file_obj:
        return str(json.load(file_obj)["aic"])


CHINA_TRANSPORT_AIC = _load_partner_aic(CHINA_TRANSPORT_DIR)


@pytest.mark.e2e
def test_direct_rpc_rejects_forged_sender_id(
    agent_urls: dict[str, str],
    unique_ids: dict[str, str],
) -> None:
    assert FORGED_SENDER_AIC != TEST_SENDER_AIC

    command = create_task_command(
        "帮我订火车票",
        "start",
        task_id=f"{unique_ids['task_id']}-forged-rpc",
        session_id=unique_ids["session_id"],
        sender_id=FORGED_SENDER_AIC,
    )
    request = create_rpc_request(command)

    with httpx.Client(timeout=30.0, verify=_TEST_CLIENT_SSL_CONTEXT) as client:
        response = client.post(
            f"{agent_urls[CHINA_TRANSPORT_AGENT]}/rpc",
            json=request,
            timeout=30.0,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] is None
    assert payload["error"]["code"] == -32009
    assert "senderId" in payload["error"]["data"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_stream_accepts_matching_leader_identity(agent_urls: dict[str, str]) -> None:
    task_id = f"e2e-stream-{uuid.uuid4().hex[:12]}"
    session_id = f"e2e-session-{uuid.uuid4().hex[:12]}"
    stream_client = AipStreamClient(
        partner_stream_url=f"{agent_urls[CHINA_TRANSPORT_AGENT]}/stream",
        partner_rpc_url=f"{agent_urls[CHINA_TRANSPORT_AGENT]}/rpc",
        leader_id=TEST_SENDER_AIC,
        ssl_context=_TEST_CLIENT_SSL_CONTEXT,
        expected_partner_aic=CHINA_TRANSPORT_AIC,
        identity_binding_enabled=True,
    )

    observed_states: list[str] = []
    partner_sender_ids: list[str] = []

    try:
        async for event in stream_client.start_stream(
            session_id=session_id,
            task_id=task_id,
            text_content="帮我查询明天北京到上海的高铁，上午出发",
        ):
            assert event.result is not None
            payload = event.result.eventData
            if not isinstance(payload, TaskResult | TaskStatusUpdateEvent):
                continue
            partner_sender_ids.append(payload.senderId)
            observed_states.append(payload.status.state.value)
            if payload.status.state.value in {"awaiting-input", "awaiting-completion", "rejected"}:
                break
    finally:
        await stream_client.close()

    assert observed_states
    assert partner_sender_ids
    assert all(sender_id == CHINA_TRANSPORT_AIC for sender_id in partner_sender_ids)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_stream_rejects_forged_sender_id(agent_urls: dict[str, str]) -> None:
    request_body = {
        "jsonrpc": "2.0",
        "id": f"rpc-{uuid.uuid4().hex[:12]}",
        "method": "stream",
        "params": {
            "message": {
                "type": "task-command",
                "id": f"cmd-{uuid.uuid4().hex[:12]}",
                "sentAt": datetime.now(UTC).isoformat(),
                "senderRole": "leader",
                "senderId": FORGED_SENDER_AIC,
                "taskId": f"e2e-stream-forged-{uuid.uuid4().hex[:12]}",
                "sessionId": f"e2e-session-{uuid.uuid4().hex[:12]}",
                "command": "start",
                "dataItems": [{"type": "text", "text": "帮我订火车票"}],
            }
        },
    }

    async with (
        httpx.AsyncClient(verify=_TEST_CLIENT_SSL_CONTEXT, timeout=30.0) as client,
        client.stream(
            "POST",
            f"{agent_urls[CHINA_TRANSPORT_AGENT]}/stream",
            json=request_body,
            headers={"Accept": "text/event-stream"},
        ) as response,
    ):
        assert response.status_code == 403
        payload = json.loads((await response.aread()).decode())

    assert payload["detail"]["code"] == -32009
    assert "senderId" in payload["detail"]["data"]
