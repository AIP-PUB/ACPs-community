"""
D1 测试：StreamHandler / NotificationHandler
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
)

NOW = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task_command(task_id: str = "t-1") -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=NOW,
        senderRole="leader",
        senderId="leader-1",
        sessionId="sess-1",
        command=TaskCommandType.Start,
        taskId=task_id,
    )


def _make_task_result(task_id: str = "t-1", state: TaskState = TaskState.Working) -> TaskResult:
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


@pytest.fixture()
def runner(tmp_path):
    """最小化 GenericRunner fixture（复用 D0 的 mock 方式）。"""
    import json as _json

    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()
    (agent_dir / "acs.json").write_text(_json.dumps({"aic": "test-aic", "capabilities": {}}), encoding="utf-8")
    (agent_dir / "config.toml").write_text("[concurrency]\nmax_concurrent_tasks = 10\n")
    (agent_dir / "prompts.toml").write_text("")
    (agent_dir / "skills.toml").write_text("")

    with (
        patch("partners.generic_runner.AuditEmitter"),
        patch("partners.generic_runner.HeartbeatEmitter"),
        patch("partners.generic_runner.MetricsEmitter"),
        patch("partners.generic_runner.DemoMetricsSampler"),
        patch("partners.generic_runner.load_signer_from_keys_json", return_value=MagicMock()),
    ):
        from partners.generic_runner import GenericRunner

        return GenericRunner("test_agent", str(agent_dir))


# ---------------------------------------------------------------------------
# StreamHandler — 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_handler_registers_listener(runner):
    """StreamHandler.__init__ 注册了状态变更监听者。"""
    from partners.stream_handler import StreamHandler

    handler = StreamHandler(runner=runner)
    # 验证监听者已注册
    assert handler._on_runner_state_change in runner._state_change_listeners


@pytest.mark.asyncio
async def test_stream_handler_on_state_change_publishes_to_hub(runner):
    """GenericRunner 状态变化后，StreamHandler 将事件发布到 StreamHub。"""
    from partners.generic_runner import TaskContext
    from partners.stream_handler import StreamHandler

    handler = StreamHandler(runner=runner)
    task_id = "t-stream-h"
    handler.hub.get_or_create_channel(task_id)

    # 预置 task
    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    # 触发状态变化
    runner._update_task_status(task_id, TaskState.Working)
    await asyncio.sleep(0)

    # Hub 中应该已有事件
    ch = handler.hub.get_channel(task_id)
    assert ch is not None
    assert ch.latest_seq >= 1


@pytest.mark.asyncio
async def test_stream_handler_terminal_closes_channel(runner):
    """终态事件发布后通道被关闭。"""
    from partners.generic_runner import TaskContext
    from partners.stream_handler import StreamHandler

    handler = StreamHandler(runner=runner)
    task_id = "t-terminal"
    handler.hub.get_or_create_channel(task_id)
    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    runner._update_task_status(task_id, TaskState.Completed)
    await asyncio.sleep(0.05)  # 让 fire-and-forget task 执行

    ch = handler.hub.get_channel(task_id)
    if ch is not None:
        assert ch.is_closed


# ---------------------------------------------------------------------------
# NotificationHandler — 测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_handler_registers_listener(runner):
    """NotificationHandler.__init__ 注册了状态变更监听者。"""
    from partners.notification_handler import NotificationHandler

    handler = NotificationHandler(runner=runner, identity_binding_enabled=False)
    assert handler._on_runner_state_change in runner._state_change_listeners


@pytest.mark.asyncio
async def test_notification_handler_dispatches_on_state_change(runner):
    """状态变化后，NotificationHandler 调用 service.dispatch。"""
    from partners.generic_runner import TaskContext
    from partners.notification_handler import NotificationHandler

    class _CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.call_count = 0

        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            self.call_count += 1
            return httpx.Response(200, content=b"")

    transport = _CountingTransport()
    handler = NotificationHandler(
        runner=runner,
        transport=transport,
        identity_binding_enabled=False,
    )

    # 注册通知配置和订阅
    from acps_sdk.aip.aip_notification_model import NotificationConfig
    from acps_sdk.aip.aip_notification_server import NotificationSubscription

    task_id = "t-notif-h"
    cfg = handler.service.store.set(NotificationConfig(url="http://cb/n", token="tok", taskId=task_id))
    assert cfg.id is not None
    handler.service.registry.add(NotificationSubscription(task_id=task_id, config_id=cfg.id, notify_on_states=None))

    # 预置 task
    runner.tasks[task_id] = TaskContext(task=_make_task_result(task_id=task_id, state=TaskState.Accepted))

    # 触发终态（Completed）
    runner._update_task_status(task_id, TaskState.Completed)
    await asyncio.sleep(0.05)

    # 回调应被调用
    assert transport.call_count >= 1
