"""
D4 · AipExecutor 单元测试

验证 AipExecutor 按 Partner ACS 能力位路由：
- streaming=true  → StreamExecutor.run()
- 普通/其他        → TaskExecutor RPC 路径
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from assistant.models.task import PartnerSelection, PlanningResult

NOW = datetime.now(UTC).isoformat()


def _make_task_result(task_id: str, state: str = "completed"):
    """构造最小 TaskResult dict-like 对象（使用 MagicMock）。"""
    from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus

    state_enum = TaskState(state)
    return TaskResult(
        id="tr-1",
        sentAt=NOW,
        senderRole="partner",
        senderId="agent",
        taskId=task_id,
        status=TaskStatus(state=state_enum, stateChangedAt=NOW),
    )


def _make_planning_result(*partner_aics: str) -> PlanningResult:
    """构造含多个 Partner 的 PlanningResult。"""
    selections = [
        PartnerSelection(
            partner_aic=aic,
            partner_name=aic,
            skill_id="skill-1",
            reason="test",
            instruction_text=f"Do task for {aic}",
        )
        for aic in partner_aics
    ]
    return PlanningResult(
        scenario_id="s-1",
        selected_partners={"dim1": selections},
    )


def _streaming_acs(base_url: str = "http://partner:9041") -> dict:
    return {
        "capabilities": {"streaming": True, "notification": False},
        "endPoints": [{"transport": "SSE", "url": base_url}],
    }


def _rpc_acs(base_url: str = "http://partner-rpc:9041") -> dict:
    return {
        "capabilities": {"streaming": False, "notification": False},
        "endPoints": [{"transport": "JSONRPC", "url": base_url}],
    }


# ---------------------------------------------------------------------------
# test_stream_partner_uses_stream_executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_partner_uses_stream_executor():
    """streaming=true 的 partner 走 StreamExecutor.run()，不走 RPC start/poll。"""
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase

    aic = "stream-partner-aic"
    planning = _make_planning_result(aic)
    final_result = _make_task_result(f"active-task-id:{aic}", state="completed")

    acs_cache = {aic: _streaming_acs()}

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache=acs_cache,
    )

    # Mock StreamExecutor.run to return final_result without real HTTP
    with patch("assistant.core.aip_executor.StreamExecutor") as mock_stream_exec_cls:
        mock_instance = AsyncMock()
        mock_instance.run = AsyncMock(return_value=final_result)
        mock_instance.close = AsyncMock()
        mock_stream_exec_cls.return_value = mock_instance

        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active-task-id",
            planning_result=planning,
        )

    assert aic in result.partner_results
    per = result.partner_results[aic]
    assert per.state.value == "completed"
    assert aic in result.completed_partners
    # StreamExecutor.run must have been called
    assert mock_stream_exec_cls.call_args.kwargs["expected_partner_aic"] == aic
    assert mock_stream_exec_cls.call_args.kwargs["identity_binding_enabled"] is True
    mock_instance.run.assert_awaited_once()
    mock_instance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_partner_awaiting_input_maps_to_awaiting_input_phase():
    """streaming partner 返回 AwaitingInput 时，AipExecutor 应收敛到 awaiting_input。"""
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase

    aic = "stream-awaiting-input-aic"
    planning = _make_planning_result(aic)
    awaiting_input_result = _make_task_result(f"active-task-id:{aic}", state="awaiting-input")

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache={aic: _streaming_acs()},
    )

    with patch("assistant.core.aip_executor.StreamExecutor") as mock_stream_exec_cls:
        mock_instance = AsyncMock()
        mock_instance.run = AsyncMock(return_value=awaiting_input_result)
        mock_instance.close = AsyncMock()
        mock_stream_exec_cls.return_value = mock_instance

        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active-task-id",
            planning_result=planning,
        )

    assert result.phase == ExecutionPhase.AWAITING_INPUT
    assert aic in result.awaiting_input_partners
    assert aic not in result.failed_partners


# ---------------------------------------------------------------------------
# test_rpc_partner_delegated_to_base
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_partner_delegated_to_base():
    """streaming=false 的 partner 仍走父类 TaskExecutor 路径。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult

    aic = "rpc-partner-aic"
    planning = _make_planning_result(aic)
    acs_cache = {aic: _rpc_acs()}

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache=acs_cache,
    )

    rpc_per = PartnerExecutionResult(
        partner_aic=aic,
        dimension_id="dim1",
        state=TaskState.Completed,
    )
    rpc_exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
    rpc_exec_result.partner_results[aic] = rpc_per
    rpc_exec_result.completed_partners.append(aic)

    with patch(
        "assistant.core.aip_executor.TaskExecutor.execute",
        new=AsyncMock(return_value=rpc_exec_result),
    ):
        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active-task-id",
            planning_result=planning,
        )

    assert aic in result.partner_results
    assert aic in result.completed_partners


# ---------------------------------------------------------------------------
# test_mixed_partners_execute_concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_partners_execute_concurrently():
    """stream partner + rpc partner 同时执行，结果正确合并到同一 ExecutionResult。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult

    stream_aic = "stream-aic"
    rpc_aic = "rpc-aic"
    planning = _make_planning_result(stream_aic, rpc_aic)
    acs_cache = {
        stream_aic: _streaming_acs("http://partner-stream:9041"),
        rpc_aic: _rpc_acs("http://partner-rpc:9041"),
    }

    executor = AipExecutor(leader_aic="leader-1", acs_cache=acs_cache)
    stream_final = _make_task_result(f"active:{stream_aic}", state="completed")

    rpc_per = PartnerExecutionResult(
        partner_aic=rpc_aic,
        dimension_id="dim1",
        state=TaskState.Completed,
    )
    rpc_exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
    rpc_exec_result.partner_results[rpc_aic] = rpc_per
    rpc_exec_result.completed_partners.append(rpc_aic)

    with (
        patch("assistant.core.aip_executor.StreamExecutor") as mock_se_cls,
        patch(
            "assistant.core.aip_executor.TaskExecutor.execute",
            new=AsyncMock(return_value=rpc_exec_result),
        ),
    ):
        mock_se = AsyncMock()
        mock_se.run = AsyncMock(return_value=stream_final)
        mock_se.close = AsyncMock()
        mock_se_cls.return_value = mock_se

        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active",
            planning_result=planning,
        )

    assert stream_aic in result.partner_results
    assert rpc_aic in result.partner_results
    assert stream_aic in result.completed_partners
    assert rpc_aic in result.completed_partners


@pytest.mark.asyncio
async def test_mixed_partners_phase_respects_stream_awaiting_input():
    """混合执行时，stream partner 的 AwaitingInput 应覆盖总体 phase。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult

    stream_aic = "stream-awaiting-input-aic"
    rpc_aic = "rpc-completed-aic"
    planning = _make_planning_result(stream_aic, rpc_aic)
    acs_cache = {
        stream_aic: _streaming_acs("http://partner-stream:9041"),
        rpc_aic: _rpc_acs("http://partner-rpc:9041"),
    }

    executor = AipExecutor(leader_aic="leader-1", acs_cache=acs_cache)
    stream_result = _make_task_result(f"active:{stream_aic}", state="awaiting-input")

    rpc_per = PartnerExecutionResult(
        partner_aic=rpc_aic,
        dimension_id="dim1",
        state=TaskState.Completed,
    )
    rpc_exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
    rpc_exec_result.partner_results[rpc_aic] = rpc_per
    rpc_exec_result.completed_partners.append(rpc_aic)

    with (
        patch("assistant.core.aip_executor.StreamExecutor") as mock_se_cls,
        patch(
            "assistant.core.aip_executor.TaskExecutor.execute",
            new=AsyncMock(return_value=rpc_exec_result),
        ),
    ):
        mock_se = AsyncMock()
        mock_se.run = AsyncMock(return_value=stream_result)
        mock_se.close = AsyncMock()
        mock_se_cls.return_value = mock_se

        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active",
            planning_result=planning,
        )

    assert result.phase == ExecutionPhase.AWAITING_INPUT
    assert stream_aic in result.awaiting_input_partners
    assert rpc_aic in result.completed_partners


# ---------------------------------------------------------------------------
# test_stream_executor_exception_maps_to_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_partner_uses_notification_executor():
    """notification=true + notification_executor 配置时走 NotificationExecutor 路径。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor

    aic = "notif-partner-aic"
    planning = _make_planning_result(aic)
    acs_cache = {
        aic: {
            "capabilities": {"streaming": False, "notification": True},
            "endPoints": [{"transport": "JSONRPC", "url": "http://partner-notif:9041"}],
        }
    }

    final_result = _make_task_result(f"active-task-id:{aic}", state="completed")

    # Mock NotificationExecutor
    mock_notif_exec = MagicMock()
    mock_notif_exec.start_for_partner = AsyncMock(
        return_value=_make_task_result(f"active-task-id:{aic}", state="accepted")
    )
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(final_result)
    mock_notif_exec.register_task_future = MagicMock(return_value=fut)
    mock_notif_exec.cancel_task_future = MagicMock()

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache=acs_cache,
        callback_base_url="http://leader/aip/notifications",
        notification_executor=mock_notif_exec,
    )

    result = await executor.execute(
        session_id="sess-1",
        active_task_id="active-task-id",
        planning_result=planning,
    )

    assert aic in result.partner_results
    per = result.partner_results[aic]
    assert per.state.value == "completed"
    assert aic in result.completed_partners
    start_kwargs = mock_notif_exec.start_for_partner.await_args.kwargs
    assert start_kwargs["partner_aic"] == aic
    mock_notif_exec.start_for_partner.assert_awaited_once()
    mock_notif_exec.register_task_future.assert_called_once()


@pytest.mark.asyncio
async def test_notification_partner_falls_back_to_rpc_without_executor():
    """notification=true 但无 notification_executor 时仍回退 RPC。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor
    from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult

    aic = "notif-no-exec-aic"
    planning = _make_planning_result(aic)
    acs_cache = {
        aic: {
            "capabilities": {"streaming": False, "notification": True},
            "endPoints": [{"transport": "JSONRPC", "url": "http://partner:9041"}],
        }
    }

    rpc_per = PartnerExecutionResult(
        partner_aic=aic,
        dimension_id="dim1",
        state=TaskState.Completed,
    )
    rpc_exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
    rpc_exec_result.partner_results[aic] = rpc_per
    rpc_exec_result.completed_partners.append(aic)

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache=acs_cache,
        # no notification_executor
    )

    with patch(
        "assistant.core.aip_executor.TaskExecutor.execute",
        new=AsyncMock(return_value=rpc_exec_result),
    ):
        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active-task-id",
            planning_result=planning,
        )

    assert aic in result.completed_partners


@pytest.mark.asyncio
async def test_notification_executor_timeout_maps_to_failed():
    """NotificationExecutor future 超时（TimeoutError）→ partner 状态映射为 Failed。"""
    from assistant.core.aip_executor import AipExecutor

    aic = "notif-timeout-aic"
    planning = _make_planning_result(aic)
    acs_cache = {
        aic: {
            "capabilities": {"streaming": False, "notification": True},
            "endPoints": [{"transport": "JSONRPC", "url": "http://partner:9041"}],
        }
    }

    mock_notif_exec = MagicMock()
    mock_notif_exec.start_for_partner = AsyncMock(
        return_value=_make_task_result(f"active-task-id:{aic}", state="accepted")
    )
    # Future that never resolves (simulates timeout with very short wait_timeout)
    loop = asyncio.get_event_loop()
    pending_fut = loop.create_future()
    mock_notif_exec.register_task_future = MagicMock(return_value=pending_fut)
    mock_notif_exec.cancel_task_future = MagicMock()

    executor = AipExecutor(
        leader_aic="leader-1",
        acs_cache=acs_cache,
        callback_base_url="http://leader/aip/notifications",
        notification_executor=mock_notif_exec,
        notification_wait_timeout_s=0.01,  # 超短超时
    )

    result = await executor.execute(
        session_id="sess-1",
        active_task_id="active-task-id",
        planning_result=planning,
    )

    assert aic in result.partner_results
    assert result.partner_results[aic].state.value == "failed"
    assert aic in result.failed_partners
    pending_fut.cancel()


@pytest.mark.asyncio
async def test_stream_executor_exception_maps_to_failed():
    """StreamExecutor.run() 抛异常 → 该 partner 状态映射为 Failed。"""
    from acps_sdk.aip.aip_base_model import TaskState
    from assistant.core.aip_executor import AipExecutor

    aic = "bad-stream-partner"
    planning = _make_planning_result(aic)
    acs_cache = {aic: _streaming_acs()}

    executor = AipExecutor(leader_aic="leader-1", acs_cache=acs_cache)

    with patch("assistant.core.aip_executor.StreamExecutor") as mock_se_cls:
        mock_se = AsyncMock()
        mock_se.run = AsyncMock(side_effect=ConnectionError("SSE connection refused"))
        mock_se.close = AsyncMock()
        mock_se_cls.return_value = mock_se

        result = await executor.execute(
            session_id="sess-1",
            active_task_id="active",
            planning_result=planning,
        )

    assert aic in result.partner_results
    assert result.partner_results[aic].state == TaskState.Failed
    assert aic in result.failed_partners
