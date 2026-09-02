"""E2E: AMP Alive Sync 端到端验证。

通过最小化 FastAPI 测试 App 覆盖所有 alive-sync 集成路径，
使用 holder monkeypatch，无需启动真实 Kafka / PostgreSQL / 外部 HTTP 服务。

场景：
  S1 — 首次自举后，discover 响应包含 aliveMap，leave/enter alive 反映正确
  S2 — ALIVE_SYNC_ENABLED=false 时，discover 响应无 aliveMap 键
  S3 — admin API：/status 与 /resync 端点正常工作
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.adp.models import (
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
    DiscoveryResponse,
    DiscoveryResult,
)
from acps_sdk.amp.alive_sync.models import AliveView
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.heartbeat_sync.holder as holder_module
from app.heartbeat_sync.api import router as alive_sync_router
from app.heartbeat_sync.enrichment import attach_alive_status

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Minimal test app (no lifespan background services)
# ---------------------------------------------------------------------------


def _build_minimal_app() -> FastAPI:
    """构建仅含所需路由的最小化测试 FastAPI App（跳过 lifespan）。"""
    test_app = FastAPI()
    test_app.include_router(alive_sync_router, prefix="/admin/alive-sync")

    @test_app.post("/acps-adp-v2/discover")
    async def _discover_stub() -> JSONResponse:
        """Stub discover：用固定 AIC 返回带 aliveMap 注入的响应。"""
        group = DiscoveryAgentGroup(
            group="default",
            agent_skills=[
                DiscoveryAgentSkill(aic="AIC-E2E-1", skill_id="s1", ranking=1),
                DiscoveryAgentSkill(aic="AIC-LEAVE-1", skill_id="s2", ranking=2),
            ],
        )
        result = DiscoveryResult(acs_map={}, agents=[group], routes=[])
        result._alive_enrichable = True
        response = DiscoveryResponse.success(result=result)
        await attach_alive_status(response)
        return JSONResponse(content=response.model_dump(by_alias=True, exclude_none=True), status_code=200)

    return test_app


_MINIMAL_APP = _build_minimal_app()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeAliveReader:
    """可在测试期间动态修改 alive 状态的假 reader。"""

    def __init__(self) -> None:
        self._views: dict[str, AliveView] = {}

    def set(self, aic: str, alive: bool) -> None:
        self._views[aic] = AliveView(aic=aic, alive=alive, last_seen_at=None)

    def remove(self, aic: str) -> None:
        self._views.pop(aic, None)

    async def load_alive_views(self, aics: list[str]) -> dict[str, AliveView]:
        return {aic: v for aic, v in self._views.items() if aic in aics}


# ---------------------------------------------------------------------------
# S1 — aliveMap 注入语义验证
# ---------------------------------------------------------------------------


class TestAliveMapInjection:
    """场景 1 变体：验证 aliveMap 在 discover 响应中的注入语义。"""

    def setup_method(self) -> None:
        holder_module.clear_alive_reader()

    def teardown_method(self) -> None:
        holder_module.clear_alive_reader()

    def test_no_alive_map_when_reader_absent(self) -> None:
        """holder 无 reader 时，discover 响应不含 aliveMap 键。"""
        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            body = client.post("/acps-adp-v2/discover").json()
        result = body.get("result") or {}
        assert "aliveMap" not in result, "reader 缺失时不应产生 aliveMap"

    def test_alive_map_present_after_bootstrap(self) -> None:
        """注册 reader 后，discover 响应中 aliveMap 包含 AIC。"""
        reader = _FakeAliveReader()
        reader.set("AIC-E2E-1", alive=True)
        holder_module.set_alive_reader(reader)  # type: ignore[arg-type]

        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            body = client.post("/acps-adp-v2/discover").json()

        result = body.get("result") or {}
        alive_map = result.get("aliveMap") or {}
        assert "AIC-E2E-1" in alive_map, "bootstrap 后 aliveMap 应包含 AIC"
        assert alive_map["AIC-E2E-1"]["alive"] is True

    def test_leave_alive_reflected(self) -> None:
        """leave_alive 后（alive=False），aliveMap 中值应为 false，键仍存在。"""
        reader = _FakeAliveReader()
        reader.set("AIC-E2E-1", alive=True)
        reader.set("AIC-LEAVE-1", alive=False)  # 模拟 leave_alive delta
        holder_module.set_alive_reader(reader)  # type: ignore[arg-type]

        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            body = client.post("/acps-adp-v2/discover").json()

        result = body.get("result") or {}
        alive_map = result.get("aliveMap") or {}
        assert "AIC-LEAVE-1" in alive_map, "leave_alive 后键仍应存在"
        assert alive_map["AIC-LEAVE-1"]["alive"] is False, "leave_alive 后值应为 false"
        assert "aliveLastSeenAt" in alive_map["AIC-LEAVE-1"]

    def test_enter_alive_reflected(self) -> None:
        """enter_alive 后（alive=True），aliveMap 中值应为 true。"""
        reader = _FakeAliveReader()
        reader.set("AIC-E2E-1", alive=True)
        reader.set("AIC-LEAVE-1", alive=True)  # 模拟 enter_alive delta
        holder_module.set_alive_reader(reader)  # type: ignore[arg-type]

        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            body = client.post("/acps-adp-v2/discover").json()

        result = body.get("result") or {}
        alive_map = result.get("aliveMap") or {}
        assert alive_map.get("AIC-LEAVE-1", {}).get("alive") is True


# ---------------------------------------------------------------------------
# S2 — ALIVE_SYNC_ENABLED=false 时无 aliveMap
# ---------------------------------------------------------------------------


class TestAliveMapDisabledBehavior:
    """场景 2：alive sync 禁用时，discover 响应应不含 aliveMap。"""

    def setup_method(self) -> None:
        holder_module.clear_alive_reader()

    def teardown_method(self) -> None:
        holder_module.clear_alive_reader()

    def test_no_alive_map_key_when_disabled(self) -> None:
        """holder 未注册（相当于 ALIVE_SYNC_ENABLED=false），aliveMap 完全缺失。"""
        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            resp = client.post("/acps-adp-v2/discover")
            body = resp.json()

        result = body.get("result") or {}
        assert "aliveMap" not in result, "ALIVE_SYNC 未启用时（holder 为空）不应序列化 aliveMap 键"


# ---------------------------------------------------------------------------
# S3 — Admin API
# ---------------------------------------------------------------------------


class TestAdminAliveSyncApi:
    """场景 3：admin 管理 API 端点基础功能验证。"""

    def test_status_returns_not_running_when_service_absent(self) -> None:
        """/admin/alive-sync/status 应在服务未启动时返回 running=false。"""
        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            resp = client.get("/admin/alive-sync/status")
        assert resp.status_code == 200
        assert resp.json().get("running") is False

    def test_resync_returns_503_when_service_absent(self) -> None:
        """/admin/alive-sync/resync 在服务未启动时应返回 503。"""
        with TestClient(_MINIMAL_APP, raise_server_exceptions=False) as client:
            resp = client.post("/admin/alive-sync/resync")
        assert resp.status_code == 503

    def test_resync_returns_200_when_service_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/admin/alive-sync/resync 在服务运行时应返回 200 并触发 request_resync。"""
        import app.heartbeat_sync.runtime as runtime_module

        fake_service = MagicMock()
        fake_service.request_resync = AsyncMock(return_value=None)
        monkeypatch.setattr(runtime_module, "_service", fake_service)

        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            resp = client.post("/admin/alive-sync/resync")

        assert resp.status_code == 200
        fake_service.request_resync.assert_called_once_with("admin_manual_trigger")

    def test_status_returns_running_when_service_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """/admin/alive-sync/status 在服务运行时应返回 running=true。"""
        import app.heartbeat_sync.runtime as runtime_module

        fake_service = MagicMock()
        fake_service.status = AsyncMock(return_value={"running": True})
        monkeypatch.setattr(runtime_module, "_service", fake_service)

        with TestClient(_MINIMAL_APP, raise_server_exceptions=True) as client:
            resp = client.get("/admin/alive-sync/status")

        assert resp.status_code == 200
        assert resp.json()["running"] is True
        fake_service.status.assert_awaited_once_with()
