"""Orchestrator 终止流程回归测试。"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_CURRENT_DIR = Path(__file__).parent
_TESTS_ROOT = _CURRENT_DIR.parent
_PROJECT_ROOT = _TESTS_ROOT.parent
_LEADER_DIR = _PROJECT_ROOT / "leader"

if str(_LEADER_DIR) not in sys.path:
    sys.path.insert(0, str(_LEADER_DIR))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from acps_sdk.aip.aip_base_model import TaskState
from assistant.core.orchestrator import Orchestrator
from assistant.models import (
    ExecutionMode,
    IntentDecision,
    IntentType,
    ScenarioRuntime,
    Session,
    TaskInstruction,
    UserResult,
    UserResultType,
)
from assistant.models.base import ActiveTaskStatus, now_iso
from assistant.models.task import ActiveTask, PartnerSelection, PartnerTask, PlanningResult


@pytest.mark.asyncio
async def test_terminate_current_task_ignores_missing_partner_endpoint_field():
    """PartnerTask 无 endpoint 字段时不应抛异常。"""
    now = now_iso()
    orchestrator = Orchestrator(
        session_manager=MagicMock(),
        scenario_loader=MagicMock(),
        intent_analyzer=MagicMock(),
        planner=MagicMock(),
        history_compressor=MagicMock(),
    )
    executor = MagicMock()
    executor.cancel_partner = AsyncMock(return_value=(None, None))
    executor.complete_partner = AsyncMock(return_value=(None, None))
    orchestrator._executor = executor
    orchestrator._planner = MagicMock()
    orchestrator._planner._acs_cache = {
        "partner-aic": {"endPoints": [{"url": "https://partner.example/rpc", "transport": "HTTP"}]}
    }

    active_task = ActiveTask(
        active_task_id="task-001",
        created_at=now,
        external_status=ActiveTaskStatus.RUNNING,
        partner_tasks={
            "partner-aic": PartnerTask(
                partnerAic="partner-aic",
                aipTaskId="aip-task-001",
                state=TaskState.Accepted,
            )
        },
    )
    session = Session(
        session_id="test-session-termination",
        mode=ExecutionMode.DIRECT_RPC,
        created_at=now,
        updated_at=now,
        touched_at=now,
        ttl_seconds=3600,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        base_scenario=ScenarioRuntime(
            id="base",
            kind="base",
            version="1.0.0",
            loaded_at=now,
        ),
        user_result=UserResult(
            type=UserResultType.PENDING,
            data_items=[],
            updated_at=now,
        ),
    )

    await orchestrator._terminate_current_task(session, active_task)

    executor.cancel_partner.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_task_new_cancels_background_task_before_terminating_current_task():
    """切换到新任务前，必须先停止旧 active_task 的后台执行。"""
    now = now_iso()
    order: list[str] = []

    session_manager = MagicMock()
    scenario_loader = MagicMock()
    scenario_loader.get_expert_scenario.return_value = ScenarioRuntime(
        id="tour",
        kind="expert",
        version="1.0.0",
        loaded_at=now,
    )
    planner = MagicMock()
    planner._acs_cache = {}
    planner.plan = AsyncMock(
        return_value=PlanningResult(
            created_at=now,
            scenario_id="tour",
            user_query="重新规划这次行程",
            selected_partners={
                "hotel": [
                    PartnerSelection(
                        partner_aic="partner-hotel",
                        skill_id="search_hotel",
                        reason="需要酒店建议",
                        instruction_text="搜索上海酒店",
                    )
                ]
            },
        )
    )

    background_executor = MagicMock()
    background_executor.cancel_task_and_wait = AsyncMock(side_effect=lambda task_id: order.append(f"cancel:{task_id}"))
    background_executor.submit_task = MagicMock()

    orchestrator = Orchestrator(
        session_manager=session_manager,
        scenario_loader=scenario_loader,
        intent_analyzer=MagicMock(),
        planner=planner,
        history_compressor=MagicMock(),
        background_executor=background_executor,
        async_execution=True,
    )

    async def terminate_side_effect(session: Session, active_task: ActiveTask) -> None:
        order.append(f"terminate:{active_task.active_task_id}")

    orchestrator._terminate_current_task = AsyncMock(side_effect=terminate_side_effect)

    session = Session(
        session_id="test-session-handle-task-new",
        mode=ExecutionMode.DIRECT_RPC,
        created_at=now,
        updated_at=now,
        touched_at=now,
        ttl_seconds=3600,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        base_scenario=ScenarioRuntime(
            id="base",
            kind="base",
            version="1.0.0",
            loaded_at=now,
        ),
        active_task=ActiveTask(
            active_task_id="task-old",
            created_at=now,
            external_status=ActiveTaskStatus.RUNNING,
            partner_tasks={
                "partner-hotel": PartnerTask(
                    partnerAic="partner-hotel",
                    aipTaskId="task-old:partner-hotel",
                    state=TaskState.Working,
                )
            },
        ),
        user_result=UserResult(
            type=UserResultType.PENDING,
            data_items=[],
            updated_at=now,
        ),
    )

    intent = IntentDecision(
        intent_type=IntentType.TASK_NEW,
        confidence=0.99,
        task_instruction=TaskInstruction(text="重新规划这次行程"),
        target_scenario="tour",
    )

    with patch("assistant.core.orchestrator.LEADER_EMITTER.emit", new=AsyncMock()):
        await orchestrator._handle_task_new(session, intent, "重新规划这次行程")

    assert order[:2] == ["cancel:task-old", "terminate:task-old"]
    background_executor.cancel_task_and_wait.assert_awaited_once_with("task-old")
    background_executor.submit_task.assert_called_once()
