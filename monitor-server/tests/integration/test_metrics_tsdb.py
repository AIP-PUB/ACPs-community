"""tests/integration/test_metrics_tsdb.py — tsdb.py 集成测试（respx 拦截，Step 4）。

使用 respx 拦截 httpx 调用，无需真实 VictoriaMetrics。
运行：just test integration -k metrics_tsdb
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx

from app.metrics.exception import RemoteWriteError
from app.metrics.samples import Sample
from app.metrics.tsdb import (
    InstantSample,
    RangeSeries,
    close_tsdb_client,
    instant,
    instant_many,
    range_many,
    range_query,
    remote_write,
)
from tests.support.constants import TEST_VM_QUERY_URL, TEST_VM_REMOTE_WRITE_URL
from tests.support.vm_helper import (
    decode_remote_write,
    matrix_result,
    vector_result,
)

pytestmark = pytest.mark.integration

AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
START = datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC)
END = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def reset_tsdb():
    """每个测试前后关闭 httpx 单例，确保 TEST_VM_* URL 配置生效。"""
    import os

    os.environ["VM_QUERY_URL"] = TEST_VM_QUERY_URL
    os.environ["VM_REMOTE_WRITE_URL"] = TEST_VM_REMOTE_WRITE_URL
    await close_tsdb_client()
    yield
    await close_tsdb_client()


# ── instant ───────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_instant_parses_vector_result() -> None:
    """instant() 正确解析 vector 响应。"""
    ts_ms = int(AT.timestamp() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=__import__("httpx").Response(
            200,
            json=vector_result(({"aic": "a001", "__name__": "amp_uptime_seconds"}, 42.5, ts_ms)),
        )
    )

    results = await instant("amp_uptime_seconds{aic='a001'}", at=AT)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, InstantSample)
    assert r.labels["aic"] == "a001"
    assert r.value == pytest.approx(42.5)


@respx.mock
@pytest.mark.asyncio
async def test_instant_returns_empty_on_no_data() -> None:
    """instant() 空响应返回空列表。"""
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=__import__("httpx").Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )
    )
    results = await instant("absent_metric", at=AT)
    assert results == []


@respx.mock
@pytest.mark.asyncio
async def test_instant_raises_on_non_200() -> None:
    """instant() 非 200 响应抛出 RemoteWriteError。"""
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=__import__("httpx").Response(500, text="Internal Error")
    )
    with pytest.raises(RemoteWriteError):
        await instant("amp_uptime_seconds", at=AT)


# ── instant_many ──────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_instant_many_concurrent() -> None:
    """instant_many() 并发发出所有请求，返回 key→results 映射。"""
    import httpx

    ts_ms = int(AT.timestamp() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json=vector_result(({"aic": "x"}, 10.0, ts_ms)),
        )
    )

    exprs = {"cpu": "amp_cpu_usage{aic='x'}", "mem": "amp_mem_usage{aic='x'}"}
    results = await instant_many(exprs, at=AT)
    assert set(results.keys()) == {"cpu", "mem"}
    assert len(results["cpu"]) == 1
    assert results["cpu"][0].value == pytest.approx(10.0)


# ── range_query ───────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_range_query_parses_matrix_result() -> None:
    """range_query() 正确解析 matrix 响应。"""
    import httpx

    ts_ms = int(START.timestamp() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json=matrix_result(({"aic": "a001"}, [(ts_ms, 1.0), (ts_ms + 60_000, 2.0)])),
        )
    )

    results = await range_query("amp_uptime_seconds", start=START, end=END, step_ms=60_000)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, RangeSeries)
    assert r.labels["aic"] == "a001"
    assert len(r.points) == 2
    assert r.points[0][1] == pytest.approx(1.0)
    assert r.points[1][1] == pytest.approx(2.0)


@respx.mock
@pytest.mark.asyncio
async def test_range_many_returns_keyed_results() -> None:
    """range_many() 并发返回 key→results 映射。"""
    import httpx

    ts_ms = int(START.timestamp() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json=matrix_result(({"aic": "b001"}, [(ts_ms, 5.5)])),
        )
    )

    exprs = {"rate": "rate(amp_requests_total[5m])"}
    results = await range_many(exprs, start=START, end=END, step_ms=60_000)
    assert "rate" in results
    assert results["rate"][0].points[0][1] == pytest.approx(5.5)


# ── remote_write ──────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_remote_write_sends_correct_payload() -> None:
    """remote_write() 发送 snappy 压缩的 protobuf 并可被 decode_remote_write 反解。"""
    import httpx

    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=httpx.Response(204))

    samples = [
        Sample(
            metric_name="amp_uptime_seconds",
            labels={"aic": "aic-001"},
            value=100.0,
            timestamp_ms=1_700_000_000_000,
        ),
        Sample(
            metric_name="amp_uptime_seconds",
            labels={"aic": "aic-001"},
            value=200.0,
            timestamp_ms=1_700_000_060_000,
        ),
    ]
    await remote_write(samples)

    assert route.called
    body = route.calls[0].request.content
    decoded = decode_remote_write(body)
    assert len(decoded) == 1
    labels, points = decoded[0]
    assert labels["__name__"] == "amp_uptime_seconds"
    assert labels["aic"] == "aic-001"
    assert len(points) == 2


@respx.mock
@pytest.mark.asyncio
async def test_remote_write_empty_samples_is_noop() -> None:
    """remote_write([]) 不发送 HTTP 请求。"""
    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(return_value=__import__("httpx").Response(204))
    await remote_write([])
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_remote_write_retries_on_transient_5xx() -> None:
    """remote_write() 在瞬时 5xx 时有界重试后成功。"""
    import httpx

    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(204),
        ]
    )
    samples = [Sample(metric_name="amp_test", labels={}, value=1.0, timestamp_ms=1_700_000_000_000)]
    await remote_write(samples)
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_remote_write_raises_after_exhausted_retries() -> None:
    """remote_write() 连续失败 3 次后抛出 RemoteWriteError。"""
    import httpx

    route = respx.post(f"{TEST_VM_REMOTE_WRITE_URL}/api/v1/write").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    samples = [Sample(metric_name="amp_test", labels={}, value=1.0, timestamp_ms=1_700_000_000_000)]
    with pytest.raises(RemoteWriteError):
        await remote_write(samples)
    assert route.call_count == 3
