"""
Leader Agent Platform - CompletionGate Handler

本模块提供 LLM-5 (CompletionGate) 的共享处理逻辑，供 orchestrator 和 background_executor 复用。

设计原则：
- LLM-5 的"continue → 轮询 → 再次 AwaitingCompletion"循环最多执行 3 次
- 若达到最大次数后 Partner 仍处于 AwaitingCompletion，则强制发送 complete

功能：
- 统一的 endpoint 解析逻辑
- CompletionGate 循环处理
- Partner 状态更新
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from acps_sdk.aip.aip_base_model import TaskState

from .completion_gate import (
    AwaitingCompletionGateResult,
    CompletionGate,
    PartnerProductSummary,
)
from .executor import ExecutionPhase, ExecutionResult, extract_partner_endpoint

if TYPE_CHECKING:
    from ..models import Session
    from ..models.task import PlanningResult
    from .executor import TaskExecutor

logger = logging.getLogger(__name__)

# LLM-5 循环最大次数
# 临时调整为 1 以测试性能影响
MAX_COMPLETION_GATE_ROUNDS = 1


def _resolve_repoll_method(executor: Any) -> Callable[..., Any]:
 """解析 continue 后二次轮询所需的方法，兼容 direct/group executor 命名差异。"""
    poll_method = getattr(executor, "_poll_until_converged", None)
    if callable(poll_method):
        return poll_method

    wait_method = getattr(executor, "_wait_until_converged", None)
    if callable(wait_method):
        return wait_method

    raise AttributeError(f"{executor.__class__.__name__} missing convergence polling method")


def resolve_partner_endpoint(
    partner_aic: str,
    planning_result: PlanningResult,
    acs_cache: dict[str, dict[str, Any]] | None = None,
) -> str | None:
 """
    统一的 Partner endpoint 解析函数。

    端点信息必须来自 ACS（静态配置或动态发现），不做任何 fallback 推断。

    Args:
        partner_aic: Partner AIC
        planning_result: 规划结果（未使用，保留接口兼容）
        acs_cache: ACS 缓存（partner_aic -> acs_data）

    Returns:
        endpoint URL 或 None
 """
    _ = planning_result  # 保留参数以兼容调用方签名
    if acs_cache:
        acs_data = acs_cache.get(partner_aic, {})
        endpoint = extract_partner_endpoint(acs_data)
        if endpoint:
            return endpoint

    return None


def build_partner_summaries(
    execution_result: ExecutionResult,
    active_task_id: str,
) -> list[PartnerProductSummary]:
 """
    从 ExecutionResult 构建 Partner 产出物摘要列表。

    Args:
        execution_result: 执行结果
        active_task_id: 活跃任务 ID

    Returns:
        PartnerProductSummary 列表
 """
    partner_summaries = []

    for partner_aic in execution_result.awaiting_completion_partners:
        pr = execution_result.partner_results.get(partner_aic)
        if not pr:
            continue

        dim_id = pr.dimension_id

 # 获取 dataItems
        data_items = []
        if pr.data_items:
            for item in pr.data_items:
                if hasattr(item, "model_dump"):
                    data_items.append(item.model_dump)
                elif hasattr(item, "dict"):
                    data_items.append(item.dict)
                else:
                    data_items.append({"text": str(item)})

 # 获取 products
        products = []
        if pr.task and pr.task.products:
            for product in pr.task.products:
                products.append(
                    {
                        "id": product.id,
                        "name": product.name,
                        "dataItems": [
                            (di.model_dump if hasattr(di, "model_dump") else {"text": str(di)})
                            for di in product.dataItems
                        ],
                    }
                )

 # 详细日志：输出构建的 summary 内容
        def _truncate(text, max_len=200):
            if not text:
                return "<empty>"
            text = str(text)
            return text[:max_len] + "..." if len(text) > max_len else text

        products_preview = "None"
        if products:
            products_preview = f"{len(products)} product(s)"
            for i, p in enumerate(products):
                for j, di in enumerate(p.get("dataItems", [])):
                    text_val = di.get("text", str(di))
                    products_preview += f"\n    [prod{i}.item{j}]: {_truncate(text_val, 150)}"

        data_items_preview = "None"
        if data_items:
            data_items_preview = f"{len(data_items)} item(s)"
            for i, di in enumerate(data_items):
                text_val = di.get("text", str(di))
                data_items_preview += f"\n    [item{i}]: {_truncate(text_val, 100)}"

        logger.info(
            f"[LLM-5 Input] Partner {partner_aic[-8:]}:\n"
            f"  state={pr.state}, dim={dim_id}\n"
            f"  products={products_preview}\n"
            f"  dataItems={data_items_preview}"
        )

        summary = PartnerProductSummary(
            partner_aic=partner_aic,
            aip_task_id=f"{active_task_id}:{partner_aic}",
            dimension_id=dim_id,
            state=pr.state.value if hasattr(pr.state, "value") else str(pr.state),
            data_items=data_items,
            products=products,
        )
        partner_summaries.append(summary)

    return partner_summaries


def update_execution_phase(execution_result: ExecutionResult) -> None:
 """
    根据 Partner 状态更新执行阶段。

    Args:
        execution_result: 执行结果（会被原地修改）
 """
 # 重新分类
    execution_result.awaiting_input_partners = []
    execution_result.awaiting_completion_partners = []
    execution_result.completed_partners = []
    execution_result.failed_partners = []

    for partner_aic, pr in execution_result.partner_results.items:
        if pr.state == TaskState.AwaitingInput:
            execution_result.awaiting_input_partners.append(partner_aic)
        elif pr.state == TaskState.AwaitingCompletion:
            execution_result.awaiting_completion_partners.append(partner_aic)
        elif pr.state == TaskState.Completed:
            execution_result.completed_partners.append(partner_aic)
        elif pr.state in (TaskState.Failed, TaskState.Rejected, TaskState.Canceled):
            execution_result.failed_partners.append(partner_aic)

 # 更新 phase
    if execution_result.awaiting_input_partners:
        execution_result.phase = ExecutionPhase.AWAITING_INPUT
    elif execution_result.awaiting_completion_partners:
        execution_result.phase = ExecutionPhase.AWAITING_COMPLETION
    elif execution_result.completed_partners and not execution_result.failed_partners:
        execution_result.phase = ExecutionPhase.COMPLETED
    elif execution_result.failed_partners:
        execution_result.phase = ExecutionPhase.FAILED


async def apply_gate_decisions(
    gate_result: AwaitingCompletionGateResult,
    execution_result: ExecutionResult,
    session_id: str,
    planning_result: PlanningResult,
    executor: TaskExecutor,
    acs_cache: dict[str, dict[str, Any]] | None = None,
) -> ExecutionResult:
 """
    应用 CompletionGate 的决策结果。

    Args:
        gate_result: LLM-5 决策结果
        execution_result: 当前执行结果
        session_id: 会话 ID
        planning_result: 规划结果
        executor: 任务执行器
        acs_cache: ACS 缓存

    Returns:
        更新后的执行结果
 """
    for decision in gate_result.decisions:
        partner_aic = decision.partner_aic
        aip_task_id = decision.aip_task_id

 # 获取 endpoint
        endpoint = resolve_partner_endpoint(
            partner_aic=partner_aic,
            planning_result=planning_result,
            acs_cache=acs_cache,
        )

        if not endpoint:
            logger.warning(f"No endpoint for {partner_aic}, skipping")
            continue

        if decision.next_action == "complete":
 # 执行 complete
            task, error = await executor.complete_partner(
                session_id=session_id,
                partner_aic=partner_aic,
                aip_task_id=aip_task_id,
                endpoint=endpoint,
            )

            if task:
                pr = execution_result.partner_results.get(partner_aic)
                if pr:
                    pr.state = task.status.state
                    pr.task = task
                    if task.products:
                        for product in task.products:
                            execution_result.products.setdefault(partner_aic, []).extend(product.dataItems)

                if partner_aic in execution_result.awaiting_completion_partners:
                    execution_result.awaiting_completion_partners.remove(partner_aic)
                if partner_aic not in execution_result.completed_partners:
                    execution_result.completed_partners.append(partner_aic)

                logger.info(f"Partner {partner_aic[:8]}... completed")

            elif error:
                logger.error(f"Failed to complete {partner_aic}: {error}")

        elif decision.next_action == "continue":
 # 执行 continue（带 followup 指令）
            followup_text = ""
            if decision.followup:
                followup_text = decision.followup.text
                if decision.followup.data:
                    followup_text += f"\n\n[追加约束]: {decision.followup.data}"

            task, error = await executor.continue_partner(
                session_id=session_id,
                partner_aic=partner_aic,
                aip_task_id=aip_task_id,
                endpoint=endpoint,
                user_input=followup_text,
            )

            if task:
                pr = execution_result.partner_results.get(partner_aic)
                if pr:
                    pr.state = task.status.state
                    pr.task = task

 # Partner 可能进入 Working 状态
                if partner_aic in execution_result.awaiting_completion_partners:
                    execution_result.awaiting_completion_partners.remove(partner_aic)

                logger.info(f"Partner {partner_aic[:8]}... continued, new state: {task.status.state}")

            elif error:
                logger.error(f"Failed to continue {partner_aic}: {error}")

 # 更新 phase
    update_execution_phase(execution_result)

    return execution_result


async def force_complete_all_partners(
    execution_result: ExecutionResult,
    session_id: str,
    active_task_id: str,
    planning_result: PlanningResult,
    executor: TaskExecutor,
    acs_cache: dict[str, dict[str, Any]] | None = None,
) -> ExecutionResult:
 """
    强制 complete 所有 AwaitingCompletion 状态的 Partner。

    Args:
        execution_result: 当前执行结果
        session_id: 会话 ID
        active_task_id: 活跃任务 ID
        planning_result: 规划结果
        executor: 任务执行器
        acs_cache: ACS 缓存

    Returns:
        更新后的执行结果
 """
    for partner_aic in list(execution_result.awaiting_completion_partners):
        endpoint = resolve_partner_endpoint(
            partner_aic=partner_aic,
            planning_result=planning_result,
            acs_cache=acs_cache,
        )

        if not endpoint:
            logger.warning(f"No endpoint for {partner_aic}, cannot force complete")
            continue

        aip_task_id = f"{active_task_id}:{partner_aic}"

        task, error = await executor.complete_partner(
            session_id=session_id,
            partner_aic=partner_aic,
            aip_task_id=aip_task_id,
            endpoint=endpoint,
        )

        if task:
            pr = execution_result.partner_results.get(partner_aic)
            if pr:
                pr.state = task.status.state
                pr.task = task
                if task.products:
                    for product in task.products:
                        execution_result.products.setdefault(partner_aic, []).extend(product.dataItems)

            execution_result.awaiting_completion_partners.remove(partner_aic)
            execution_result.completed_partners.append(partner_aic)
            logger.info(f"Force completed Partner {partner_aic[:8]}...")

        elif error:
            logger.error(f"Failed to force complete {partner_aic}: {error}")

 # 更新 phase
    update_execution_phase(execution_result)

    return execution_result


async def handle_awaiting_completion_with_loop(
    session: Session,
    active_task_id: str,
    execution_result: ExecutionResult,
    planning_result: PlanningResult,
    completion_gate: CompletionGate,
    executor: TaskExecutor,
    acs_cache: dict[str, dict[str, Any]] | None = None,
    scenario_id: str | None = None,
    max_rounds: int = MAX_COMPLETION_GATE_ROUNDS,
    on_progress: Callable[[str], None] | None = None,
) -> ExecutionResult:
 """
    处理 AwaitingCompletion 状态，实现完整的 LLM-5 循环机制。

    设计：
    - 观察到任一 AwaitingCompletion：收集产出物并执行 LLM-5
    - 若 LLM-5 判定可完成：对相关 Partner 发送 complete
    - 若需补充/修正：对相关 Partner 发送 continue，Partner 返回 Working 后继续轮询
    - 循环最多执行 max_rounds 次，超过后强制 complete

    Args:
        session: 会话对象
        active_task_id: 活跃任务 ID
        execution_result: 当前执行结果
        planning_result: 规划结果
        completion_gate: CompletionGate 实例 (LLM-5)
        executor: 任务执行器
        acs_cache: ACS 缓存
        scenario_id: 场景 ID
        max_rounds: 最大循环次数
        on_progress: 进度更新回调函数，接受进度消息字符串

    Returns:
        更新后的执行结果
 """
    if not completion_gate:
        logger.warning("No completion gate configured, auto-completing all")
        return await force_complete_all_partners(
            execution_result=execution_result,
            session_id=session.session_id,
            active_task_id=active_task_id,
            planning_result=planning_result,
            executor=executor,
            acs_cache=acs_cache,
        )

    completion_gate_round = 0

    while execution_result.phase == ExecutionPhase.AWAITING_COMPLETION and completion_gate_round < max_rounds:
        completion_gate_round += 1
        logger.info(f"Task {active_task_id[:8]}: CompletionGate round {completion_gate_round}/{max_rounds}")

 # === 进度更新：完成度评估轮次 ===
        if on_progress:
            on_progress(f"正在进行完成度评估 (第 {completion_gate_round}/{max_rounds} 轮)...")

        try:
 # 构建 Partner 产出物摘要
            partner_summaries = build_partner_summaries(
                execution_result=execution_result,
                active_task_id=active_task_id,
            )

 # 调用 LLM-5 完成闸门
            gate_result = await completion_gate.evaluate(
                partner_summaries=partner_summaries,
                user_constraints=None,  # TODO: 从 session.user_context 获取用户约束
                scenario_id=scenario_id,
            )

            logger.info(f"CompletionGate decided: {len(gate_result.decisions)} decisions")

 # === 进度更新：完成度评估决策结果 ===
            if on_progress:
                complete_count = sum(1 for d in gate_result.decisions if d.next_action == "complete")
                continue_count = sum(1 for d in gate_result.decisions if d.next_action == "continue")
                on_progress(f"评估完成: {complete_count} 个已完成, {continue_count} 个继续执行")

 # 应用决策
            execution_result = await apply_gate_decisions(
                gate_result=gate_result,
                execution_result=execution_result,
                session_id=session.session_id,
                planning_result=planning_result,
                executor=executor,
                acs_cache=acs_cache,
            )

 # 如果有 Partner 进入 Working 状态（continue 后），需要重新轮询
            has_working = any(
                pr.state in (TaskState.Working, TaskState.Accepted) for pr in execution_result.partner_results.values
            )

            if has_working:
                logger.info("Some partners returned to Working, re-polling...")

 # === 进度更新：Partner 继续执行 ===
                if on_progress:
                    working_count = sum(
                        1
                        for pr in execution_result.partner_results.values
                        if pr.state in (TaskState.Working, TaskState.Accepted)
                    )
                    on_progress(f"{working_count} 个 Partner 继续执行中...")

 # 构建 partner_tasks 映射用于轮询
                partner_tasks = _build_partner_tasks_for_polling(
                    active_task_id=active_task_id,
                    planning_result=planning_result,
                    acs_cache=acs_cache,
                )
 # 重新轮询直到收敛
                repoll_until_converged = _resolve_repoll_method(executor)
                execution_result = await repoll_until_converged(
                    session_id=session.session_id,
                    partner_tasks=partner_tasks,
                    result=execution_result,
                )
                update_execution_phase(execution_result)

 # 如果变成了 AWAITING_INPUT，退出循环
            if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
                logger.info("Some partners entered AwaitingInput, exiting loop")
                break

        except Exception as e:
            logger.error(
                f"CompletionGate round {completion_gate_round} failed: {e}",
                exc_info=True,
            )
            break

 # 如果达到最大循环次数后仍有 AwaitingCompletion 的 Partner，强制 complete
    if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
        logger.warning(
            f"Task {active_task_id[:8]}: Max CompletionGate rounds reached, forcing complete for remaining partners"
        )
        execution_result = await force_complete_all_partners(
            execution_result=execution_result,
            session_id=session.session_id,
            active_task_id=active_task_id,
            planning_result=planning_result,
            executor=executor,
            acs_cache=acs_cache,
        )

    return execution_result


def _build_partner_tasks_for_polling(
    active_task_id: str,
    planning_result: PlanningResult,
    acs_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
 """
    构建用于轮询的 partner_tasks 映射。

    Args:
        active_task_id: 活跃任务 ID
        planning_result: 规划结果
        acs_cache: ACS 缓存

    Returns:
        partner_tasks 映射
 """
    partner_tasks = {}

    if not planning_result or not hasattr(planning_result, "selected_partners"):
        return partner_tasks

    for dim_id, selections in planning_result.selected_partners.items:
        for selection in selections:
            partner_aic = selection.partner_aic
            endpoint = resolve_partner_endpoint(
                partner_aic=partner_aic,
                planning_result=planning_result,
                acs_cache=acs_cache,
            )

            if endpoint:
                partner_tasks[partner_aic] = {
                    "dimension_id": dim_id,
                    "aip_task_id": f"{active_task_id}:{partner_aic}",
                    "selection": selection,
                    "endpoint": endpoint,
                }

    return partner_tasks
