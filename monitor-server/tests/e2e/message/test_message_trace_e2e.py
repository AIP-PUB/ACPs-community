"""E2E — Message trace 串联（EM10）。

验收项：
- 同一 messageId 的 send/receive 共享 trace_id
- receive 的 parent_span_id == send 的 span_id
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.e2e.message.test_message_events_e2e import _time_range_body
from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_message_trace_parent_span_linkage(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """send.span_id == receive.parent_span_id（O-M6 端到端正确性）。"""
    from app.core.config import settings

    message_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    send_span = uuid.uuid4().hex[:16]
    recv_span = uuid.uuid4().hex[:16]
    exchange = f"trace-e2e-{uuid.uuid4().hex[:8]}"

    from tests.support.factory import make_message_log_record

    log_send = str(uuid.uuid4())
    log_recv = str(uuid.uuid4())

    await produce_message_event(
        record=make_message_log_record(
            log_id=log_send,
            event_type="send",
            system="rabbitmq",
            destination_name=exchange,
            destination_kind="exchange",
            message_id=message_id,
            trace_id=trace_id,
            span_id=send_span,
            parent_span_id="",
        )
    )
    await produce_message_event(
        record=make_message_log_record(
            log_id=log_recv,
            event_type="receive",
            system="rabbitmq",
            destination_name=exchange,
            destination_kind="exchange",
            message_id=message_id,
            trace_id=trace_id,
            span_id=recv_span,
            parent_span_id=send_span,
        )
    )
    await wait_for_message_event_ingested(log_recv, timeout_s=25)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/events/query",
        json={
            **_time_range_body(),
            "filter": {
                "conditions": [{"field": "messageId", "op": "eq", "value": message_id}],
                "logic": "and",
            },
        },
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    by_type = {item.get("eventType"): item for item in items if item.get("messageId") == message_id}
    assert "send" in by_type and "receive" in by_type
    assert by_type["send"].get("traceId") == trace_id
    assert by_type["receive"].get("traceId") == trace_id
    assert by_type["receive"].get("parentSpanId") == by_type["send"].get("spanId")
