"""
D2 集成测试：验证 create_agent_app 能正确挂载 stream/notification 端点。

使用最小化 agent 目录（含 acs.json 能力位），不实际启动 lifespan，
只检查路由表。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.routing import APIRoute


def _make_agent_dir(tmp_path: Path, *, streaming: bool, notification: bool) -> str:
    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()
    acs = {
        "aic": "test-aic",
        "capabilities": {
            "streaming": streaming,
            "notification": notification,
        },
    }
    (agent_dir / "acs.json").write_text(json.dumps(acs), encoding="utf-8")
    (agent_dir / "config.toml").write_text(
        "[app]\nidentity_binding_enabled = false\n\n[concurrency]\nmax_concurrent_tasks = 10\n"
    )
    (agent_dir / "prompts.toml").write_text("")
    (agent_dir / "skills.toml").write_text("")
    return str(agent_dir)


def _app_routes(app: FastAPI) -> set[str]:
    """返回 FastAPI app 注册的所有路由路径集合。"""
    return {r.path for r in app.routes if isinstance(r, APIRoute)}


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_no_stream_no_notif_only_rpc(_, _dm, _me, _he, _ae, tmp_path):
    """当 capabilities 均为 false 时，只有 /rpc + /health 端点。"""
    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=False, notification=False)
    app = create_agent_app("test_agent", agent_dir)
    routes = _app_routes(app)
    assert "/rpc" in routes
    assert "/health" in routes
    assert "/stream" not in routes
    assert "/notification/set" not in routes


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_streaming_enabled_registers_stream_route(_, _dm, _me, _he, _ae, tmp_path):
    """当 streaming=true 时，/stream 端点被注册。"""
    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=True, notification=False)
    app = create_agent_app("test_agent", agent_dir)
    routes = _app_routes(app)
    assert "/stream" in routes
    assert "/notification/set" not in routes


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_notification_enabled_registers_notif_routes(_, _dm, _me, _he, _ae, tmp_path):
    """当 notification=true 时，4 个 /notification/* 端点被注册。"""
    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=False, notification=True)
    app = create_agent_app("test_agent", agent_dir)
    routes = _app_routes(app)
    assert "/notification/set" in routes
    assert "/notification/delete" in routes
    assert "/notification/get" in routes
    assert "/notification/start" in routes
    assert "/stream" not in routes


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_stream_endpoint_503_before_lifespan(_, _dm, _me, _he, _ae, tmp_path):
    """streaming=true 时，lifespan 启动前（StreamHandler 未就绪），POST /stream 返回 503。"""
    import asyncio

    import httpx

    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=True, notification=False)
    app = create_agent_app("test_agent", agent_dir)

    # 不启动 lifespan，直接发请求
    async def _send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/stream",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "stream",
                    "params": {
                        "message": {
                            "type": "task-command",
                            "id": "cmd-1",
                            "sentAt": "2026-01-01T00:00:00Z",
                            "senderRole": "leader",
                            "senderId": "leader-1",
                            "command": "start",
                            "taskId": "t-1",
                        }
                    },
                },
            )

    resp = asyncio.run(_send())
    assert resp.status_code == 503


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_both_enabled_registers_all_routes(_, _dm, _me, _he, _ae, tmp_path):
    """当 streaming=true 且 notification=true 时，所有端点均被注册。"""
    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=True, notification=True)
    app = create_agent_app("test_agent", agent_dir)
    routes = _app_routes(app)
    assert "/rpc" in routes
    assert "/stream" in routes
    assert "/notification/set" in routes
    assert "/notification/delete" in routes
    assert "/notification/get" in routes
    assert "/notification/start" in routes
    assert "/health" in routes


@patch("partners.generic_runner.AuditEmitter")
@patch("partners.generic_runner.HeartbeatEmitter")
@patch("partners.generic_runner.MetricsEmitter")
@patch("partners.generic_runner.DemoMetricsSampler")
@patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock())
def test_app_adds_peer_certificate_middleware(_, _dm, _me, _he, _ae, tmp_path):
    from acps_sdk.aip.aip_peer_cert import AipPeerCertificateMiddleware

    from partners.main import create_agent_app

    agent_dir = _make_agent_dir(tmp_path, streaming=False, notification=False)
    app = create_agent_app("test_agent", agent_dir)

    assert any(getattr(m, "cls", None) is AipPeerCertificateMiddleware for m in app.user_middleware)
