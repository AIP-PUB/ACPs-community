"""tests/unit/test_leader_lifecycle_system.py — leader lifespan System 埋点（ES5）。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture
def system_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_file = tmp_path / "amp_system.jsonl"
    import assistant.system_setup as system_setup

    monkeypatch.setattr(system_setup, "_SYSTEM_LOG_FILE", log_file)
    monkeypatch.setattr(system_setup.LEADER_SYSTEM_EMITTER, "_log_file", log_file)
    return log_file


async def _request_with_lifespan(app, method: str, path: str, **kwargs: object) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)


def test_leader_lifecycle_emits_startup_and_shutdown(system_log_file: Path) -> None:
    with (
        patch("leader.main.init_components", AsyncMock()),
        patch("leader.main.shutdown_components", AsyncMock()),
    ):
        from leader.main import app

        response = asyncio.run(_request_with_lifespan(app, "GET", "/"))
        assert response.status_code == 200

    records = [json.loads(line) for line in system_log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    modules = [r["body"]["module"] for r in records]
    assert "startup" in modules
    assert "shutdown" in modules
    startup = next(r for r in records if r["body"]["module"] == "startup")
    assert startup["body"]["category"] == "lifecycle"
    assert startup["body"]["aic"]
    assert "tags" not in startup["body"]


def test_leader_lifecycle_startup_emit_failure_does_not_block(system_log_file: Path) -> None:
    import assistant.system_setup as system_setup

    with (
        patch("leader.main.init_components", AsyncMock()),
        patch("leader.main.shutdown_components", AsyncMock()),
        patch.object(system_setup.LEADER_SYSTEM_EMITTER, "emit_sync", side_effect=RuntimeError("emit fail")),
    ):
        from leader.main import app

        response = asyncio.run(_request_with_lifespan(app, "GET", "/"))
        assert response.status_code == 200
