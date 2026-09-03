"""
D3 · NotificationExecutor 单元测试
"""

from __future__ import annotations

import sys
from pathlib import Path

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus
from acps_sdk.aip.aip_notification_model import NotificationConfig

NOW = datetime.now(UTC).isoformat()


def _make_task_result(task_id: str, state: TaskState) -> TaskResult:
    return TaskResult(
        id="r1",
        sentAt=NOW,
        senderRole="partner",
        senderId="p1",
        taskId=task_id,
        status=TaskStatus(state=state, stateChangedAt=NOW),
    )


def _patch_executor(monkeypatch=None):
    """返回可用于 patch 的上下文管理器元组。"""
    return (
        patch("assistant.core.notification_executor.AipNotificationClient"),
        patch("assistant.core.notification_executor.AipRpcClient"),
    )


@pytest.mark.asyncio
async def test_start_auto_generates_token():
    """start(token=None) 时自动生成 token 并存入 _task_to_token。"""
    from assistant.core.notification_executor import NotificationExecutor

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_nc = AsyncMock()
        mock_nc.set_notification = AsyncMock(
            return_value=NotificationConfig(id="cfg-1", url="http://cb", token="tok", taskId="t1")
        )
        mock_nc.start_notification = AsyncMock(return_value=True)
        mock_nc.close = AsyncMock()
        mock_nc_cls.return_value = mock_nc

        mock_rpc = AsyncMock()
        mock_rpc.start_task = AsyncMock(return_value=_make_task_result("t1", TaskState.Accepted))
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = NotificationExecutor(
            partner_base_url="http://fake",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            expected_partner_aic="p1",
        )
        await executor.start(session_id="sess-1", user_input="hi", task_id="t1")
        await executor.close()

    assert "t1" in executor._task_to_token
    token = executor._task_to_token["t1"]
    assert len(token) == 64  # secrets.token_hex(32) → 32 bytes → 64 hex chars


@pytest.mark.asyncio
async def test_start_registers_task_session():
    """start 后 _task_to_session[task_id] == session_id。"""
    from assistant.core.notification_executor import NotificationExecutor

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_nc = AsyncMock()
        mock_nc.set_notification = AsyncMock(
            return_value=NotificationConfig(id="cfg-2", url="http://cb", token="tok", taskId="t2")
        )
        mock_nc.start_notification = AsyncMock(return_value=True)
        mock_nc.close = AsyncMock()
        mock_nc_cls.return_value = mock_nc

        mock_rpc = AsyncMock()
        mock_rpc.start_task = AsyncMock(return_value=_make_task_result("t2", TaskState.Accepted))
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = NotificationExecutor(
            partner_base_url="http://fake",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            expected_partner_aic="p1",
        )
        await executor.start(session_id="my-sess", user_input="hi", task_id="t2")
        await executor.close()

    assert executor._task_to_session.get("t2") == "my-sess"


@pytest.mark.asyncio
async def test_validate_token_correct():
    """注册 token 后 _validate_token 返回 True（时序安全比较）。"""
    from assistant.core.notification_executor import NotificationExecutor

    executor = NotificationExecutor(
        partner_base_url="http://fake",
        leader_id="l1",
        callback_base_url="http://leader/cb",
        expected_partner_aic="p1",
    )
    task_id = "t3"
    executor._task_to_token[task_id] = "mytoken123"
    task_result = _make_task_result(task_id, TaskState.Completed)

    assert executor._validate_token("mytoken123", task_result) is True
    await executor.close()


@pytest.mark.asyncio
async def test_validate_token_wrong():
    """错误 token 返回 False。"""
    from assistant.core.notification_executor import NotificationExecutor

    executor = NotificationExecutor(
        partner_base_url="http://fake",
        leader_id="l1",
        callback_base_url="http://leader/cb",
        expected_partner_aic="p1",
    )
    task_id = "t4"
    executor._task_to_token[task_id] = "correct-token"
    task_result = _make_task_result(task_id, TaskState.Completed)

    assert executor._validate_token("wrong-token", task_result) is False
    await executor.close()


@pytest.mark.asyncio
async def test_on_callback_routes_to_session():
    """on_callback(task_result) 能根据 taskId 找回已注册的 session。"""
    from assistant.core.notification_executor import NotificationExecutor

    executor = NotificationExecutor(
        partner_base_url="http://fake",
        leader_id="l1",
        callback_base_url="http://leader/cb",
        expected_partner_aic="p1",
    )
    task_id = "t5"
    executor._task_to_token[task_id] = "tok"
    executor._task_to_session[task_id] = "expected-session"

    task_result = _make_task_result(task_id, TaskState.Completed)

    session_found: list[str] = []

    async def _patched(tr: TaskResult) -> None:
        session = executor._task_to_session.get(tr.taskId)
        if session:
            session_found.append(session)

    executor._dispatch_callback = _patched  # type: ignore[assignment]
    await executor.on_callback(task_result)
    import asyncio

    await asyncio.sleep(0)  # 让 fire-and-forget task 执行
    await executor.close()

    assert session_found == ["expected-session"]


@pytest.mark.asyncio
async def test_register_task_future_resolves_on_terminal():
    """register_task_future() 返回 Future，terminal 回调到来时自动 set_result。"""
    import asyncio

    from assistant.core.notification_executor import NotificationExecutor

    executor = NotificationExecutor(
        partner_base_url="http://fake",
        leader_id="l1",
        callback_base_url="http://leader/cb",
        expected_partner_aic="p1",
    )
    task_id = "t-future"
    future = executor.register_task_future(task_id)
    assert not future.done()

    tr = _make_task_result(task_id, TaskState.Completed)
    await executor.on_callback(tr)
    await asyncio.sleep(0)  # 让 fire-and-forget task 运行

    assert future.done()
    assert future.result().taskId == task_id
    await executor.close()


@pytest.mark.asyncio
async def test_register_task_future_not_resolved_on_non_terminal():
    """非终态回调不应解析 Future。"""
    import asyncio

    from assistant.core.notification_executor import NotificationExecutor

    executor = NotificationExecutor(
        partner_base_url="http://fake",
        leader_id="l1",
        callback_base_url="http://leader/cb",
        expected_partner_aic="p1",
    )
    task_id = "t-working"
    future = executor.register_task_future(task_id)

    tr = _make_task_result(task_id, TaskState.Working)
    await executor.on_callback(tr)
    await asyncio.sleep(0)

    assert not future.done(), "working 状态不应 resolve future"
    future.cancel()
    await executor.close()


@pytest.mark.asyncio
async def test_start_for_partner_uses_correct_urls():
    """start_for_partner 向正确的 partner URL 发起请求，不使用构造时的 placeholder URL。"""
    import asyncio
    from unittest.mock import call

    from assistant.core.notification_executor import NotificationExecutor

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        # 主 executor（placeholder URL）
        main_nc = AsyncMock()
        main_nc.close = AsyncMock()
        mock_nc_cls.return_value = main_nc

        main_rpc = AsyncMock()
        main_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = main_rpc

        executor = NotificationExecutor(
            partner_base_url="http://placeholder",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            identity_binding_enabled=False,
        )

        # 重置 call 计数后再 patch，让 start_for_partner 创建新实例
        mock_nc_cls.reset_mock()
        mock_rpc_cls.reset_mock()

        per_partner_nc = AsyncMock()
        per_partner_nc.set_notification = AsyncMock(
            return_value=NotificationConfig(id="cfg-pp", url="http://cb/t-pp", token="tok", taskId="t-pp")
        )
        per_partner_nc.start_notification = AsyncMock(return_value=True)
        per_partner_nc.close = AsyncMock()
        mock_nc_cls.return_value = per_partner_nc

        per_partner_rpc = AsyncMock()
        per_partner_rpc.start_task = AsyncMock(return_value=_make_task_result("t-pp", TaskState.Accepted))
        per_partner_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = per_partner_rpc

        result = await executor.start_for_partner(
            partner_base_url="http://real-partner:9041",
            partner_aic="p-real",
            session_id="sess",
            user_input="hi",
            task_id="t-pp",
        )

    # 验证 AipNotificationClient 使用 partner service base URL，避免重复拼接 /notification。
    mock_nc_cls.assert_called_once()
    call_kwargs = mock_nc_cls.call_args.kwargs
    assert call_kwargs["partner_url"] == "http://real-partner:9041"
    assert result.taskId == "t-pp"
    await executor.close()


@pytest.mark.asyncio
async def test_start_backfills_terminal_initial_result_to_registered_future():
    """若 start_task 已返回终态，executor 也应立即收敛等待中的 future。"""
    from assistant.core.notification_executor import NotificationExecutor

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_nc = AsyncMock()
        mock_nc.set_notification = AsyncMock(
            return_value=NotificationConfig(id="cfg-term", url="http://cb", token="tok", taskId="t-term")
        )
        mock_nc.start_notification = AsyncMock(return_value=True)
        mock_nc.close = AsyncMock()
        mock_nc_cls.return_value = mock_nc

        mock_rpc = AsyncMock()
        mock_rpc.start_task = AsyncMock(return_value=_make_task_result("t-term", TaskState.Rejected))
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = NotificationExecutor(
            partner_base_url="http://fake",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            expected_partner_aic="p1",
        )
        future = executor.register_task_future("t-term")

        result = await executor.start(session_id="sess-term", user_input="hi", task_id="t-term")

    assert result.status.state == TaskState.Rejected
    assert future.done()
    assert future.result().taskId == "t-term"
    assert future.result().status.state == TaskState.Rejected
    await executor.close()


@pytest.mark.asyncio
async def test_executor_close_closes_client():
    """close() 调用内部 AipNotificationClient.close() 和 AipRpcClient.close()。"""
    from assistant.core.notification_executor import NotificationExecutor

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_nc = AsyncMock()
        mock_nc.close = AsyncMock()
        mock_nc_cls.return_value = mock_nc

        mock_rpc = AsyncMock()
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = NotificationExecutor(
            partner_base_url="http://fake",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            expected_partner_aic="p1",
        )
        await executor.close()

    mock_nc.close.assert_awaited_once()
    mock_rpc.close.assert_awaited_once()


def test_notification_executor_passes_transport_to_rpc_client():
    """构造固定/临时 RPC client 时都应复用注入的 transport。"""
    from assistant.core.notification_executor import NotificationExecutor

    transport = object()

    with (
        patch("assistant.core.notification_executor.AipNotificationClient") as mock_nc_cls,
        patch("assistant.core.notification_executor.AipRpcClient") as mock_rpc_cls,
    ):
        mock_nc = AsyncMock()
        mock_nc.close = AsyncMock()
        mock_nc_cls.return_value = mock_nc

        mock_rpc = AsyncMock()
        mock_rpc.close = AsyncMock()
        mock_rpc_cls.return_value = mock_rpc

        executor = NotificationExecutor(
            partner_base_url="http://fake",
            leader_id="l1",
            callback_base_url="http://leader/cb",
            expected_partner_aic="p1",
            transport=transport,
        )

        executor._build_rpc_client("http://another-partner", "p2")

    first_call = mock_rpc_cls.call_args_list[0]
    second_call = mock_rpc_cls.call_args_list[1]
    assert first_call.kwargs["transport"] is transport
    assert second_call.kwargs["transport"] is transport
