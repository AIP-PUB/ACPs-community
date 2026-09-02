"""
N2 测试：NotificationService 聚合 + 四个 JSON-RPC 端点
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_notification_model import (
    NotificationConfig,
    NOTIFICATION_TOKEN_HEADER,
)
from acps_sdk.aip.aip_notification_server import (
    NotificationConfigStore,
    NotificationRegistry,
    NotificationService,
    add_aip_notification_router,
)

NOW = datetime.now(timezone.utc).isoformat()


class _FixedStatusTransport(httpx.AsyncBaseTransport):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.call_count = 0
        self.last_request_headers: dict[str, str] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request_headers = {key.lower(): value for key, value in request.headers.items()}
        return httpx.Response(self.status_code, content=b"")


def _make_app(transport=None) -> tuple[FastAPI, NotificationService]:
    """构建带 notification 路由的测试应用，返回 (app, service)。"""
    app = FastAPI()
    service = NotificationService(transport=transport, identity_binding_enabled=False)
    add_aip_notification_router(app, service)
    return app, service


def _post(app: FastAPI, path: str, **kwargs: object) -> Response:
    async def _send() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, **kwargs)

    return asyncio.run(_send())


async def _post_async(app: FastAPI, path: str, **kwargs: object) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, **kwargs)


def _set_body(task_id: str = "t-1", url: str = "http://cb/notify") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "notification/set",
        "id": "1",
        "params": {"url": url, "token": "tok123", "taskId": task_id},
    }


def _delete_body(task_id: str = "t-1", config_id: str | None = None) -> dict:
    body: dict = {
        "jsonrpc": "2.0",
        "method": "notification/delete",
        "id": "2",
        "params": {"taskId": task_id},
    }
    if config_id:
        body["params"]["configId"] = config_id
    return body


def _get_body(task_id: str = "t-1", config_id: str | None = None) -> dict:
    body: dict = {
        "jsonrpc": "2.0",
        "method": "notification/get",
        "id": "3",
        "params": {"taskId": task_id},
    }
    if config_id:
        body["params"]["configId"] = config_id
    return body


# ---------------------------------------------------------------------------
# notification/set
# ---------------------------------------------------------------------------


def test_set_creates_config():
    """POST /notification/set 返回含 id 的 NotificationConfig。"""
    app, _ = _make_app()
    resp = _post(app, "/notification/set", json=_set_body("t-set"))
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert data["result"]["id"] is not None
    assert data["result"]["taskId"] == "t-set"


def test_set_invalid_body_400():
    """请求体缺少必填字段 → 400。"""
    app, _ = _make_app()
    resp = _post(app, "/notification/set", json={"bad": "data"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# notification/delete
# ---------------------------------------------------------------------------


def test_delete_existing():
    """先 set，再 delete → 成功，result.success=True。"""
    app, service = _make_app()

    set_resp = _post(app, "/notification/set", json=_set_body("t-del"))
    config_id = set_resp.json()["result"]["id"]

    del_resp = _post(app, "/notification/delete", json=_delete_body("t-del", config_id))
    assert del_resp.status_code == 200
    assert del_resp.json()["result"]["success"] is True


def test_delete_nonexistent_404():
    """删除不存在的 config → 404。"""
    app, _ = _make_app()
    resp = _post(app, "/notification/delete", json=_delete_body("t-noexist", "nc-bad"))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# notification/get
# ---------------------------------------------------------------------------


def test_get_all():
    """先 set 2 条，再 get（不指定 configId）→ 返回列表。"""
    app, _ = _make_app()
    _post(app, "/notification/set", json=_set_body("t-get", "http://a/cb"))
    _post(app, "/notification/set", json=_set_body("t-get", "http://b/cb"))
    get_resp = _post(app, "/notification/get", json=_get_body("t-get"))
    assert get_resp.status_code == 200
    result = get_resp.json()["result"]
    assert isinstance(result, list)
    assert len(result) == 2


def test_get_specific():
    """指定 configId → 返回单条列表。"""
    app, _ = _make_app()
    set_resp = _post(app, "/notification/set", json=_set_body("t-getone"))
    config_id = set_resp.json()["result"]["id"]

    get_resp = _post(app, "/notification/get", json=_get_body("t-getone", config_id))
    assert get_resp.status_code == 200
    result = get_resp.json()["result"]
    assert len(result) == 1
    assert result[0]["id"] == config_id


# ---------------------------------------------------------------------------
# notification/start
# ---------------------------------------------------------------------------


def test_start_registers_subscription():
    """notification/start 注册订阅，service.registry 中有对应记录。"""
    app, service = _make_app()

    set_resp = _post(app, "/notification/set", json=_set_body("t-start"))
    config_id = set_resp.json()["result"]["id"]

    start_body = {
        "jsonrpc": "2.0",
        "method": "notification/start",
        "id": "4",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "start",
                "taskId": "t-start",
                "commandParams": {"notificationConfigId": config_id},
            }
        },
    }
    resp = _post(app, "/notification/start", json=start_body)
    assert resp.status_code == 200
    # 验证订阅已注册
    subs = service.registry.matches("t-start", TaskState.Completed)
    assert len(subs) >= 1


# ---------------------------------------------------------------------------
# NotificationService.dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_dispatch_calls_callback():
    """service.dispatch() 时，匹配订阅的回调被调用。"""
    transport = _FixedStatusTransport(200)
    app, service = _make_app(transport=transport)

    # Set + Start
    set_resp = await _post_async(app, "/notification/set", json=_set_body("t-dispatch", "http://cb/notify"))
    config_id = set_resp.json()["result"]["id"]

    start_body = {
        "jsonrpc": "2.0",
        "method": "notification/start",
        "id": "5",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "start",
                "taskId": "t-dispatch",
                "commandParams": {"notificationConfigId": config_id},
            }
        },
    }
    await _post_async(app, "/notification/start", json=start_body)

    task_result = TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId="t-dispatch",
        status=TaskStatus(state=TaskState.Completed, stateChangedAt=NOW),
    )
    await service.dispatch(task_result)
    assert transport.call_count == 1
    assert transport.last_request_headers is not None
    assert transport.last_request_headers.get(NOTIFICATION_TOKEN_HEADER.lower()) == "tok123"


# ---------------------------------------------------------------------------
# notifyOnStates 状态过滤
# ---------------------------------------------------------------------------


def _start_body_with_states(task_id: str, config_id: str, states: list[str]) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "notification/start",
        "id": "6",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-filter",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "start",
                "taskId": task_id,
                "commandParams": {
                    "notificationConfigId": config_id,
                    "notifyOnStates": states,
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_notify_working_state_filtered_when_only_completed_subscribed():
    """notifyOnStates=["completed"] 时，working 状态不触发回调，completed 状态触发。"""
    transport = _FixedStatusTransport(200)
    app, service = _make_app(transport=transport)

    task_id = "t-filter"
    set_resp = await _post_async(app, "/notification/set", json=_set_body(task_id, "http://cb/filter"))
    config_id = set_resp.json()["result"]["id"]

    # 订阅：只关注 completed 状态
    start_resp = await _post_async(
        app,
        "/notification/start",
        json=_start_body_with_states(task_id, config_id, ["completed"]),
    )
    assert start_resp.status_code == 200

    def _tr(state: TaskState) -> TaskResult:
        return TaskResult(
            id="tr-f",
            sentAt=NOW,
            senderRole="partner",
            senderId="agent",
            taskId=task_id,
            status=TaskStatus(state=state, stateChangedAt=NOW),
        )

    # 推送 working → 不触发
    await service.dispatch(_tr(TaskState.Working))
    assert transport.call_count == 0, "working 不应触发 callback"

    # 推送 completed → 触发
    await service.dispatch(_tr(TaskState.Completed))
    assert transport.call_count == 1, "completed 应触发一次 callback"


@pytest.mark.asyncio
async def test_notify_all_states_when_no_filter():
    """notifyOnStates 不传时，所有状态均触发回调。"""
    transport = _FixedStatusTransport(200)
    app, service = _make_app(transport=transport)

    task_id = "t-nofilter"
    set_resp = await _post_async(app, "/notification/set", json=_set_body(task_id, "http://cb/all"))
    config_id = set_resp.json()["result"]["id"]

    # 订阅：不指定 notifyOnStates
    start_body = {
        "jsonrpc": "2.0",
        "method": "notification/start",
        "id": "7",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-all",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "command": "start",
                "taskId": task_id,
                "commandParams": {"notificationConfigId": config_id},
            }
        },
    }
    await _post_async(app, "/notification/start", json=start_body)

    def _tr(state: TaskState) -> TaskResult:
        return TaskResult(
            id="tr-a",
            sentAt=NOW,
            senderRole="partner",
            senderId="agent",
            taskId=task_id,
            status=TaskStatus(state=state, stateChangedAt=NOW),
        )

    await service.dispatch(_tr(TaskState.Working))
    await service.dispatch(_tr(TaskState.Completed))
    assert transport.call_count == 2, "两次 dispatch 均应触发 callback"
