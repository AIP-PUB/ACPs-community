"""
N3 测试：AipNotificationClient + NotificationReceiver
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_notification_client import AipNotificationClient, NotificationReceiver
from acps_sdk.aip.aip_notification_model import (
    NotificationConfig,
    NOTIFICATION_TOKEN_HEADER,
)

NOW = datetime.now(timezone.utc).isoformat()


def _task_result(task_id: str = "t-1", state: TaskState = TaskState.Completed) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


class _NotifServerTransport(httpx.AsyncBaseTransport):
    """模拟 Partner 通知服务，回显请求 ID。"""

    def __init__(self, response_body: dict) -> None:
        self._response_body = response_body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content)
        req_id = req_body.get("id")
        resp = {**self._response_body, "id": req_id}
        return httpx.Response(
            200,
            content=json.dumps(resp).encode(),
            headers={"Content-Type": "application/json"},
        )


# ---------------------------------------------------------------------------
# AipNotificationClient — set_notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_notification():
    """set_notification 发送 notification/set 并返回 NotificationConfig。"""
    config = NotificationConfig(id="nc-1", url="http://cb/n", token="tok", taskId="t-1")
    transport = _NotifServerTransport({"jsonrpc": "2.0", "result": json.loads(config.model_dump_json(exclude_none=True))})

    client = AipNotificationClient(
        partner_url="http://partner/",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    result = await client.set_notification(
        task_id="t-1", callback_url="http://cb/n", token="tok"
    )
    assert result.id == "nc-1"
    assert result.taskId == "t-1"
    await client.close()


@pytest.mark.asyncio
async def test_delete_notification():
    """delete_notification 发送 notification/delete，返回 True。"""
    transport = _NotifServerTransport({"jsonrpc": "2.0", "result": {"success": True}})
    client = AipNotificationClient(
        partner_url="http://partner/",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    success = await client.delete_notification(task_id="t-1", config_id="nc-1")
    assert success is True
    await client.close()


@pytest.mark.asyncio
async def test_get_notifications():
    """get_notifications 发送 notification/get，返回 NotificationConfig 列表。"""
    config = NotificationConfig(id="nc-1", url="http://cb/n", token="tok", taskId="t-1")
    transport = _NotifServerTransport(
        {"jsonrpc": "2.0", "result": [json.loads(config.model_dump_json(exclude_none=True))]}
    )
    client = AipNotificationClient(
        partner_url="http://partner/",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    configs = await client.get_notifications(task_id="t-1")
    assert len(configs) == 1
    assert configs[0].id == "nc-1"
    await client.close()


@pytest.mark.asyncio
async def test_start_notification():
    """start_notification 发送 notification/start，返回成功。"""
    transport = _NotifServerTransport({"jsonrpc": "2.0", "result": True})
    client = AipNotificationClient(
        partner_url="http://partner/",
        leader_id="l",
        transport=transport,
        identity_binding_enabled=False,
    )
    ok = await client.start_notification(
        task_id="t-1",
        config_id="nc-1",
        session_id="sess-1",
    )
    assert ok is True
    await client.close()


# ---------------------------------------------------------------------------
# NotificationReceiver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receiver_valid_token_calls_handler():
    """有效 token → handler 被调用（使用 AsyncClient ASGI transport，共享 event loop）。"""
    token = "my-secret-token"
    received = []

    async def handler(task_result: TaskResult) -> None:
        received.append(task_result)

    receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=False)
    app = FastAPI()
    receiver.mount(app, "/callback")

    tr = _task_result()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/callback",
            content=tr.model_dump_json(exclude_none=True).encode(),
            headers={
                "Content-Type": "application/json",
                NOTIFICATION_TOKEN_HEADER: token,
            },
        )
    assert resp.status_code == 200
    # 给 fire-and-forget task 一次执行机会
    await asyncio.sleep(0)
    assert len(received) == 1
    assert received[0].taskId == "t-1"


@pytest.mark.asyncio
async def test_receiver_legacy_token_header_calls_handler():
    """旧版大小写写法仍可被接收（HTTP header 名大小写不敏感）。"""
    token = "legacy-token"
    received = []

    async def handler(task_result: TaskResult) -> None:
        received.append(task_result)

    receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=False)
    app = FastAPI()
    receiver.mount(app, "/callback")

    tr = _task_result()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/callback",
            content=tr.model_dump_json(exclude_none=True).encode(),
            headers={
                "Content-Type": "application/json",
                "X-ACPS-AIP-Notification-Token": token,
            },
        )
    assert resp.status_code == 200
    await asyncio.sleep(0)
    assert len(received) == 1
    assert received[0].taskId == "t-1"


@pytest.mark.asyncio
async def test_receiver_invalid_token_401():
    """无效 token → 401，handler 不被调用。"""
    token = "correct-token"
    received = []

    async def handler(task_result: TaskResult) -> None:
        received.append(task_result)

    receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=False)
    app = FastAPI()
    receiver.mount(app, "/callback")

    tr = _task_result()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/callback",
            content=tr.model_dump_json(exclude_none=True).encode(),
            headers={
                "Content-Type": "application/json",
                NOTIFICATION_TOKEN_HEADER: "wrong-token",
            },
        )
    assert resp.status_code == 401
    assert received == []


@pytest.mark.asyncio
async def test_receiver_missing_token_401():
    """缺少 token 头 → 401。"""
    token = "secret"

    async def handler(task_result: TaskResult) -> None:
        pass

    receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=False)
    app = FastAPI()
    receiver.mount(app, "/callback")

    tr = _task_result()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/callback",
            content=tr.model_dump_json(exclude_none=True).encode(),
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_receiver_malformed_body_returns_400():
    """请求体不是合法 TaskResult JSON → 400。"""
    token = "tok-malform"

    async def handler(task_result: TaskResult) -> None:
        pass

    receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=False)
    app = FastAPI()
    receiver.mount(app, "/callback")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post(
            "/callback",
            content=b'{"not": "a task result"}',
            headers={
                "Content-Type": "application/json",
                NOTIFICATION_TOKEN_HEADER: token,
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_start_roundtrip_via_asgi():
    """AipNotificationClient set + start 通过 ASGI transport 对接 NotificationService 全流程。"""
    from acps_sdk.aip.aip_notification_server import NotificationService, add_aip_notification_router
    from acps_sdk.aip.aip_notification_client import AipNotificationClient

    app = FastAPI()
    service = NotificationService(identity_binding_enabled=False)
    add_aip_notification_router(app, service)

    # 使用 ASGI transport 替换真实 HTTP
    transport = httpx.ASGITransport(app=app)
    client = AipNotificationClient(
        partner_url="http://test",
        leader_id="leader-1",
        transport=transport,
        identity_binding_enabled=False,
    )

    task_id = "t-roundtrip"

    # 1. set
    cfg = await client.set_notification(
        task_id=task_id,
        callback_url="http://leader/callback",
        token="mytoken",
    )
    assert cfg.id is not None

    # 2. start subscription
    ok = await client.start_notification(
        task_id=task_id,
        config_id=cfg.id,
        session_id="sess-rt",
    )
    assert ok is True

    # 验证订阅已注册
    from acps_sdk.aip.aip_base_model import TaskState
    subs = service.registry.matches(task_id, TaskState.Completed)
    assert len(subs) >= 1

    await client.close()
