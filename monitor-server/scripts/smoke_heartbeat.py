#!/usr/bin/env python3
"""Heartbeat 开发模式冒烟测试：直连 Kafka 投递心跳 → 轮询 Query API 确认 alive。

前置：infra（kafka+redis）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/smoke_heartbeat.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证"消息 → Redis → Query API"。
"""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    # ── 0. 加载配置 ───────────────────────────────────────────────────────────
    from app.core.config import settings

    monitor_url = f"http://localhost:{settings.uvicorn_port}"
    prefix = f"{settings.api_v1_str}/heartbeat"
    test_aic = "urn:test:heartbeat:e2e"
    partition_count = settings.heartbeat_input_partition_count
    bootstrap = settings.kafka_bootstrap_servers
    topic = settings.heartbeat_topic

    print("=== Heartbeat Dev Smoke ===")
    print(f"[INFO] monitor: {monitor_url}, topic: {topic}, aic: {test_aic}")

    # ── 1. 构造完整 HeartbeatLogRecord ───────────────────────────────────────
    from acps_sdk.amp import HeartbeatBody, HeartbeatLogRecord

    from app.heartbeat.sharding import input_partition_for_aic

    record = HeartbeatLogRecord(
        log_id=HeartbeatLogRecord.new_log_id(),
        timestamp=datetime.now(UTC).isoformat(),
        aic=test_aic,
        body=HeartbeatBody(uptime_seconds=1.0),
    )
    partition = input_partition_for_aic(test_aic, partition_count)
    payload = json.dumps(
        record.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
    ).encode("utf-8")

    # ── 2. 投递到 Kafka amp.heartbeat ─────────────────────────────────────────
    try:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
        await producer.start()
        try:
            await producer.send_and_wait(
                topic,
                value=payload,
                key=test_aic.encode("utf-8"),
                partition=partition,
            )
        finally:
            await producer.stop()
        print(f"[OK] 已投递心跳: aic={test_aic}, partition={partition}")
    except Exception as exc:
        print(f"[FAIL] Kafka 投递失败: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── 3. 轮询 Query API 直到 isAlive=true 或超时 ──────────────────────────
    import httpx

    timeout = 15.0
    poll_interval = 0.5
    url = f"{monitor_url}{prefix}/liveness/{test_aic}"
    deadline = time.monotonic() + timeout
    is_alive = False
    liveness_state = ""

    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    is_alive = data.get("isAlive", False)
                    liveness_state = data.get("livenessState", "")
                    if is_alive:
                        break
            except httpx.HTTPError:
                continue
            await asyncio.sleep(poll_interval)

    if is_alive:
        print(f"[OK] liveness: isAlive={is_alive}, livenessState={liveness_state}")
    else:
        print(f"[FAIL] 超时 {timeout}s 后 isAlive 仍为 False (livenessState={liveness_state})", file=sys.stderr)
        sys.exit(1)

    # ── 4. 验证 summary ────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{monitor_url}{prefix}/summary")
            if resp.status_code == 200:
                alive_count = resp.json().get("data", {}).get("aliveCount", 0)
                print(f"[OK] summary: aliveCount={alive_count}")
            else:
                print(f"[WARN] summary 返回 {resp.status_code}（非致命）")
        except Exception as exc:
            print(f"[WARN] summary 查询失败: {exc}（非致命）")

    print()
    print("[PASS] Heartbeat 消息→Redis→Query API 通路验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
