from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_identity import extract_common_name
from acps_sdk.aip.aip_notification_model import NOTIFICATION_TOKEN_HEADER
from acps_sdk.aip.aip_peer_cert import AipPeerCertH11Protocol, AipPeerCertificateMiddleware
from fastapi import FastAPI

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"
_workspace_root = _project_root.parent
_demo_partner_root = _workspace_root / "demo-partner"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from assistant.api.notification_routes import register_notification_routes
from assistant.core.notification_executor import NotificationExecutor

LEADER_CERT_FILE = _leader_dir / "atr" / "client.pem"
LEADER_TRUST_BUNDLE = _leader_dir / "atr" / "trust-bundle.pem"
LEADER_SERVER_CERT_FILE = _demo_partner_root / "partners" / "online" / "beijing_food" / "server.pem"
LEADER_SERVER_KEY_FILE = _demo_partner_root / "partners" / "online" / "beijing_food" / "server.key"
PARTNER_ONLINE_DIR = _demo_partner_root / "partners" / "online"
CHINA_TRANSPORT_DIR = PARTNER_ONLINE_DIR / "china_transport"
CHINA_TRANSPORT_CLIENT_CERT = CHINA_TRANSPORT_DIR / "client.pem"
CHINA_TRANSPORT_CLIENT_KEY = CHINA_TRANSPORT_DIR / "client.key"
CHINA_TRANSPORT_TRUST_BUNDLE = CHINA_TRANSPORT_DIR / "trust-bundle.pem"


def _cert_common_name(cert_file: Path) -> str:
    cert = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
    common_name = extract_common_name(cert)
    assert common_name is not None
    return common_name


def _load_partner_aic(agent_dir: Path) -> str:
    with (agent_dir / "acs.json").open(encoding="utf-8") as file_obj:
        return str(json.load(file_obj)["aic"])


LEADER_AIC = _cert_common_name(LEADER_CERT_FILE)
CHINA_TRANSPORT_AIC = _load_partner_aic(CHINA_TRANSPORT_DIR)


def _build_server_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=str(LEADER_SERVER_CERT_FILE),
        keyfile=str(LEADER_SERVER_KEY_FILE),
    )
    context.load_verify_locations(cafile=str(CHINA_TRANSPORT_TRUST_BUNDLE))
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _build_partner_client_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(CHINA_TRANSPORT_TRUST_BUNDLE),
    )
    context.load_cert_chain(
        certfile=str(CHINA_TRANSPORT_CLIENT_CERT),
        keyfile=str(CHINA_TRANSPORT_CLIENT_KEY),
    )
    context.check_hostname = False
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _reserve_free_port() -> tuple[int, socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    return int(sock.getsockname()[1]), sock


@contextmanager
def _run_tls_app(app: FastAPI, *, ssl_context: ssl.SSLContext) -> Iterator[str]:
    port, sock = _reserve_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        http=AipPeerCertH11Protocol,
        ws="none",
        log_level="warning",
        proxy_headers=False,
        lifespan="on",
    )
    config.load()
    config.ssl = ssl_context
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        sock.close()
        raise RuntimeError("Timed out waiting for notification callback test server to start")

    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def _build_task_result(*, task_id: str, sender_id: str, state: TaskState) -> TaskResult:
    now = datetime.now(UTC).isoformat()
    return TaskResult(
        id=f"result-{task_id}",
        sentAt=now,
        senderRole="partner",
        senderId=sender_id,
        taskId=task_id,
        sessionId=f"session-{task_id}",
        status=TaskStatus(state=state, stateChangedAt=now),
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_notification_executor_accepts_real_partner_callback_identity(
    e2e_runtime: Any,
) -> None:
    task_id = f"notif-e2e-{uuid.uuid4().hex[:12]}"
    session_id = f"session-{uuid.uuid4().hex[:12]}"

    executor = NotificationExecutor(
        partner_base_url=e2e_runtime.partner_urls["china_transport"],
        leader_id=LEADER_AIC,
        callback_base_url="https://127.0.0.1:1/aip/notifications",
        expected_partner_aic=CHINA_TRANSPORT_AIC,
        identity_binding_enabled=True,
        ssl_context=e2e_runtime.client_ssl_context,
    )

    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    register_notification_routes(app, executor)

    callback_server_ssl = _build_server_ssl_context()

    try:
        with _run_tls_app(app, ssl_context=callback_server_ssl) as callback_base_url:
            executor._callback_base_url = f"{callback_base_url}/aip/notifications"
            future = executor.register_task_future(task_id)

            initial_result = await executor.start_for_partner(
                partner_base_url=e2e_runtime.partner_urls["china_transport"],
                partner_aic=CHINA_TRANSPORT_AIC,
                session_id=session_id,
                user_input="推荐北京的美食餐厅",
                task_id=task_id,
            )

            callback_result = await asyncio.wait_for(future, timeout=60.0)
    finally:
        await executor.close()

    assert initial_result.senderId == CHINA_TRANSPORT_AIC
    assert callback_result.senderId == CHINA_TRANSPORT_AIC
    assert callback_result.status.state == TaskState.Rejected
    assert callback_result.taskId == task_id


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_notification_receiver_rejects_forged_sender_id_with_valid_partner_cert() -> None:
    task_id = f"notif-forged-{uuid.uuid4().hex[:12]}"
    token = "token-identity-check"

    executor = NotificationExecutor(
        partner_base_url="https://127.0.0.1:1",
        leader_id=LEADER_AIC,
        callback_base_url="https://127.0.0.1:1/aip/notifications",
        expected_partner_aic=CHINA_TRANSPORT_AIC,
        identity_binding_enabled=True,
        ssl_context=_build_partner_client_ssl_context(),
    )
    executor._task_to_token[task_id] = token

    app = FastAPI()
    app.add_middleware(AipPeerCertificateMiddleware)
    register_notification_routes(app, executor)

    callback_server_ssl = _build_server_ssl_context()
    partner_client_ssl = _build_partner_client_ssl_context()
    forged_result = _build_task_result(
        task_id=task_id,
        sender_id=LEADER_AIC,
        state=TaskState.Completed,
    )

    try:
        with _run_tls_app(app, ssl_context=callback_server_ssl) as callback_base_url:
            async with httpx.AsyncClient(verify=partner_client_ssl, timeout=10.0) as client:
                response = await client.post(
                    f"{callback_base_url}/aip/notifications/{task_id}",
                    content=forged_result.model_dump_json().encode(),
                    headers={
                        "Content-Type": "application/json",
                        NOTIFICATION_TOKEN_HEADER: token,
                    },
                )
    finally:
        await executor.close()

    assert response.status_code == 403
    payload = response.json()
    assert payload["detail"]["code"] == -32009
    assert "senderId" in payload["detail"]["data"]
