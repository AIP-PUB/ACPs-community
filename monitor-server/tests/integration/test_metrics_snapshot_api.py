"""tests/integration/test_metrics_snapshot_api.py — snapshots/query API 集成测试（Step 4）。

使用真实 Redis + ASGI transport，通过 seed_snapshot 注入数据后 POST snapshots/query 验证响应。
运行：just test integration -k metrics_snapshot_api
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from tests.support.constants import TEST_VM_QUERY_URL, TEST_VM_REMOTE_WRITE_URL
from tests.support.redis_helper import reset_metrics_redis_state, seed_snapshot, seed_watermark
from tests.support.vm_helper import empty_vector, vector_result

pytestmark = pytest.mark.integration

BASE_TS_MS = 1_748_700_000_000  # 2025-05-31
_PREFIX = "/acps-amp-v1/metrics"


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    import time

    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]
    # seed a recent watermark so freshness check passes in testing env (lagging_threshold_ms=5000ms)
    await seed_watermark(redis_client, int(time.time() * 1000))  # type: ignore[arg-type]
    yield
    await reset_metrics_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
async def reset_tsdb_client():
    import os

    from app.metrics.tsdb import close_tsdb_client

    os.environ["VM_QUERY_URL"] = TEST_VM_QUERY_URL
    os.environ["VM_REMOTE_WRITE_URL"] = TEST_VM_REMOTE_WRITE_URL
    await close_tsdb_client()
    yield
    await close_tsdb_client()


@pytest.fixture
async def http_client() -> AsyncGenerator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── snapshots/query ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_query_returns_seeded_agents(http_client: AsyncClient, redis_client: object) -> None:
    """snapshots/query 返回已注入的快照（Redis → snapshot_service → API）。"""
    await seed_snapshot(redis_client, aic="aic-snap-001", observed_at_ms=BASE_TS_MS)  # type: ignore[arg-type]
    await seed_snapshot(redis_client, aic="aic-snap-002", observed_at_ms=BASE_TS_MS + 1000)  # type: ignore[arg-type]

    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={
            "filter": {"conditions": [{"field": "aic", "op": "in", "value": ["aic-snap-001", "aic-snap-002"]}]},
            "page": {"limit": 20},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    returned_aics = {item["aic"] for item in data["items"]}
    assert "aic-snap-001" in returned_aics
    assert "aic-snap-002" in returned_aics


@pytest.mark.asyncio
async def test_snapshot_query_empty_returns_200(http_client: AsyncClient) -> None:
    """snapshots/query 无快照时返回空 items，不报错（C-METRIC-QUERY-2 兜底）。"""
    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={"page": {"limit": 20}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


@pytest.mark.asyncio
async def test_snapshot_query_specific_aics_filter(http_client: AsyncClient, redis_client: object) -> None:
    """指定 aic 过滤时只返回对应的快照。"""
    await seed_snapshot(redis_client, aic="aic-in-set", observed_at_ms=BASE_TS_MS)  # type: ignore[arg-type]
    await seed_snapshot(redis_client, aic="aic-out-of-set", observed_at_ms=BASE_TS_MS)  # type: ignore[arg-type]

    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={
            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": "aic-in-set"}]},
            "page": {"limit": 20},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    returned_aics = {item["aic"] for item in data["items"]}
    assert "aic-in-set" in returned_aics
    assert "aic-out-of-set" not in returned_aics


@pytest.mark.asyncio
async def test_snapshot_query_pagination(http_client: AsyncClient, redis_client: object) -> None:
    """snapshots/query 分页：limit=1 只返回最新一条。"""
    await seed_snapshot(redis_client, aic="aic-pg-001", observed_at_ms=BASE_TS_MS)  # type: ignore[arg-type]
    await seed_snapshot(redis_client, aic="aic-pg-002", observed_at_ms=BASE_TS_MS + 1000)  # type: ignore[arg-type]

    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={"page": {"limit": 1}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_snapshot_query_invalid_pagination_returns_422(http_client: AsyncClient) -> None:
    """非法分页参数（limit=0，违反 ge=1 约束）→ 422 Unprocessable Entity。"""
    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={"page": {"limit": 0}},
    )
    assert resp.status_code == 422


@respx.mock
@pytest.mark.asyncio
async def test_snapshot_repair_from_tsdb_backfills_redis(
    http_client: AsyncClient,
    redis_client: object,
) -> None:
    """索引存在但 Hash 缺失 → TSDB exact-anchor 修复并异步回填 Redis。"""
    import httpx

    from app.metrics.snapshot_cache import SNAPSHOT_INDEX_KEY, get_snapshot, snapshot_hash_key

    aic = "aic-repair-001"
    anchor_ms = BASE_TS_MS + 5000
    anchor_s = anchor_ms / 1000

    await redis_client.zadd(SNAPSHOT_INDEX_KEY, {aic: anchor_ms})  # type: ignore[attr-defined]
    assert await redis_client.exists(snapshot_hash_key(aic)) == 0  # type: ignore[attr-defined]

    def _query_side_effect(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if "amp_snapshot_present" in query and "tlast_over_time" in query:
            return httpx.Response(
                200,
                json=vector_result(({"aic": aic}, anchor_s, anchor_ms)),
            )
        if "uptime_seconds" in query and "tlast_over_time" in query:
            return httpx.Response(
                200,
                json=vector_result(({"aic": aic}, anchor_s, anchor_ms)),
            )
        if "uptime_seconds" in query and "last_over_time" in query:
            return httpx.Response(
                200,
                json=vector_result(({"aic": aic}, 42.0, anchor_ms)),
            )
        return httpx.Response(200, json=empty_vector())

    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(side_effect=_query_side_effect)

    resp = await http_client.post(
        f"{_PREFIX}/snapshots/query",
        json={
            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]},
            "page": {"limit": 20},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["aic"] == aic
    assert data["items"][0]["uptimeSeconds"] == pytest.approx(42.0)

    backfilled = None
    for _ in range(20):
        backfilled = await get_snapshot(redis_client, aic)  # type: ignore[arg-type]
        if backfilled is not None:
            break
        await asyncio.sleep(0.05)

    assert backfilled is not None
    assert backfilled.aic == aic
    assert backfilled.observed_at_ms == anchor_ms
    assert backfilled.uptime_seconds == pytest.approx(42.0)
