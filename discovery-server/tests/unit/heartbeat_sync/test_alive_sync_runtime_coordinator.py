from __future__ import annotations

import pytest

from app.core import lifespan as lifespan_module
from app.core.config import settings

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_start_alive_sync_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = lifespan_module.RuntimeCoordinator()
    monkeypatch.setattr(settings, "ALIVE_SYNC_ENABLED", False)
    monkeypatch.setattr(settings, "ALIVE_SYNC_AUTO_START", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_PROVIDER_BASE_URL", "http://localhost:9009/acps-amp-v1/heartbeat")
    monkeypatch.setattr(settings, "APP_ENV", "development")

    async def fake_start_alive_sync(_settings: object) -> None:
        raise AssertionError("未启用时不应启动 alive-sync")

    monkeypatch.setattr(lifespan_module, "start_alive_sync", fake_start_alive_sync)

    await coordinator._start_alive_sync()

    assert coordinator.runtime_state.alive_sync.running is False
    assert coordinator.runtime_state.alive_sync.last_error is None


@pytest.mark.asyncio
async def test_start_alive_sync_skips_when_testing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = lifespan_module.RuntimeCoordinator()
    monkeypatch.setattr(settings, "ALIVE_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_AUTO_START", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_PROVIDER_BASE_URL", "http://localhost:9009/acps-amp-v1/heartbeat")
    monkeypatch.setattr(settings, "APP_ENV", "testing")

    async def fake_start_alive_sync(_settings: object) -> None:
        raise AssertionError("testing 环境不应自动启动 alive-sync")

    monkeypatch.setattr(lifespan_module, "start_alive_sync", fake_start_alive_sync)

    await coordinator._start_alive_sync()

    assert coordinator.runtime_state.alive_sync.running is False
    assert coordinator.runtime_state.alive_sync.last_error is None


@pytest.mark.asyncio
async def test_start_alive_sync_marks_running_when_guard_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = lifespan_module.RuntimeCoordinator()
    monkeypatch.setattr(settings, "ALIVE_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_AUTO_START", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_PROVIDER_BASE_URL", "http://localhost:9009/acps-amp-v1/heartbeat")
    monkeypatch.setattr(settings, "APP_ENV", "development")

    sentinel_store = object()
    called: dict[str, object] = {}

    async def fake_start_alive_sync(_settings: object) -> None:
        called["started"] = True

    def fake_set_alive_reader(reader: object) -> None:
        called["reader"] = reader

    monkeypatch.setattr(lifespan_module, "PostgresAliveSyncStore", lambda: sentinel_store)
    monkeypatch.setattr(lifespan_module, "set_alive_reader", fake_set_alive_reader)
    monkeypatch.setattr(lifespan_module, "start_alive_sync", fake_start_alive_sync)

    await coordinator._start_alive_sync()

    assert called.get("started") is True
    assert called.get("reader") is sentinel_store
    assert coordinator.runtime_state.alive_sync.running is True
    assert coordinator.runtime_state.alive_sync.last_error is None


@pytest.mark.asyncio
async def test_start_alive_sync_clears_reader_when_start_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = lifespan_module.RuntimeCoordinator()
    monkeypatch.setattr(settings, "ALIVE_SYNC_ENABLED", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_AUTO_START", True)
    monkeypatch.setattr(settings, "ALIVE_SYNC_PROVIDER_BASE_URL", "http://localhost:9009/acps-amp-v1/heartbeat")
    monkeypatch.setattr(settings, "APP_ENV", "development")

    called = {"clear": 0}

    async def fake_start_alive_sync(_settings: object) -> None:
        raise RuntimeError("bootstrap failed")

    def fake_clear_alive_reader() -> None:
        called["clear"] += 1

    monkeypatch.setattr(lifespan_module, "PostgresAliveSyncStore", lambda: object())
    monkeypatch.setattr(lifespan_module, "start_alive_sync", fake_start_alive_sync)
    monkeypatch.setattr(lifespan_module, "clear_alive_reader", fake_clear_alive_reader)

    await coordinator._start_alive_sync()

    assert called["clear"] == 1
    assert coordinator.runtime_state.alive_sync.running is False
    assert coordinator.runtime_state.alive_sync.last_error == "bootstrap failed"


@pytest.mark.asyncio
async def test_stop_alive_sync_always_clears_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = lifespan_module.RuntimeCoordinator()
    coordinator.runtime_state.alive_sync.running = True

    called = {"clear": 0}

    async def fake_stop_alive_sync() -> None:
        raise RuntimeError("stop failed")

    def fake_clear_alive_reader() -> None:
        called["clear"] += 1

    monkeypatch.setattr(lifespan_module, "stop_alive_sync", fake_stop_alive_sync)
    monkeypatch.setattr(lifespan_module, "clear_alive_reader", fake_clear_alive_reader)

    await coordinator._stop_alive_sync()

    assert called["clear"] == 1
    assert coordinator.runtime_state.alive_sync.running is False
