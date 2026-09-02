"""tests/integration/test_metrics_query_api.py — Metrics Query API 集成测试（Step 4）。

通过 ASGI transport + respx 拦截验证 HTTP 层 → service → tsdb 链路。
运行：just test integration -k metrics_query_api
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.support.constants import TEST_VM_QUERY_URL, TEST_VM_REMOTE_WRITE_URL
from tests.support.redis_helper import reset_metrics_redis_state, seed_watermark
from tests.support.vm_helper import empty_matrix, matrix_result, vector_result

pytestmark = pytest.mark.integration

BASE_TS_MS = 1_748_700_000_000
_PREFIX = "/acps-amp-v1/metrics"


def _recent_time_range() -> tuple[str, str]:
    """返回最近 30 分钟的 ISO 8601 时间范围，确保在 testing retention 内。"""
    end = datetime.now(UTC)
    start = end - timedelta(minutes=30)
    return start.isoformat(), end.isoformat()


@pytest.fixture(scope="session")
async def redis_client():
    from app.core.redis_client import close_redis, get_redis

    r = get_redis()
    yield r
    await close_redis()


@pytest.fixture(autouse=True)
async def reset_redis_watermark(redis_client: object) -> AsyncGenerator[None]:
    """每个测试前后重置 Redis 状态并注入水位，避免 503 AMP_READ_MODEL_LAGGING。"""
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


# ── /metrics/series/query ─────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_series_query_returns_200(http_client: AsyncClient) -> None:
    """series/query 返回 200 及至少 0 条结果（正常流程）。"""
    import httpx

    start_iso, end_iso = _recent_time_range()
    now_ms = int(time.time() * 1000)
    ts_ms_1 = now_ms - 60_000
    ts_ms_2 = now_ms
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json=matrix_result(
                ({"aic": "aic-001", "__name__": "amp_load_uptime_seconds"}, [(ts_ms_1, 100.0), (ts_ms_2, 110.0)])
            ),
        )
    )

    resp = await http_client.post(
        f"{_PREFIX}/series/query",
        json={
            "metric": "uptimeSeconds",
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "meta" in data


@respx.mock
@pytest.mark.asyncio
async def test_series_query_empty_result(http_client: AsyncClient) -> None:
    """series/query 无数据时返回空 items。"""
    import httpx

    start_iso, end_iso = _recent_time_range()
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query_range").mock(return_value=httpx.Response(200, json=empty_matrix()))

    resp = await http_client.post(
        f"{_PREFIX}/series/query",
        json={
            "metric": "uptimeSeconds",
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


@pytest.mark.asyncio
async def test_series_query_missing_metric_returns_422(http_client: AsyncClient) -> None:
    """series/query 缺少 metric 字段 → 422 Unprocessable Entity。"""
    start_iso, end_iso = _recent_time_range()
    resp = await http_client.post(
        f"{_PREFIX}/series/query",
        json={"timeRange": {"startAt": start_iso, "endAt": end_iso}},
    )
    assert resp.status_code == 422


# ── /metrics/rankings/query ───────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_rankings_query_returns_200(http_client: AsyncClient) -> None:
    """rankings/query 返回 200。"""
    import httpx

    start_iso, end_iso = _recent_time_range()
    now_ms = int(time.time() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json=vector_result(
                ({"aic": "aic-top1", "__name__": "amp_load_uptime_seconds"}, 999.0, now_ms),
                ({"aic": "aic-top2", "__name__": "amp_load_uptime_seconds"}, 888.0, now_ms),
            ),
        )
    )

    resp = await http_client.post(
        f"{_PREFIX}/rankings/query",
        json={
            "metric": "uptimeSeconds",
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
            "aggregation": "latest",
            "direction": "desc",
            "topN": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


# ── /metrics/slo/evaluate ─────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_slo_evaluate_returns_200(http_client: AsyncClient) -> None:
    """slo/evaluate 返回 200 + summary + items。"""
    import httpx

    start_iso, end_iso = _recent_time_range()
    now_ms = int(time.time() * 1000)
    respx.get(f"{TEST_VM_QUERY_URL}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json=vector_result(
                ({"aic": "aic-slo-001"}, 99.5, now_ms),
            ),
        )
    )

    resp = await http_client.post(
        f"{_PREFIX}/slo/evaluate",
        json={
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
            "rules": [
                {
                    "sli": "success_rate",
                    "window": "PT5M",
                    "target": 95.0,
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "items" in data


# ── Profile 开关（§6.19 / §9.3） ───────────────────────────────────────────────


def _delegating_settings(*, analytics_enabled: bool, governance_enabled: bool) -> object:
    """返回委托真实 Settings 的对象，仅覆盖 profile 开关。"""
    from app.core.config import get_settings

    real = get_settings()

    class _Delegating:
        def __getattr__(self, name: str) -> object:
            if name == "metrics_analytics_enabled":
                return analytics_enabled
            if name == "metrics_governance_enabled":
                return governance_enabled
            return getattr(real, name)

    return _Delegating()


def _build_profile_app(*, analytics_enabled: bool, governance_enabled: bool) -> FastAPI:
    """按 profile 开关重新装配 metrics router（隔离于 main.app）。"""
    import importlib
    import sys

    from fastapi import FastAPI

    fake = _delegating_settings(
        analytics_enabled=analytics_enabled,
        governance_enabled=governance_enabled,
    )
    sys.modules.pop("app.metrics.api", None)
    import app.core.config as config_mod

    original_get_settings = config_mod.get_settings
    config_mod.get_settings = lambda: fake  # type: ignore[assignment]
    try:
        import app.metrics.api as metrics_api

        importlib.reload(metrics_api)
        test_app = FastAPI()
        test_app.include_router(metrics_api.router, prefix="/acps-amp-v1")
        return test_app
    finally:
        config_mod.get_settings = original_get_settings
        sys.modules.pop("app.metrics.api", None)
        importlib.reload(importlib.import_module("app.metrics.api"))


@pytest.fixture
async def http_client_analytics_disabled() -> AsyncGenerator[AsyncClient]:
    app = _build_profile_app(analytics_enabled=False, governance_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def http_client_governance_disabled() -> AsyncGenerator[AsyncClient]:
    app = _build_profile_app(analytics_enabled=True, governance_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_rankings_not_registered_when_analytics_disabled(
    http_client_analytics_disabled: AsyncClient,
) -> None:
    """analytics_enabled=False → rankings/query 路由未注册（404）。"""
    start_iso, end_iso = _recent_time_range()
    resp = await http_client_analytics_disabled.post(
        f"{_PREFIX}/rankings/query",
        json={
            "metric": "uptimeSeconds",
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
            "aggregation": "latest",
            "direction": "desc",
            "topN": 5,
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_slo_and_capacity_not_registered_when_governance_disabled(
    http_client_governance_disabled: AsyncClient,
) -> None:
    """governance_enabled=False → slo/evaluate 与 capacity/saturation 未注册（404）。"""
    start_iso, end_iso = _recent_time_range()
    slo_resp = await http_client_governance_disabled.post(
        f"{_PREFIX}/slo/evaluate",
        json={
            "timeRange": {"startAt": start_iso, "endAt": end_iso},
            "rules": [{"sli": "success_rate", "window": "PT5M", "target": 95.0}],
        },
    )
    cap_resp = await http_client_governance_disabled.post(
        f"{_PREFIX}/capacity/saturation",
        json={"lookback": "PT10M"},
    )
    assert slo_resp.status_code == 404
    assert cap_resp.status_code == 404
