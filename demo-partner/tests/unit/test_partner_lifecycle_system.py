"""tests/unit/test_partner_lifecycle_system.py — partner lifespan System 埋点（ES4）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from acps_sdk.amp.system_emitter import SystemEmitter

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"


def _make_agent_dir(tmp_path: Path) -> str:
    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()
    (agent_dir / "acs.json").write_text(
        json.dumps({"aic": "test-aic-001", "capabilities": {"streaming": False, "notification": False}}),
        encoding="utf-8",
    )
    (agent_dir / "config.toml").write_text(
        "[app]\nidentity_binding_enabled = true\n\n[server]\nport = 9021\n[concurrency]\nmax_concurrent_tasks = 10\n",
        encoding="utf-8",
    )
    (agent_dir / "prompts.toml").write_text("", encoding="utf-8")
    (agent_dir / "skills.toml").write_text("", encoding="utf-8")
    return str(agent_dir)


def _group_rpc_body(leader_aic: str = LEADER_AIC) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "group-rpc-1",
        "method": "group",
        "params": {
            "protocol": "rabbitmq:4.0",
            "group": {
                "groupId": "group-1",
                "leader": {"aic": leader_aic},
                "partners": [],
            },
            "server": {
                "host": "localhost",
                "port": 5671,
                "vhost": "acps",
            },
            "amqp": {
                "exchange": "group-1",
                "exchangeType": "fanout",
                "routingKey": "",
            },
        },
    }


async def _request_with_lifespan(
    app: Any,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)


@pytest.fixture
def isolated_system_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """将 partner system 日志重定向到临时目录，避免污染仓库 logs/。"""
    log_file = tmp_path / "amp_system_test_agent.jsonl"
    original_init = SystemEmitter.__init__

    def _redirect_init(
        self: SystemEmitter,
        _log_file: Path,
        aic: str,
        *,
        resource: dict[str, str] | None = None,
    ) -> None:
        original_init(self, log_file, aic, resource=resource)

    monkeypatch.setattr(SystemEmitter, "__init__", _redirect_init)
    return log_file


@patch("partners.main.GroupHandler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_lifecycle_startup_and_shutdown_emit_system(
    _signer: MagicMock,
    mock_group_handler_cls: MagicMock,
    tmp_path: Path,
    isolated_system_log: Path,
) -> None:
    from partners.main import create_agent_app

    mock_handler = MagicMock()
    mock_handler.start = AsyncMock()
    mock_handler.shutdown = AsyncMock()
    mock_group_handler_cls.return_value = mock_handler

    agent_dir = _make_agent_dir(tmp_path)
    app = create_agent_app("test_agent", agent_dir)

    with (
        patch("partners.generic_runner.AsyncOpenAI"),
    ):
        response = asyncio.run(_request_with_lifespan(app, "GET", "/health"))
        assert response.status_code == 200

    assert isolated_system_log.exists()
    records = [
        json.loads(line) for line in isolated_system_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    modules = [r["body"]["module"] for r in records]
    assert "startup" in modules
    assert "shutdown" in modules
    startup = next(r for r in records if r["body"]["module"] == "startup")
    assert startup["body"]["category"] == "lifecycle"
    assert startup["body"]["agent_name"] == "test_agent"
    assert startup["body"]["port"] == 9021


@patch("partners.main.GroupHandler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_lifecycle_startup_emit_failure_does_not_block(
    _signer: MagicMock,
    mock_group_handler_cls: MagicMock,
    tmp_path: Path,
    isolated_system_log: Path,
) -> None:
    from partners.main import create_agent_app

    mock_handler = MagicMock()
    mock_handler.start = AsyncMock()
    mock_handler.shutdown = AsyncMock()
    mock_group_handler_cls.return_value = mock_handler

    agent_dir = _make_agent_dir(tmp_path)
    app = create_agent_app("test_agent", agent_dir)

    with (
        patch("partners.generic_runner.AsyncOpenAI"),
        patch(
            "partners.generic_runner.SystemEmitter.emit_sync",
            side_effect=RuntimeError("emit fail"),
        ),
    ):
        response = asyncio.run(_request_with_lifespan(app, "GET", "/health"))
        assert response.status_code == 200


@patch("partners.main.GroupHandler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_lifecycle_shutdown_emit_failure_still_runs_cleanup(
    _signer: MagicMock,
    mock_group_handler_cls: MagicMock,
    tmp_path: Path,
    isolated_system_log: Path,
) -> None:
    from partners.main import create_agent_app

    mock_handler = MagicMock()
    mock_handler.start = AsyncMock()
    mock_handler.shutdown = AsyncMock()
    mock_group_handler_cls.return_value = mock_handler

    def _emit_side_effect(body: dict[str, Any] | None, **_kwargs: object) -> str:
        if isinstance(body, dict) and body.get("module") == "shutdown":
            raise RuntimeError("shutdown emit fail")
        return "log-id-ok"

    agent_dir = _make_agent_dir(tmp_path)
    app = create_agent_app("test_agent", agent_dir)

    with (
        patch("partners.generic_runner.AsyncOpenAI"),
        patch(
            "partners.generic_runner.SystemEmitter.emit_sync",
            side_effect=_emit_side_effect,
        ),
    ):
        response = asyncio.run(_request_with_lifespan(app, "GET", "/health"))
        assert response.status_code == 200

    mock_handler.shutdown.assert_awaited_once()


@patch("partners.main.GroupHandler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_group_rpc_rejects_leader_mtls_identity_mismatch(
    _signer: MagicMock,
    mock_group_handler_cls: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from partners.main import create_agent_app

    mock_handler = MagicMock()
    mock_handler.start = AsyncMock()
    mock_handler.shutdown = AsyncMock()
    mock_handler.handle_group_rpc = AsyncMock()
    mock_group_handler_cls.return_value = mock_handler

    monkeypatch.setenv("AIP_IDENTITY_BINDING_ENABLED", "true")
    agent_dir = _make_agent_dir(tmp_path)
    app = create_agent_app("test_agent", agent_dir)

    with (
        patch("partners.generic_runner.AsyncOpenAI"),
        patch("partners.main.get_request_peer_aic", return_value=LEADER_AIC),
    ):
        response = asyncio.run(
            _request_with_lifespan(app, "POST", "/group/rpc", json=_group_rpc_body(leader_aic=OTHER_AIC))
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32009
    mock_handler.handle_group_rpc.assert_not_called()


@patch("partners.main.GroupHandler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_group_rpc_accepts_matching_leader_mtls_identity(
    _signer: MagicMock,
    mock_group_handler_cls: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from acps_sdk.aip.aip_group_model import RabbitMQResponse, RabbitMQResponseResult

    from partners.main import create_agent_app

    mock_handler = MagicMock()
    mock_handler.start = AsyncMock()
    mock_handler.shutdown = AsyncMock()
    mock_handler.handle_group_rpc = AsyncMock(
        return_value=RabbitMQResponse(
            id="group-rpc-1",
            result=RabbitMQResponseResult(
                connectionName="conn-1",
                vhost="acps",
                nodeName="rabbit@node",
                queueName="queue-1",
                processId="pid-1",
            ),
        )
    )
    mock_group_handler_cls.return_value = mock_handler

    monkeypatch.setenv("AIP_IDENTITY_BINDING_ENABLED", "true")
    agent_dir = _make_agent_dir(tmp_path)
    app = create_agent_app("test_agent", agent_dir)

    with (
        patch("partners.generic_runner.AsyncOpenAI"),
        patch("partners.main.get_request_peer_aic", return_value=LEADER_AIC),
    ):
        response = asyncio.run(
            _request_with_lifespan(app, "POST", "/group/rpc", json=_group_rpc_body(leader_aic=LEADER_AIC))
        )

    assert response.status_code == 200
    assert response.json()["result"]["queueName"] == "queue-1"
    mock_handler.handle_group_rpc.assert_awaited_once()
