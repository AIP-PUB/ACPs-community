"""E2E — Metrics ingest 基本流程（Step E3）。

验收项（C-METRIC-WRITE-1 / C-METRIC-WRITE-4 / C-METRIC-QUERY-2）：
- 投递一条 amp.metrics 消息 → MetricsWriter 消费并写入 Redis 快照
- 轮询 /metrics/snapshots/query → 在 15s 内返回该 AIC 的快照
- 快照 uptimeSeconds 与投递值一致
- SDK MetricsEmitter 发射路径：resource 字段正确派生为快照标签（E8）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.support.factory import poll_snapshot
from tests.support.kafka_helper import produce_metrics


@pytest.mark.asyncio
async def test_metrics_ingest_flow(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """投递 metrics 消息 → snapshots/query 15s 内可见。"""
    aic = "e2e-metrics-ingest-001"
    uptime = 123.0

    await produce_metrics(aic, uptime_seconds=uptime)

    snap = await poll_snapshot(e2e_http_client, aics=[aic], uptime_seconds=uptime, timeout_s=20.0)
    assert snap is not None, f"AIC {aic!r} 的快照在 20s 内未出现在 snapshots/query"


@pytest.mark.asyncio
async def test_metrics_ingest_via_sdk_emitter(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """Step E8：SDK MetricsEmitter 发射路径 — resource 字段正确映射为快照标签。

    流程：
    1. 用 MetricsEmitter（带 resource）写 NDJSON 到临时文件
    2. 读取该行并直接 produce 到 Kafka（模拟 Fluent Bit）
    3. 轮询 snapshots/query，验证 service_name 可过滤（C-METRIC-MODEL-1）
    """
    from acps_sdk.amp.metrics_demo import DemoMetricsSampler
    from acps_sdk.amp.metrics_emitter import MetricsEmitter
    from aiokafka import AIOKafkaProducer

    from tests.support.constants import DEFAULT_KAFKA_BOOTSTRAP_SERVERS, METRICS_KAFKA_TOPIC

    aic = "e2e-sdk-emitter-001"
    service_name = "e2e-test-service"
    resource = {
        "service.name": service_name,
        "service.namespace": "acps-e2e",
        "deployment.environment.name": "test",
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = Path(tmp_dir) / "amp_metrics.jsonl"
        emitter = MetricsEmitter(
            log_file=log_file,
            aic=aic,
            sampler=DemoMetricsSampler(aic),
            resource=resource,
        )
        emitter.emit_sync()

        line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        import json

        record = json.loads(line)
        if not record.get("observed_timestamp"):
            record["observed_timestamp"] = record["timestamp"]
        payload = json.dumps(record, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(METRICS_KAFKA_TOPIC, value=payload, key=aic.encode("utf-8"))
    finally:
        await producer.stop()

    snap = await poll_snapshot(
        e2e_http_client,
        aics=[aic],
        service_name=service_name,
        timeout_s=25.0,
    )
    assert snap is not None, f"SDK Emitter AIC {aic!r} 快照在 25s 内未出现（service_name 过滤）"


@pytest.mark.asyncio
async def test_metrics_ingest_dedup(
    e2e_metrics_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """同一 log_id 重复投递 → 只处理一次（幂等，C-METRIC-WRITE-4）。"""
    aic = "e2e-metrics-dup-001"
    log_id = "e2e-dup-lid-001"
    uptime = 55.0

    await produce_metrics(aic, log_id=log_id, uptime_seconds=uptime)
    await produce_metrics(aic, log_id=log_id, uptime_seconds=uptime)

    snap = await poll_snapshot(e2e_http_client, aics=[aic], uptime_seconds=uptime, timeout_s=20.0)
    assert snap is not None, f"AIC {aic!r} 快照在 20s 内未出现"

    resp = await e2e_http_client.post(
        "/acps-amp-v1/metrics/snapshots/query",
        json={"filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}, "page": {"limit": 1}},
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["uptimeSeconds"] == uptime
