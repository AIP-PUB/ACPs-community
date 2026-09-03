#!/usr/bin/env python3
"""Metrics 开发模式冒烟测试：直连 Kafka 投递 metrics 消息 → 轮询 snapshots/query 确认快照可见。

前置：infra（kafka+redis+victoria-metrics）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/smoke_metrics.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证：
  Kafka amp.metrics → MetricsWriter → Redis 快照缓存 → /metrics/snapshots/query
"""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    # ── 0. 加载配置 ───────────────────────────────────────────────────────────
    from app.core.config import settings

    monitor_url = f"http://localhost:{settings.uvicorn_port}"
    bootstrap = settings.kafka_bootstrap_servers
    topic = settings.metrics_topic
    test_aic = "urn:test:metrics:e2e"

    print("=== Metrics Dev Smoke ===")
    print(f"[INFO] monitor: {monitor_url}, topic: {topic}, aic: {test_aic}")

    # ── 1. 构造 MetricsLogRecord ──────────────────────────────────────────────
    from acps_sdk.amp.models import LoadMetrics, MetricsBody, MetricsLogRecord, WindowMetrics

    now_iso = datetime.now(UTC).isoformat()
    record = MetricsLogRecord(
        log_id=MetricsLogRecord.new_log_id(),
        timestamp=now_iso,
        aic=test_aic,
        body=MetricsBody(
            uptime_seconds=42.0,
            load_metrics=LoadMetrics(active_tasks=2, queued_tasks=1),
            window_metrics=[
                WindowMetrics(window="PT5M", success_rate=99.5),
            ],
        ),
        resource={
            "service.name": "e2e-test-service",
            "service.namespace": "acps-e2e",
            "deployment.environment.name": "dev",
        },
    )
    # observed_timestamp 作为 §2.3 LogAppendTime 缺失时的回退，保证 Writer 可解析稳定 observedAt
    record_dict = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    record_dict["observed_timestamp"] = now_iso
    payload = json.dumps(record_dict, ensure_ascii=False).encode("utf-8")

    # ── 2. 投递到 Kafka amp.metrics ───────────────────────────────────────────
    try:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap,
            security_protocol="PLAINTEXT",
        )
        await producer.start()
        try:
            await producer.send_and_wait(
                topic,
                value=payload,
                key=test_aic.encode("utf-8"),
            )
        finally:
            await producer.stop()
        print(f"[OK] 已投递 metrics 消息: aic={test_aic}, log_id={record.log_id}")
    except Exception as exc:
        print(f"[FAIL] Kafka 投递失败: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── 3. 轮询 /metrics/snapshots/query 直到快照出现或超时 ───────────────────
    import httpx

    timeout = 20.0
    poll_interval = 0.5
    url = f"{monitor_url}/acps-amp-v1/metrics/snapshots/query"
    deadline = time.monotonic() + timeout
    found = False
    uptime_val = None

    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(
                    url,
                    json={
                        "filter": {"conditions": [{"field": "aic", "op": "eq", "value": test_aic}]},
                        "page": {"limit": 1},
                    },
                )
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    if items and items[0].get("aic") == test_aic:
                        uptime_val = items[0].get("uptimeSeconds")
                        found = True
                        break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(poll_interval)

    if found:
        print(f"[OK] 快照可见: aic={test_aic}, uptimeSeconds={uptime_val}")
    else:
        print(f"[FAIL] 超时 {timeout}s 后 snapshots/query 未返回 {test_aic!r}", file=sys.stderr)
        sys.exit(1)

    # ── 4. 验证 watermark 已推进 ──────────────────────────────────────────────
    try:
        from app.core.redis_client import get_redis
        from app.metrics.freshness import read_watermark

        redis = get_redis()
        wm = await read_watermark(redis)
        if wm is not None and wm > 0:
            print(f"[OK] freshness watermark 已推进: {wm} ms")
        else:
            print(f"[WARN] watermark 未推进（wm={wm}）—— MetricsWriter 可能尚未处理")
    except Exception as exc:
        print(f"[WARN] watermark 检查失败: {exc}（非致命）")

    print()
    print("[PASS] Metrics 消息→Redis快照→Query API 通路验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
