#!/usr/bin/env python3
"""Message 开发模式冒烟测试：直连 Kafka 投递 send/receive/ack + dead_letter → 轮询 Query API。

前置：infra（kafka+redis+clickhouse）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/e2e_message_verify.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证：
  Kafka amp.message → MessageWriter → ClickHouse → /message/events|lifecycles|deadletters
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def _poll_until(
    *,
    label: str,
    check: Callable[[], Awaitable[bool]],
    timeout_s: float = 60.0,
    poll_interval_s: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if await check():
                print(f"[OK] {label}")
                return True
        except Exception as exc:
            print(f"[WARN] {label} 轮询异常: {exc}")
        await asyncio.sleep(poll_interval_s)
    print(f"[FAIL] 超时 {timeout_s}s：{label}", file=sys.stderr)
    return False


async def main() -> None:
    from app.core.config import settings

    monitor_url = f"http://localhost:{settings.uvicorn_port}"
    bootstrap = settings.kafka_bootstrap_servers
    topic = settings.message_topic
    api_base = f"{monitor_url}{settings.api_v1_str}/message"

    message_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    send_span = uuid.uuid4().hex[:16]
    recv_span = uuid.uuid4().hex[:16]
    exchange = f"e2e-exchange-{uuid.uuid4().hex[:8]}"
    test_aic = f"urn:test:message:e2e:{uuid.uuid4().hex[:8]}"

    print("=== Message Dev Smoke ===")
    print(f"[INFO] monitor={monitor_url}, topic={topic}, message_id={message_id}, trace_id={trace_id}")

    from tests.support.factory import make_message_log_record
    from tests.support.kafka_helper import produce_message_event

    common = {
        "aic": test_aic,
        "system": "rabbitmq",
        "destination_name": exchange,
        "destination_kind": "exchange",
        "message_id": message_id,
        "trace_id": trace_id,
        "correlation_id": str(uuid.uuid4()),
    }

    events = [
        make_message_log_record(
            log_id=str(uuid.uuid4()),
            aic=test_aic,
            event_type="send",
            system="rabbitmq",
            destination_name=exchange,
            destination_kind="exchange",
            message_id=message_id,
            trace_id=trace_id,
            correlation_id=common["correlation_id"],
            span_id=send_span,
            parent_span_id="",
        ),
        make_message_log_record(
            log_id=str(uuid.uuid4()),
            aic=test_aic,
            event_type="receive",
            system="rabbitmq",
            destination_name=exchange,
            destination_kind="exchange",
            message_id=message_id,
            trace_id=trace_id,
            correlation_id=common["correlation_id"],
            span_id=recv_span,
            parent_span_id=send_span,
        ),
        make_message_log_record(
            log_id=str(uuid.uuid4()),
            aic=test_aic,
            event_type="ack",
            system="rabbitmq",
            destination_name=exchange,
            destination_kind="exchange",
            message_id=message_id,
            trace_id=trace_id,
            correlation_id=common["correlation_id"],
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=recv_span,
            settlement_latency_ms=12,
        ),
    ]

    dead_log_id = str(uuid.uuid4())
    dead_message_id = str(uuid.uuid4())
    dead_record = make_message_log_record(
        aic=test_aic,
        log_id=dead_log_id,
        event_type="dead_letter",
        system="rabbitmq",
        destination_name=exchange,
        destination_kind="exchange",
        message_id=dead_message_id,
        trace_id=uuid.uuid4().hex,
        span_id=uuid.uuid4().hex[:16],
        settlement_reason="e2e-injected-dead-letter",
    )

    try:
        for record in events:
            await produce_message_event(record=record, topic=topic, bootstrap_servers=bootstrap)
        await produce_message_event(record=dead_record, topic=topic, bootstrap_servers=bootstrap)
        print(f"[OK] 已投递 {len(events)} 条生命周期事件 + 1 条 dead_letter")
    except Exception as exc:
        print(f"[FAIL] Kafka 投递失败: {exc}", file=sys.stderr)
        sys.exit(1)

    import httpx

    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).isoformat()
    end = now.isoformat()

    async with httpx.AsyncClient(timeout=10.0) as client:

        async def events_hit() -> bool:
            resp = await client.post(
                f"{api_base}/events/query",
                json={
                    "timeRange": {"startAt": start, "endAt": end},
                    "filter": {
                        "conditions": [{"field": "messageId", "op": "eq", "value": message_id}],
                        "logic": "and",
                    },
                    "page": {"limit": 20},
                },
            )
            if resp.status_code != 200:
                return False
            items = resp.json().get("items", [])
            types = {item.get("eventType") for item in items}
            return {"send", "receive", "ack"}.issubset(types)

        if not await _poll_until(label="events/query 命中 send/receive/ack", check=events_hit):
            print(f"[DIAG] topic={topic}, trace_id={trace_id}", file=sys.stderr)
            sys.exit(1)

        async def lifecycle_hit() -> bool:
            resp = await client.get(f"{api_base}/lifecycles/{message_id}")
            if resp.status_code != 200:
                return False
            data = resp.json()
            return bool(
                data.get("sendCount", 0) >= 1
                and data.get("receiveCount", 0) >= 1
                and data.get("terminalState") == "ack"
            )

        if not await _poll_until(label=f"lifecycles/{message_id} sendCount=1 terminalState=ack", check=lifecycle_hit):
            resp = await client.get(f"{api_base}/lifecycles/{message_id}")
            print(f"[DIAG] lifecycles 响应: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        async def deadletter_hit() -> bool:
            resp = await client.post(
                f"{api_base}/deadletters/query",
                json={
                    "timeRange": {"startAt": start, "endAt": end},
                    "filter": {
                        "conditions": [{"field": "messageId", "op": "eq", "value": dead_message_id}],
                        "logic": "and",
                    },
                    "page": {"limit": 10},
                },
            )
            if resp.status_code not in (200, 204):
                return False
            if resp.status_code == 204:
                return True
            items = resp.json().get("items", [])
            return any(item.get("messageId") == dead_message_id for item in items)

        if not await _poll_until(label="deadletters/query 命中 dead_letter", check=deadletter_hit):
            print(f"[DIAG] dead_message_id={dead_message_id}", file=sys.stderr)
            sys.exit(1)

    print()
    print("[PASS] Message 消息→ClickHouse→Query API 通路验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
