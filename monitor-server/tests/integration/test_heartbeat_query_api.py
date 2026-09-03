"""tests/integration/test_heartbeat_query_api.py — Heartbeat Query API 集成测试（§9.3）。

需要 Redis 7+ 在 localhost:6379 可用。
运行：just test integration -k heartbeat_query_api
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from tests.support.redis_helper import (
    ensure_functions_for_tests,
    reset_heartbeat_redis_state,
    seed_heartbeat,
)

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def isolated_redis(redis_client: object) -> AsyncGenerator[None]:
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]
    yield
    await reset_heartbeat_redis_state(redis_client)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
async def loaded_functions(redis_client: object) -> None:
    await ensure_functions_for_tests(redis_client)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
async def http_client() -> AsyncGenerator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── GET /liveness/{aic} ───────────────────────────────────────────────────────


class TestGetLiveness:
    async def test_returns_200_when_aic_exists(self, http_client, redis_client, loaded_functions) -> None:
        """AIC 存在时返回 200 + liveness 视图。"""
        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic="aic-query-001", observed_at_ms=now_ms - 500)

        resp = await http_client.get(f"{settings.api_v1_str}/heartbeat/liveness/aic-query-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["aic"] == "aic-query-001"
        assert body["data"]["isAlive"] is True
        assert "meta" in body
        assert "evaluatedAt" in body["meta"]
        assert "silenceThresholdSeconds" in body["meta"]
        assert "evictAfterSeconds" in body["meta"]
        assert "dataFreshnessAt" in body["meta"]

    async def test_returns_404_when_aic_not_found(self, http_client, redis_client, loaded_functions) -> None:
        """AIC 不存在时返回 404 + AMP 错误体。"""
        resp = await http_client.get(f"{settings.api_v1_str}/heartbeat/liveness/nonexistent-aic")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "AMP_HEARTBEAT_AIC_UNKNOWN"

    async def test_silent_aic_is_alive_false(self, http_client, redis_client, loaded_functions) -> None:
        """超过 silence_threshold 的 AIC，isAlive=False。"""
        silence_ms = settings.heartbeat_silence_threshold_seconds * 1000
        old_ms = int(time.time() * 1000) - silence_ms - 3_000
        await seed_heartbeat(redis_client, aic="aic-silent-001", observed_at_ms=old_ms)

        resp = await http_client.get(f"{settings.api_v1_str}/heartbeat/liveness/aic-silent-001")
        assert resp.status_code == 200
        assert resp.json()["data"]["isAlive"] is False
        assert resp.json()["data"]["livenessState"] == "silent"


# ── POST /liveness/query ──────────────────────────────────────────────────────


class TestQueryLiveness:
    async def test_aic_in_filter_returns_matching_aics(self, http_client, redis_client, loaded_functions) -> None:
        """aic in [..] 过滤器返回匹配的 AIC 列表。"""
        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic="aic-q-001", observed_at_ms=now_ms - 500)
        await seed_heartbeat(redis_client, aic="aic-q-002", observed_at_ms=now_ms - 600)

        resp = await http_client.post(
            f"{settings.api_v1_str}/heartbeat/liveness/query",
            json={"filter": {"conditions": [{"field": "aic", "op": "in", "value": ["aic-q-001", "aic-q-002"]}]}},
        )
        assert resp.status_code == 200
        body = resp.json()
        aics = [item["data"]["aic"] for item in body["items"]]
        assert "aic-q-001" in aics
        assert "aic-q-002" in aics

    async def test_query_without_filter_returns_400(self, http_client, redis_client, loaded_functions) -> None:
        """无 filter 查询返回 400（C-QUERY-1）。"""
        resp = await http_client.post(
            f"{settings.api_v1_str}/heartbeat/liveness/query",
            json={},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "AMP_QUERY_REQUIRES_SELECTIVE_FILTER"

    async def test_query_with_time_range_returns_422(self, http_client, redis_client, loaded_functions) -> None:
        """timeRange 字段存在时返回 422（P2-11）。"""
        resp = await http_client.post(
            f"{settings.api_v1_str}/heartbeat/liveness/query",
            json={
                "filter": {"conditions": [{"field": "aic", "op": "eq", "value": "x"}]},
                "timeRange": {"startAt": "2024-01-01T00:00:00Z", "endAt": "2024-01-02T00:00:00Z"},
            },
        )
        assert resp.status_code == 422

    async def test_meta_fields_present(self, http_client, redis_client, loaded_functions) -> None:
        """响应 meta 包含必填字段（spec §6.2.1）。"""
        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic="aic-meta-001", observed_at_ms=now_ms - 500)

        resp = await http_client.post(
            f"{settings.api_v1_str}/heartbeat/liveness/query",
            json={"filter": {"conditions": [{"field": "aic", "op": "eq", "value": "aic-meta-001"}]}},
        )
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert "evaluatedAt" in meta
        assert "silenceThresholdSeconds" in meta
        assert "evictAfterSeconds" in meta
        assert "dataFreshnessAt" in meta


# ── GET /summary ───────────────────────────────────────────────────────────────


class TestGetSummary:
    async def test_returns_200_with_counts(self, http_client, redis_client, loaded_functions) -> None:
        """返回 200 + 正确的 alive/silent/total 计数。"""
        now_ms = int(time.time() * 1000)
        await seed_heartbeat(redis_client, aic="aic-s-001", observed_at_ms=now_ms - 500)
        await seed_heartbeat(redis_client, aic="aic-s-002", observed_at_ms=now_ms - 500)

        resp = await http_client.get(f"{settings.api_v1_str}/heartbeat/summary")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["totalKnown"] == 2
        assert data["aliveCount"] == 2
        assert data["silentCount"] == 0
        assert "meta" in body

    async def test_summary_meta_has_required_fields(self, http_client, redis_client, loaded_functions) -> None:
        """summary meta 包含必填字段。"""
        resp = await http_client.get(f"{settings.api_v1_str}/heartbeat/summary")
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        for field in ["evaluatedAt", "silenceThresholdSeconds", "evictAfterSeconds", "dataFreshnessAt"]:
            assert field in meta, f"meta 缺少 {field}"
