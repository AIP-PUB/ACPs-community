"""
N1 测试：NotificationConfigStore / NotificationRegistry / NotificationDispatcher
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_notification_model import NotificationConfig
from acps_sdk.aip.aip_notification_server import (
    NotificationConfigStore,
    NotificationDispatcher,
    NotificationRegistry,
    NotificationSubscription,
)

NOW = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 可重复使用的 async 传输层 helper
# ---------------------------------------------------------------------------


class _FixedStatusTransport(httpx.AsyncBaseTransport):
    """每次请求都返回固定状态码，并记录调用次数（支持真正的 async 重试）。"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.call_count = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.requests.append(request)
        return httpx.Response(self.status_code, content=b"")


def _cfg(task_id: str = "t-1", url: str = "http://cb/notify", *, cfg_id: str | None = None) -> NotificationConfig:
    return NotificationConfig(id=cfg_id, url=url, token="tok", taskId=task_id)


def _task_result(task_id: str = "t-1", state: TaskState = TaskState.Completed) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


# ---------------------------------------------------------------------------
# NotificationConfigStore
# ---------------------------------------------------------------------------


def test_set_creates_id():
    store = NotificationConfigStore()
    cfg = store.set(_cfg(task_id="t1"))
    assert cfg.id is not None
    assert cfg.id.startswith("notification-")


def test_set_updates_existing():
    store = NotificationConfigStore()
    cfg = store.set(_cfg(task_id="t1"))
    existing_id = cfg.id
    updated = store.set(NotificationConfig(id=existing_id, url="http://new/cb", token="tok", taskId="t1"))
    assert updated.url == "http://new/cb"
    assert updated.id == existing_id


def test_delete_specific():
    store = NotificationConfigStore()
    cfg = store.set(_cfg(task_id="t1"))
    result = store.delete("t1", config_id=cfg.id)
    assert result is True
    assert store.get("t1") == []


def test_delete_all_for_task():
    store = NotificationConfigStore()
    store.set(_cfg(task_id="t1"))
    store.set(_cfg(task_id="t1"))
    result = store.delete("t1", config_id=None)
    assert result is True
    assert store.get("t1") == []


def test_delete_returns_false_when_not_found():
    store = NotificationConfigStore()
    result = store.delete("t-nonexistent", config_id="nc-xxx")
    assert result is False


def test_get_specific_and_all():
    store = NotificationConfigStore()
    c1 = store.set(_cfg(task_id="t1", url="http://a/cb"))
    c2 = store.set(_cfg(task_id="t1", url="http://b/cb"))

    by_id = store.get("t1", config_id=c1.id)
    assert len(by_id) == 1
    assert by_id[0].id == c1.id

    all_cfgs = store.get("t1")
    assert len(all_cfgs) == 2


# ---------------------------------------------------------------------------
# NotificationRegistry
# ---------------------------------------------------------------------------


def test_registry_matches_all_states():
    registry = NotificationRegistry()
    sub = NotificationSubscription(task_id="t1", config_id="nc-1", notify_on_states=None)
    registry.add(sub)
    assert registry.matches("t1", TaskState.Completed) == [sub]
    assert registry.matches("t1", TaskState.Working) == [sub]


def test_registry_matches_specific_states():
    registry = NotificationRegistry()
    sub = NotificationSubscription(
        task_id="t1", config_id="nc-1", notify_on_states=[TaskState.Completed]
    )
    registry.add(sub)
    assert registry.matches("t1", TaskState.Completed) == [sub]
    assert registry.matches("t1", TaskState.Working) == []


def test_registry_remove_task():
    registry = NotificationRegistry()
    sub = NotificationSubscription(task_id="t1", config_id="nc-1", notify_on_states=None)
    registry.add(sub)
    registry.remove_task("t1")
    assert registry.matches("t1", TaskState.Completed) == []


# ---------------------------------------------------------------------------
# NotificationDispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_post_success():
    """mock 回调端点返回 200，dispatch 调用一次，无重试。"""
    transport = _FixedStatusTransport(200)

    registry = NotificationRegistry()
    store = NotificationConfigStore()
    cfg = store.set(_cfg(task_id="t1", url="http://testserver/cb"))
    sub = NotificationSubscription(task_id="t1", config_id=cfg.id, notify_on_states=None)
    registry.add(sub)

    dispatcher = NotificationDispatcher(
        config_store=store,
        registry=registry,
        transport=transport,
        identity_binding_enabled=False,
    )
    await dispatcher.dispatch(_task_result(task_id="t1", state=TaskState.Completed))
    assert transport.call_count == 1
    await dispatcher.close()


@pytest.mark.asyncio
async def test_dispatcher_retry_on_5xx():
    """mock 返回 500，重试 max_retries 次后放弃，不抛异常。"""
    transport = _FixedStatusTransport(500)

    registry = NotificationRegistry()
    store = NotificationConfigStore()
    cfg = store.set(_cfg(task_id="t1"))
    sub = NotificationSubscription(task_id="t1", config_id=cfg.id, notify_on_states=None)
    registry.add(sub)

    max_retries = 2
    dispatcher = NotificationDispatcher(
        config_store=store,
        registry=registry,
        transport=transport,
        identity_binding_enabled=False,
        max_retries=max_retries,
        backoff_s=0.0,
    )
    await dispatcher.dispatch(_task_result(task_id="t1", state=TaskState.Completed))
    # 1 次首发 + max_retries 次重试
    assert transport.call_count == 1 + max_retries
    await dispatcher.close()


@pytest.mark.asyncio
async def test_dispatcher_no_matching_subscription():
    """无订阅时 dispatch 不发任何 HTTP 请求。"""
    transport = _FixedStatusTransport(200)

    registry = NotificationRegistry()
    store = NotificationConfigStore()

    dispatcher = NotificationDispatcher(
        config_store=store,
        registry=registry,
        transport=transport,
        identity_binding_enabled=False,
    )
    await dispatcher.dispatch(_task_result(task_id="t-no-sub", state=TaskState.Completed))
    assert transport.call_count == 0
    await dispatcher.close()
