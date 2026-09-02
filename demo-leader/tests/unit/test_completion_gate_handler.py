from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from acps_sdk.aip.aip_base_model import Product, TaskResult, TaskState, TaskStatus, TextDataItem
from assistant.core.completion_gate import (
    AwaitingCompletionDecision,
    AwaitingCompletionGateResult,
    FollowupDirective,
)
from assistant.core.completion_gate_handler import (
    _build_partner_tasks_for_polling,
    apply_gate_decisions,
    build_partner_summaries,
    force_complete_all_partners,
    handle_awaiting_completion_with_loop,
    resolve_partner_endpoint,
    update_execution_phase,
)
from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult
from assistant.models.task import PartnerSelection, PlanningResult

NOW = "2026-06-30T00:00:00+00:00"


class DictOnlyItem:
    def dict(self) -> dict[str, str]:
        return {"type": "text", "text": "dict item"}


def _task(
    task_id: str,
    state: TaskState,
    *,
    products: list[Product] | None = None,
    status_items: list[TextDataItem] | None = None,
) -> TaskResult:
    return TaskResult(
        id=f"result-{task_id}",
        sentAt=NOW,
        senderRole="partner",
        senderId="partner-a",
        taskId=task_id,
        sessionId="sess-1",
        status=TaskStatus(state=state, stateChangedAt=NOW, dataItems=status_items),
        products=products,
    )


def _planning() -> PlanningResult:
    return PlanningResult(
        selectedPartners={
            "food": [
                PartnerSelection(
                    partnerAic="partner-a",
                    skillId="skill-food",
                    reason="best match",
                    instructionText="plan food",
                )
            ],
            "hotel": [
                PartnerSelection(
                    partnerAic="partner-b",
                    skillId="skill-hotel",
                    reason="best match",
                    instructionText="plan hotel",
                )
            ],
        }
    )


def _acs_cache() -> dict[str, dict[str, Any]]:
    return {
        "partner-a": {"endPoints": [{"transport": "HTTP", "url": "http://partner-a/rpc"}]},
        "partner-b": {"endPoints": [{"transport": "JSONRPC", "url": "https://partner-b/rpc"}]},
    }


def test_resolve_partner_endpoint_uses_acs_only() -> None:
    assert resolve_partner_endpoint("partner-a", _planning(), _acs_cache()) == "http://partner-a/rpc"
    assert resolve_partner_endpoint("missing", _planning(), _acs_cache()) is None
    assert resolve_partner_endpoint("partner-a", _planning(), None) is None


def test_build_partner_summaries_includes_data_items_and_products() -> None:
    product = Product(
        id="prod-1",
        name="Menu",
        dataItems=[TextDataItem(text="duck")],
    )
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a", "missing"],
        partner_results={
            "partner-a": PartnerExecutionResult(
                partner_aic="partner-a",
                dimension_id="food",
                state=TaskState.AwaitingCompletion,
                task=_task("task-a", TaskState.AwaitingCompletion, products=[product]),
                data_items=[TextDataItem(text="model item"), DictOnlyItem(), "plain item"],  # type: ignore[list-item]
            )
        },
    )

    summaries = build_partner_summaries(result, "active-1")

    assert len(summaries) == 1
    assert summaries[0].aip_task_id == "active-1:partner-a"
    assert summaries[0].data_items[0]["text"] == "model item"
    assert summaries[0].data_items[1]["text"] == "dict item"
    assert summaries[0].data_items[2]["text"] == "plain item"
    assert summaries[0].products[0]["dataItems"][0]["text"] == "duck"


def test_update_execution_phase_classifies_partner_states() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.POLLING,
        partner_results={
            "input": PartnerExecutionResult("input", "d", TaskState.AwaitingInput),
            "completion": PartnerExecutionResult("completion", "d", TaskState.AwaitingCompletion),
            "done": PartnerExecutionResult("done", "d", TaskState.Completed),
            "failed": PartnerExecutionResult("failed", "d", TaskState.Failed),
        },
        completed_partners=["stale"],
    )

    update_execution_phase(result)

    assert result.phase == ExecutionPhase.AWAITING_INPUT
    assert result.awaiting_input_partners == ["input"]
    assert result.awaiting_completion_partners == ["completion"]
    assert result.completed_partners == ["done"]
    assert result.failed_partners == ["failed"]

    result.partner_results = {"done": PartnerExecutionResult("done", "d", TaskState.Completed)}
    update_execution_phase(result)
    assert result.phase == ExecutionPhase.COMPLETED

    result.partner_results = {"failed": PartnerExecutionResult("failed", "d", TaskState.Rejected)}
    update_execution_phase(result)
    assert result.phase == ExecutionPhase.FAILED


@pytest.mark.asyncio
async def test_apply_gate_decisions_completes_and_continues_partners() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a", "partner-b"],
        partner_results={
            "partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion),
            "partner-b": PartnerExecutionResult("partner-b", "hotel", TaskState.AwaitingCompletion),
        },
    )
    completed_task = _task(
        "task-a",
        TaskState.Completed,
        products=[Product(id="prod-a", dataItems=[TextDataItem(text="done")])],
    )
    continued_task = _task("task-b", TaskState.Working)
    executor = SimpleNamespace(
        complete_partner=AsyncMock(return_value=(completed_task, None)),
        continue_partner=AsyncMock(return_value=(continued_task, None)),
    )
    gate_result = AwaitingCompletionGateResult(
        decidedAt=datetime.now(UTC).isoformat(),
        decisions=[
            AwaitingCompletionDecision(
                partnerAic="partner-a",
                aipTaskId="task-a",
                nextAction="complete",
            ),
            AwaitingCompletionDecision(
                partnerAic="partner-b",
                aipTaskId="task-b",
                nextAction="continue",
                followup=FollowupDirective(text="revise", data={"budget": 100}),
            ),
            AwaitingCompletionDecision(
                partnerAic="missing",
                aipTaskId="task-missing",
                nextAction="complete",
            ),
        ],
    )

    updated = await apply_gate_decisions(
        gate_result,
        result,
        "sess-1",
        _planning(),
        executor,  # type: ignore[arg-type]
        _acs_cache(),
    )

    executor.complete_partner.assert_awaited_once_with(
        session_id="sess-1",
        partner_aic="partner-a",
        aip_task_id="task-a",
        endpoint="http://partner-a/rpc",
    )
    executor.continue_partner.assert_awaited_once()
    assert "revise" in executor.continue_partner.await_args.kwargs["user_input"]
    assert "追加约束" in executor.continue_partner.await_args.kwargs["user_input"]
    assert updated.partner_results["partner-a"].state == TaskState.Completed
    assert updated.partner_results["partner-b"].state == TaskState.Working
    assert updated.products["partner-a"][0].text == "done"


@pytest.mark.asyncio
async def test_apply_gate_decisions_logs_executor_errors_without_state_change() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a"],
        partner_results={"partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion)},
    )
    executor = SimpleNamespace(
        complete_partner=AsyncMock(return_value=(None, "complete failed")),
        continue_partner=AsyncMock(return_value=(None, "continue failed")),
    )

    updated = await apply_gate_decisions(
        AwaitingCompletionGateResult(
            decidedAt=NOW,
            decisions=[
                AwaitingCompletionDecision(partnerAic="partner-a", aipTaskId="task-a", nextAction="complete"),
            ],
        ),
        result,
        "sess-1",
        _planning(),
        executor,  # type: ignore[arg-type]
        _acs_cache(),
    )

    assert updated.partner_results["partner-a"].state == TaskState.AwaitingCompletion


@pytest.mark.asyncio
async def test_force_complete_all_partners_updates_result_and_skips_missing_endpoint() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a", "missing"],
        partner_results={
            "partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion),
            "missing": PartnerExecutionResult("missing", "other", TaskState.AwaitingCompletion),
        },
    )
    executor = SimpleNamespace(
        complete_partner=AsyncMock(return_value=(_task("active-1:partner-a", TaskState.Completed), None))
    )

    updated = await force_complete_all_partners(
        result,
        "sess-1",
        "active-1",
        _planning(),
        executor,  # type: ignore[arg-type]
        _acs_cache(),
    )

    executor.complete_partner.assert_awaited_once()
    assert updated.partner_results["partner-a"].state == TaskState.Completed
    assert "partner-a" not in updated.awaiting_completion_partners
    assert "partner-a" in updated.completed_partners


def test_build_partner_tasks_for_polling() -> None:
    tasks = _build_partner_tasks_for_polling("active-1", _planning(), _acs_cache())

    assert tasks["partner-a"]["dimension_id"] == "food"
    assert tasks["partner-a"]["aip_task_id"] == "active-1:partner-a"
    assert tasks["partner-b"]["endpoint"] == "https://partner-b/rpc"
    assert _build_partner_tasks_for_polling("active-1", SimpleNamespace(), _acs_cache()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_handle_awaiting_completion_without_gate_force_completes() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a"],
        partner_results={"partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion)},
    )
    executor = SimpleNamespace(
        complete_partner=AsyncMock(return_value=(_task("active-1:partner-a", TaskState.Completed), None))
    )

    updated = await handle_awaiting_completion_with_loop(
        session=SimpleNamespace(session_id="sess-1"),
        active_task_id="active-1",
        execution_result=result,
        planning_result=_planning(),
        completion_gate=None,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        acs_cache=_acs_cache(),
    )

    assert updated.phase == ExecutionPhase.COMPLETED
    executor.complete_partner.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_awaiting_completion_continue_repolls_and_reports_progress() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a"],
        partner_results={"partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion)},
    )
    gate = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=AwaitingCompletionGateResult(
                decidedAt=NOW,
                decisions=[
                    AwaitingCompletionDecision(
                        partnerAic="partner-a",
                        aipTaskId="task-a",
                        nextAction="continue",
                        followup=FollowupDirective(text="more detail"),
                    )
                ],
            )
        )
    )
    completed = ExecutionResult(
        phase=ExecutionPhase.COMPLETED,
        completed_partners=["partner-a"],
        partner_results={"partner-a": PartnerExecutionResult("partner-a", "food", TaskState.Completed)},
    )
    executor = SimpleNamespace(
        continue_partner=AsyncMock(return_value=(_task("task-a", TaskState.Working), None)),
        complete_partner=AsyncMock(),
        _poll_until_converged=AsyncMock(return_value=completed),
    )
    progress: list[str] = []

    updated = await handle_awaiting_completion_with_loop(
        session=SimpleNamespace(session_id="sess-1"),
        active_task_id="active-1",
        execution_result=result,
        planning_result=_planning(),
        completion_gate=gate,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        acs_cache=_acs_cache(),
        scenario_id="travel",
        max_rounds=1,
        on_progress=progress.append,
    )

    assert updated is completed
    gate.evaluate.assert_awaited_once()
    executor._poll_until_converged.assert_awaited_once()
    assert any("评估完成" in item for item in progress)


@pytest.mark.asyncio
async def test_handle_awaiting_completion_forces_after_max_rounds_and_stops_on_errors() -> None:
    result = ExecutionResult(
        phase=ExecutionPhase.AWAITING_COMPLETION,
        awaiting_completion_partners=["partner-a"],
        partner_results={"partner-a": PartnerExecutionResult("partner-a", "food", TaskState.AwaitingCompletion)},
    )
    gate = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("llm down")))
    executor = SimpleNamespace(
        complete_partner=AsyncMock(return_value=(_task("active-1:partner-a", TaskState.Completed), None))
    )

    updated = await handle_awaiting_completion_with_loop(
        session=SimpleNamespace(session_id="sess-1"),
        active_task_id="active-1",
        execution_result=result,
        planning_result=_planning(),
        completion_gate=gate,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        acs_cache=_acs_cache(),
        max_rounds=1,
    )

    assert updated.phase == ExecutionPhase.COMPLETED
    executor.complete_partner.assert_awaited_once()
