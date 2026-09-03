#!/usr/bin/env python3
"""Access 开发模式冒烟测试：直连 Kafka 投递 access 消息 → 轮询 events/query 确认可见。

前置：infra（kafka+redis+clickhouse）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/e2e_access_verify.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证：
  Kafka amp.access → AccessWriter → ClickHouse → /access/events/query
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    from app.core.config import settings

    monitor_url = f"http://localhost:{settings.uvicorn_port}"
    bootstrap = settings.kafka_bootstrap_servers
    topic = settings.access_topic
    test_aic = f"urn:test:access:e2e:{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex

    print("=== Access Dev Smoke ===")
    print(f"[INFO] monitor: {monitor_url}, topic: {topic}, aic: {test_aic}, log_id: {log_id}")

    from tests.support.factory import make_access_log_record
    from tests.support.kafka_helper import produce_access_event

    record = make_access_log_record(
        aic=test_aic,
        log_id=log_id,
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id="",
        method="POST",
        route="/rpc",
        response_status=200,
        duration_ms=42,
        service_name="e2e-access-svc",
        observed_timestamp=datetime.now(UTC).isoformat(),
    )

    try:
        await produce_access_event(record=record, topic=topic, bootstrap_servers=bootstrap)
        print(f"[OK] 已投递 access 消息: log_id={log_id}")
    except Exception as exc:
        print(f"[FAIL] Kafka 投递失败: {exc}", file=sys.stderr)
        sys.exit(1)

    import httpx

    timeout = 25.0
    poll_interval = 0.5
    url = f"{monitor_url}/acps-amp-v1/access/events/query"
    start = (datetime.now(UTC) - __import__("datetime").timedelta(hours=1)).isoformat()
    end = datetime.now(UTC).isoformat()
    deadline = time.monotonic() + timeout
    found = False

    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(
                    url,
                    json={
                        "timeRange": {"startAt": start, "endAt": end},
                        "filter": {"conditions": [{"field": "aic", "op": "eq", "value": test_aic}]},
                        "page": {"limit": 20},
                    },
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if any(item.get("logId") == log_id or item.get("log_id") == log_id for item in items):
                        found = True
                        break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(poll_interval)

    if found:
        print(f"[OK] events/query 命中 log_id={log_id}")
    else:
        print(
            f"[FAIL] 超时 {timeout}s：events/query 未返回 log_id={log_id}（topic={topic}）",
            file=sys.stderr,
        )
        sys.exit(1)

    print()
    print("[PASS] Access 消息→ClickHouse→Query API 通路验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
