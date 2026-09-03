#!/usr/bin/env python3
"""System 开发模式冒烟测试：直连 Kafka 投递 system 消息 → 轮询 events/query 确认可见。

前置：infra（kafka+redis+opensearch）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/e2e_system_verify.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证：
  Kafka amp.system → SystemWriter → OpenSearch → /system/events/query
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    from app.core.config import settings

    monitor_url = f"http://localhost:{settings.uvicorn_port}"
    bootstrap = settings.kafka_bootstrap_servers
    topic = settings.system_topic
    test_aic = f"urn:test:system:e2e:{uuid.uuid4().hex[:8]}"
    correlation_id = f"e2e-verify-{uuid.uuid4().hex[:12]}"
    log_id = str(uuid.uuid4())

    print("=== System Dev Smoke ===")
    print(
        f"[INFO] monitor: {monitor_url}, topic: {topic}, "
        f"aic: {test_aic}, correlation_id: {correlation_id}, log_id: {log_id}"
    )

    from tests.support.factory import make_system_log_record
    from tests.support.kafka_helper import produce_system_event

    record = make_system_log_record(
        aic=test_aic,
        log_id=log_id,
        message="e2e-system-verify",
        category="test",
        component="verify",
        module_name="script",
        severity_number=9,
        severity_text="INFO",
        correlation_id=correlation_id,
        observed_timestamp=datetime.now(UTC).isoformat(),
    )

    try:
        await produce_system_event(record=record, topic=topic, bootstrap_servers=bootstrap)
        print(f"[OK] 已投递 system 消息: log_id={log_id}")
    except Exception as exc:
        print(f"[FAIL] Kafka 投递失败: {exc}", file=sys.stderr)
        sys.exit(1)

    import httpx

    timeout = 60.0
    poll_interval = 2.0
    url = f"{monitor_url}/acps-amp-v1/system/events/query"
    start = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    end = datetime.now(UTC).isoformat()
    deadline = time.monotonic() + timeout
    found = False
    last_status = None
    last_body = ""

    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(
                    url,
                    json={
                        "timeRange": {"startAt": start, "endAt": end},
                        "filter": {"conditions": [{"field": "correlationId", "op": "eq", "value": correlation_id}]},
                        "page": {"limit": 20},
                    },
                )
                last_status = resp.status_code
                last_body = resp.text[:500]
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if any(item.get("logId") == log_id or item.get("log_id") == log_id for item in items):
                        found = True
                        break
            except httpx.HTTPError as exc:
                last_body = str(exc)
            await asyncio.sleep(poll_interval)

    if found:
        print(f"[OK] events/query 命中 log_id={log_id} (correlationId={correlation_id})")
    else:
        print(
            f"[FAIL] 超时 {timeout}s：events/query 未返回 correlationId={correlation_id} "
            f"(topic={topic}, last_http={last_status}, body={last_body!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    print()
    print("[PASS] System 消息→OpenSearch→Query API 通路验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
