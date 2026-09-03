"""tests/integration/test_metrics_writer_integration.py — MetricsWriter 集成测试（Step 4）。

使用真实 Redis（localhost:6379/db=3）+ respx 拦截 httpx（VictoriaMetrics）。
每条消息构造 Mock Kafka Message，直接调用 handle_message + _flush_batch 验证端到端写入路径。
运行：just test integration -k metrics_writer_integration
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from app.metrics.dedupe import claim_log_ids
from app.metrics.freshness import read_watermark
from app.metrics.snapshot_cache import get_snapshot
from app.metrics.tsdb import close_tsdb_client
from tests.support.constants import TEST_VM_QUERY_URL, TEST_VM_REMOTE_WRITE_URL
from tests.support.factory import make_metrics_log_record_bytes, make_window_metrics
from tests.support.redis_helper import reset_metrics_redis_state
from tests.support.vm_helper import decode_remote_write

pytestmark = pytest.mark.integration

BASE_TS_MS = 1_748_700_000_000  # 2025-05-31


def _msg(payload: bytes, *, offset: int = 0, timestamp: int = BASE_TS_MS, timestamp_type: int = 1) -> Any:
    """构造最小化 Kafka 消息（消除 AIOKafkaConsumer 依赖）。"""
    return SimpleNamespace(value=payload, offset=offset, timestamp=timestamp, timestamp_type=timestamp_type)


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
async def reset_tsdb_client():
    import os

    os.environ["VM_QUERY_URL"] = TEST_VM_QUERY_URL
    os.environ["VM_REMOTE_WRITE_URL"] = TEST_VM_REMOTE_WRITE_URL
    await close_tsdb_client()
    yield
    await close_tsdb_client()


@pytest.fixture
def writer(redis_client: Any):
    from unittest.mock import MagicMock

    from app.metrics.writer import MetricsWriter

    # 注入真实 Redis，但不实际连 Kafka Consumer
    w = MetricsWriter.__new__(MetricsWriter)
    w._redis = redis_client
    w._pending = []
    w._last_flush_monotonic = 0.0
    w._consumer = MagicMock()  # 不调用 start()
    w._running = True
    return w


# ── handle_message ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_message_appends_to_pending(writer: Any) -> None:
    """handle_message 将合法 metrics 记录加入 _pending。"""
    payload = make_metrics_log_record_bytes(aic="aic-001", log_id="lid-001")
    await writer.handle_message(_msg(payload))
    assert len(writer._pending) == 1
    assert writer._pending[0].aic == "aic-001"
    assert writer._pending[0].log_id == "lid-001"


@pytest.mark.asyncio
async def test_handle_message_non_metrics_skipped(writer: Any) -> None:
    """非 metrics 类型的记录不加入 _pending。"""
    raw = make_metrics_log_record_bytes(aic="aic-001")
    data = json.loads(raw)
    data["log_type"] = "audit"
    await writer.handle_message(_msg(json.dumps(data).encode()))
    assert writer._pending == []


# ── _flush_batch ──────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_flush_writes_to_vm_and_updates_redis(writer: Any, redis_client: Any) -> None:
    """成功 flush → Remote Write 调用 + snapshot cache 更新 + watermark 推进。"""
    import httpx

    write_route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=httpx.Response(204))

    payload = make_metrics_log_record_bytes(
        aic="aic-flush-001",
        log_id="lid-flush-001",
        windows=[make_window_metrics()],
    )
    await writer.handle_message(_msg(payload, timestamp=BASE_TS_MS))
    result = await writer._flush_batch(list(writer._pending))

    assert result is True
    assert write_route.called

    # snapshot cache 应已更新
    snap = await get_snapshot(redis_client, "aic-flush-001")
    assert snap is not None
    assert snap.aic == "aic-flush-001"

    # watermark 应已推进
    wm = await read_watermark(redis_client)
    assert wm is not None
    assert wm >= BASE_TS_MS


@respx.mock
@pytest.mark.asyncio
async def test_flush_dedupe_skips_duplicate(writer: Any, redis_client: Any) -> None:
    """重复 log_id 被 dedupe 过滤 → 不调用 Remote Write（幂等）。"""
    import httpx

    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=httpx.Response(204))

    payload = make_metrics_log_record_bytes(aic="aic-dup-001", log_id="lid-dup-001")

    # 先 claim 占住
    await claim_log_ids(redis_client, ["lid-dup-001"])

    await writer.handle_message(_msg(payload, timestamp=BASE_TS_MS))
    result = await writer._flush_batch(list(writer._pending))

    assert result is True
    assert not route.called  # 全部被 dedupe 跳过


@respx.mock
@pytest.mark.asyncio
async def test_flush_remote_write_failure_releases_dedupe(writer: Any, redis_client: Any) -> None:
    """Remote Write 失败 → 释放 dedupe 占用，下次可再 claim。"""
    import httpx

    respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=httpx.Response(503, text="unavailable"))

    payload = make_metrics_log_record_bytes(aic="aic-fail-001", log_id="lid-fail-001")
    await writer.handle_message(_msg(payload, timestamp=BASE_TS_MS))
    result = await writer._flush_batch(list(writer._pending))

    assert result is False

    # dedupe 应已 release，可再 claim
    claimed = await claim_log_ids(redis_client, ["lid-fail-001"])
    assert claimed == {"lid-fail-001"}


# ── 样本解码验证 ──────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_flush_writes_correct_metric_names(writer: Any) -> None:
    """写入 VM 的 protobuf 包含预期的 metric_name（__name__ 标签）。"""
    import httpx

    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=httpx.Response(204))

    payload = make_metrics_log_record_bytes(
        aic="aic-chk-001",
        log_id="lid-chk-001",
        uptime_seconds=123.0,
    )
    await writer.handle_message(_msg(payload, timestamp=BASE_TS_MS))
    await writer._flush_batch(list(writer._pending))

    assert route.called
    body = route.calls[0].request.content
    decoded = decode_remote_write(body)
    metric_names = {labels.get("__name__") for labels, _ in decoded}
    assert "amp_load_uptime_seconds" in metric_names
