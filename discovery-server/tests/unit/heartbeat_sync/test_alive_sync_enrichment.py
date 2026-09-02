"""tests: attach_alive_status enrichment 单元测试。

验证：
- 无 reader 时不注入（holder 未注册）
- _alive_enrichable=False 时不注入（转发结果透传）
- 成功注入 aliveMap
- reader.load_alive_views 返回空时不赋 alive_map
"""

from __future__ import annotations

import pytest
from acps_sdk.adp.models import (
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
    DiscoveryResponse,
    DiscoveryResult,
    ErrorDetail,
)
from acps_sdk.amp.alive_sync.models import AliveView

import app.heartbeat_sync.holder as holder_module
from app.heartbeat_sync.enrichment import attach_alive_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(aic: str) -> DiscoveryAgentSkill:
    return DiscoveryAgentSkill(aic=aic, skill_id="skill-1", ranking=1)


def _make_group(aic: str) -> DiscoveryAgentGroup:
    return DiscoveryAgentGroup(
        group="default",
        agent_skills=[_make_skill(aic)],
    )


def _make_response(aics: list[str], enrichable: bool = True) -> DiscoveryResponse:
    groups = [_make_group(aic) for aic in aics]
    result = DiscoveryResult(acs_map={}, agents=groups, routes=[])
    result._alive_enrichable = enrichable
    return DiscoveryResponse.success(result=result)


def _view(aic: str, alive: bool) -> AliveView:
    return AliveView(aic=aic, alive=alive, last_seen_at=None)


# ---------------------------------------------------------------------------
# Fake AliveReader
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, views: dict[str, AliveView]) -> None:
        self._views = views

    async def load_alive_views(self, aics: list[str]) -> dict[str, AliveView]:
        return {aic: v for aic, v in self._views.items() if aic in aics}


class _RaisingReader:
    async def load_alive_views(self, aics: list[str]) -> dict[str, AliveView]:
        del aics
        raise RuntimeError("db unavailable")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_reader_skips_injection() -> None:
    """holder 未注册时，不注入 aliveMap。"""
    holder_module.clear_alive_reader()
    response = _make_response(["aic-1"])
    await attach_alive_status(response)
    assert response.result is not None
    assert response.result.alive_map is None


@pytest.mark.asyncio
async def test_not_enrichable_skips_injection() -> None:
    """转发结果（_alive_enrichable=False）不覆盖 aliveMap。"""
    reader = _FakeReader({"aic-1": _view("aic-1", True)})
    holder_module.set_alive_reader(reader)  # type: ignore[arg-type]
    try:
        response = _make_response(["aic-1"], enrichable=False)
        await attach_alive_status(response)
        assert response.result is not None
        assert response.result.alive_map is None
    finally:
        holder_module.clear_alive_reader()


@pytest.mark.asyncio
async def test_successful_injection() -> None:
    """本地产出结果应正确注入 aliveMap。"""
    view = _view("aic-1", True)
    reader = _FakeReader({"aic-1": view})
    holder_module.set_alive_reader(reader)  # type: ignore[arg-type]
    try:
        response = _make_response(["aic-1"])
        await attach_alive_status(response)
        assert response.result is not None
        assert response.result.alive_map is not None
        assert "aic-1" in response.result.alive_map
        assert response.result.alive_map["aic-1"]["alive"] is True
    finally:
        holder_module.clear_alive_reader()


@pytest.mark.asyncio
async def test_empty_views_skips_alive_map() -> None:
    """reader 返回空时不赋 alive_map（不产生 {} 空字典）。"""
    reader = _FakeReader({})
    holder_module.set_alive_reader(reader)  # type: ignore[arg-type]
    try:
        response = _make_response(["aic-not-found"])
        await attach_alive_status(response)
        assert response.result is not None
        assert response.result.alive_map is None
    finally:
        holder_module.clear_alive_reader()


@pytest.mark.asyncio
async def test_partial_injection() -> None:
    """只注入有 alive 记录的 AIC，没记录的 AIC 不出现在 aliveMap。"""
    view = _view("aic-2", False)
    reader = _FakeReader({"aic-2": view})
    holder_module.set_alive_reader(reader)  # type: ignore[arg-type]
    try:
        response = _make_response(["aic-1", "aic-2"])
        await attach_alive_status(response)
        assert response.result is not None
        assert response.result.alive_map is not None
        assert "aic-1" not in response.result.alive_map
        assert response.result.alive_map["aic-2"]["alive"] is False
    finally:
        holder_module.clear_alive_reader()


@pytest.mark.asyncio
async def test_none_result_is_noop() -> None:
    """response.result 为 None（错误响应）时不崩溃。"""
    holder_module.clear_alive_reader()
    error = ErrorDetail(code=404, message="no agents")
    response = DiscoveryResponse(error=error, result=None)
    await attach_alive_status(response)  # should not raise


@pytest.mark.asyncio
async def test_reader_exception_is_swallowed() -> None:
    """reader 异常时吞掉错误，不影响主查询结果。"""
    holder_module.set_alive_reader(_RaisingReader())  # type: ignore[arg-type]
    try:
        response = _make_response(["aic-1"])
        await attach_alive_status(response)
        assert response.result is not None
        assert response.result.alive_map is None
    finally:
        holder_module.clear_alive_reader()
