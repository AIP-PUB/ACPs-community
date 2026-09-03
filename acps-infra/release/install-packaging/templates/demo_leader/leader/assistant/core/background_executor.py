"""
Leader Agent Platform - BackgroundExecutor

本模块实现后台任务执行器，负责：
1. 接收 Planning 完成后的任务
2. 在后台异步执行任务（Executor + Aggregator）
3. 更新 TaskExecution 状态供 /result 查询

设计原则：
- 使用 asyncio.create_task 实现真正的异步执行
- /submit 在 Planning 后立即返回，不等待执行完成
- 执行过程中的状态变化实时更新到 TaskExecutionManager
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..models.base import ActiveTaskId, SessionId
from ..models.task import PlanningResult
from ..models.task_execution import (
    TaskExecution,
    TaskExecutionPhase,
)
from .completion_gate_handler import (
    MAX_COMPLETION_GATE_ROUNDS,
    handle_awaiting_completion_with_loop,
    resolve_partner_endpoint,
)
from .executor import ExecutionPhase, ExecutionResult
from .task_execution_manager import TaskExecutionManager, get_task_execution_manager

if TYPE_CHECKING:
    from ..models import Session
    from ..models.task import UserResult
    from .aggregator import Aggregator
    from .clarification_merger import ClarificationMerger
    from .completion_gate import CompletionGate
    from .executor import TaskExecutor
    from .session_manager import SessionManager

logger = logging.getLogger(__name__)


def _build_progress_user_result(message: str) -> UserResult:
 """
    构建进度更新的 UserResult。

    Args:
        message: 进度消息

    Returns:
        UserResult 对象，type=pending
 """
    from ..models.aip import TextDataItem
    from ..models.base import UserResultType, now_iso
    from ..models.task import UserResult

    return UserResult(
        type=UserResultType.PENDING,
        data_items=[
            TextDataItem(
                type="text",
                text=message,
            )
        ],
        updated_at=now_iso,
    )


class BackgroundExecutor:
 """
    后台任务执行器。

    负责在后台执行任务并更新状态，使 /submit 能够快速返回。
 """

    def __init__(
        self,
        task_execution_manager: TaskExecutionManager | None = None,
        executor: TaskExecutor | None = None,
        group_executor: TaskExecutor | None = None,
        aggregator: Aggregator | None = None,
        clarification_merger: ClarificationMerger | None = None,
        completion_gate: CompletionGate | None = None,
        session_manager: SessionManager | None = None,
    ):
 """
        初始化后台执行器。

        Args:
            task_execution_manager: 任务执行管理器
            executor: 任务执行器（Direct RPC）
            group_executor: 群组模式任务执行器
            aggregator: 结果整合器（LLM-6）
            clarification_merger: 反问合并器（LLM-3）
            completion_gate: 完成闸门（LLM-5）
            session_manager: Session 管理器
 """
        self._task_execution_manager = task_execution_manager or get_task_execution_manager
        self._executor = executor
        self._group_executor = group_executor
        self._aggregator = aggregator
        self._clarification_merger = clarification_merger
        self._completion_gate = completion_gate
        self._session_manager = session_manager
        self._planner: Any | None = None  # 由 set_components 设置

 # 运行中的后台任务
        self._running_tasks: dict[ActiveTaskId, asyncio.Task] = {}

 # 用于依赖注入的回调（orchestrator 会设置）
        self._execute_planning_callback: Callable | None = None
        self._build_execution_response_callback: Callable | None = None

    def set_callbacks(
        self,
        execute_planning: Callable | None = None,
        build_execution_response: Callable | None = None,
    ) -> None:
 """
        设置执行回调函数。

        这些回调由 Orchestrator 提供，用于复用其执行逻辑。

        Args:
            execute_planning: 执行规划的回调
            build_execution_response: 构建执行响应的回调
 """
        self._execute_planning_callback = execute_planning
        self._build_execution_response_callback = build_execution_response

    def set_components(
        self,
        executor: TaskExecutor | None = None,
        group_executor: TaskExecutor | None = None,
        aggregator: Aggregator | None = None,
        clarification_merger: ClarificationMerger | None = None,
        completion_gate: CompletionGate | None = None,
        session_manager: SessionManager | None = None,
        planner: Any | None = None,
    ) -> None:
 """
        设置依赖组件（由 Orchestrator 在 start 时调用）。
 """
        if executor:
            self._executor = executor
        if group_executor:
            self._group_executor = group_executor
        if aggregator:
            self._aggregator = aggregator
        if clarification_merger:
            self._clarification_merger = clarification_merger
        if completion_gate:
            self._completion_gate = completion_gate
        if session_manager:
            self._session_manager = session_manager
        if planner:
            self._planner = planner

    def _update_user_progress(self, session: Session, message: str) -> None:
 """
        更新 session.user_result 以显示执行进度。

        此方法用于在 LLM-5/LLM-6 执行期间向前端显示进度信息。

        Args:
            session: 会话对象
            message: 进度消息
 """
        session.user_result = _build_progress_user_result(message)
        if self._session_manager:
            self._session_manager.update_session(session)
        logger.debug(f"[BackgroundExecutor] Progress updated: {message[:50]}...")

    def _is_current_active_task(
        self,
        session: Session,
        task_id: ActiveTaskId,
    ) -> bool:
 """检查 task_id 是否仍然对应当前 session.active_task。"""
        return bool(session.active_task and session.active_task.active_task_id == task_id)

    def resume_after_continue(
        self,
        task_id: ActiveTaskId,
        session: Session,
        planning_result: PlanningResult | None = None,
    ) -> None:
 """
        在 TASK_INPUT 执行 continue 后恢复后台轮询。

        当用户补充信息后，Partner 从 awaiting-input 变为 working，
        需要重新启动后台轮询来监控 Partner 后续状态变化。

        Args:
            task_id: 任务 ID
            session: 会话对象
            planning_result: 规划结果（可选，若无则从 session 重建）
 """
 # 检查是否已有运行中的任务
        if task_id in self._running_tasks:
            bg_task = self._running_tasks[task_id]
            if not bg_task.done:
                logger.debug(f"[BackgroundExecutor] Task {task_id[:12]} already has running background task")
                return

 # 如果没有 planning_result，尝试从 session 重建
        if not planning_result and session.active_task:
            planning_result = self._rebuild_planning_result_from_session(session)

        if not planning_result:
            logger.warning(f"[BackgroundExecutor] Cannot resume task {task_id[:12]}: no planning_result")
            return

 # 启动后台恢复任务
        bg_task = asyncio.create_task(
            self._resume_polling_async(
                task_id=task_id,
                session=session,
                planning_result=planning_result,
            )
        )
        self._running_tasks[task_id] = bg_task

 # 任务完成后清理
        bg_task.add_done_callback(lambda t: self._running_tasks.pop(task_id, None))

        logger.info(f"[BackgroundExecutor] Task {task_id[:12]} resumed for background polling")

    async def cancel_task_and_wait(
        self,
        task_id: ActiveTaskId,
        timeout: float = 5.0,
    ) -> bool:
 """
        取消后台任务并等待其结束，避免旧任务继续回写当前 Session。
 """
        bg_task = self._running_tasks.get(task_id)
        if not bg_task:
            return False

        if bg_task.done:
            return True

        bg_task.cancel
        logger.info(f"Task {task_id} cancellation requested, waiting for shutdown")

        try:
            await asyncio.wait_for(bg_task, timeout=timeout)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            logger.warning(f"Timeout waiting for task {task_id} cancellation")
        except Exception as exc:
            logger.warning(f"Task {task_id} raised during cancellation: {exc}")

        return True

    def _rebuild_planning_result_from_session(
        self,
        session: Session,
    ) -> PlanningResult | None:
 """
        从 session 的 active_task 重建 PlanningResult。

        Args:
            session: 会话对象

        Returns:
            重建的 PlanningResult 或 None
 """
        if not session.active_task or not session.active_task.partner_tasks:
            return None

        from ..models.task import PartnerSelection, PlanningResult

 # 从 partner_tasks 重建 selected_partners
        selected_partners: dict[str, list] = {}

        for partner_aic, partner_task in session.active_task.partner_tasks.items:
 # 获取 dimensions
            dimensions = partner_task.dimensions or ["unknown"]
            dim_id = dimensions[0] if dimensions else "unknown"

 # 构建 PartnerSelection
            selection = PartnerSelection(
                partner_aic=partner_aic,
                partner_name=partner_task.partner_name or partner_aic,
                skill_id="unknown",  # 无法从 session 恢复
                reason="Rebuilt from session for resume polling",  # 必填字段
                instruction_text=partner_task.sub_query or "",
            )

 # Note: endpoint 信息会在 poll_partners 中通过 ACS 服务动态获取

            if dim_id not in selected_partners:
                selected_partners[dim_id] = []
            selected_partners[dim_id].append(selection)

        return PlanningResult(
            scenario_id=(session.expert_scenario.id if session.expert_scenario else "unknown"),
            active_dimensions=list(selected_partners.keys),
            selected_partners=selected_partners,
        )

    async def _resume_polling_async(
        self,
        task_id: ActiveTaskId,
        session: Session,
        planning_result: PlanningResult,
    ) -> None:
 """
        异步恢复轮询的核心逻辑。

        Args:
            task_id: 任务 ID
            session: 会话对象
            planning_result: 规划结果
 """
        import time

        task_start_time = time.time
        short_task_id = task_id[:12]
        logger.info(f"[BackgroundExecutor] === Task {short_task_id} polling resumed ===")

        max_rounds = MAX_COMPLETION_GATE_ROUNDS

        try:
 # 标记为运行中
            self._task_execution_manager.mark_task_running(task_id)

 # 创建轮询更新回调
            def on_poll_update(exec_result: ExecutionResult) -> None:
 """每轮轮询后更新 session 中的 partner_tasks"""
                self._update_partner_tasks_from_execution(
                    session,
                    exec_result,
                    planning_result,
                    task_id=task_id,
                )
                if self._session_manager:
                    self._session_manager.update_session(session)
                logger.debug(f"[BackgroundExecutor] Updated partner_tasks (resume), phase={exec_result.phase.value}")

 # === 进度更新：开始轮询 Partners ===
            partner_count = sum(len(ps) for ps in planning_result.selected_partners.values)
            self._update_user_progress(session, f"正在等待 {partner_count} 个 Partner 响应...")

 # 执行轮询（从当前 Partner 状态开始）
            execution_start = time.time
            logger.debug("[BackgroundExecutor] >>> Resuming partner polling...")

 # 根据 session.mode 选择正确的 executor
            from ..models.base import ExecutionMode

            executor = self._executor
            if session.mode == ExecutionMode.GROUP and self._group_executor:
                executor = self._group_executor
                logger.debug("[BackgroundExecutor] Using GroupTaskExecutor for resume polling")

            if executor:
                execution_result = await executor.poll_partners(
                    planning_result=planning_result,
                    session_id=session.session_id,
                    active_task_id=task_id,
                    on_poll_update=on_poll_update,
                )
            else:
                raise RuntimeError("No executor configured")

            execution_elapsed = (time.time - execution_start) * 1000
            logger.debug(
                f"[BackgroundExecutor] <<< Partner polling completed in {execution_elapsed:.0f}ms, "
                f"phase={execution_result.phase.value}"
            )

 # 更新执行进度
            self._update_progress_from_execution(task_id, execution_result)

 # 最终同步 partner_tasks 到 session
            self._update_partner_tasks_from_execution(
                session,
                execution_result,
                planning_result,
                task_id=task_id,
            )
            if self._session_manager:
                from ..models.base import ActiveTaskStatus

                if self._is_current_active_task(session, task_id):
                    session.active_task.external_status = ActiveTaskStatus.RUNNING
                self._session_manager.update_session(session)

 # 检查是否需要等待用户输入
            if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
                logger.info(f"[BackgroundExecutor] Task {short_task_id} awaiting user input (resume)")
                clarification_text = self._build_clarification_text(execution_result)
                execution_dict = self._serialize_execution_result(execution_result)

                self._task_execution_manager.mark_task_awaiting_input(
                    task_id=task_id,
                    clarification_text=clarification_text,
                    execution_result=execution_dict,
                )

                if self._session_manager:
                    from ..models.aip import TextDataItem
                    from ..models.base import UserResultType, now_iso
                    from ..models.task import UserResult

                    session.user_result = UserResult(
                        type=UserResultType.CLARIFICATION,
                        data_items=[
                            TextDataItem(
                                type="text",
                                text=clarification_text or "请补充更多信息",
                            )
                        ],
                        updated_at=now_iso,
                    )
                    self._session_manager.update_session(session)
                return

 # 处理 AWAITING_COMPLETION 状态
            if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
                logger.debug("[BackgroundExecutor] >>> Starting LLM-5 completion gate loop (resume)...")
                llm5_start = time.time

 # === 进度更新：开始完成度评估 ===
                self._update_user_progress(session, "正在评估 Partner 产出物完整性...")

                scenario_id = session.expert_scenario.id if session.expert_scenario else None
 # 从 planner 获取 ACS 缓存
                acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

 # 创建进度更新回调 (resume path)
                def on_llm5_progress_resume(message: str) -> None:
                    self._update_user_progress(session, message)

                execution_result = await handle_awaiting_completion_with_loop(
                    session=session,
                    active_task_id=task_id,
                    execution_result=execution_result,
                    planning_result=planning_result,
                    completion_gate=self._completion_gate,
                    executor=self._executor,
                    acs_cache=acs_cache,
                    scenario_id=scenario_id,
                    max_rounds=max_rounds,
                    on_progress=on_llm5_progress_resume,
                )

                llm5_elapsed = (time.time - llm5_start) * 1000
                logger.debug(f"[BackgroundExecutor] <<< LLM-5 loop completed in {llm5_elapsed:.0f}ms (resume)")
                self._update_progress_from_execution(task_id, execution_result)

 # 同步 LLM-5 完成后的 partner 状态到 session
                self._update_partner_tasks_from_execution(
                    session,
                    execution_result,
                    planning_result,
                    task_id=task_id,
                )
                if self._session_manager:
                    self._session_manager.update_session(session)

 # 再次检查是否需要等待用户输入
            if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
                logger.info(f"[BackgroundExecutor] Task {short_task_id} awaiting user input (post LLM-5 resume)")
                clarification_text = self._build_clarification_text(execution_result)
                execution_dict = self._serialize_execution_result(execution_result)

                self._task_execution_manager.mark_task_awaiting_input(
                    task_id=task_id,
                    clarification_text=clarification_text,
                    execution_result=execution_dict,
                )

                if self._session_manager:
                    from ..models.aip import TextDataItem
                    from ..models.base import UserResultType, now_iso
                    from ..models.task import UserResult

                    session.user_result = UserResult(
                        type=UserResultType.CLARIFICATION,
                        data_items=[
                            TextDataItem(
                                type="text",
                                text=clarification_text or "请补充更多信息",
                            )
                        ],
                        updated_at=now_iso,
                    )
                    self._session_manager.update_session(session)
                return

 # 执行 LLM-6 聚合
            logger.debug("[BackgroundExecutor] >>> Starting LLM-6 aggregation (resume)...")
            aggregation_start = time.time

 # === 进度更新：开始 LLM-6 整合 ===
            complete_count = sum(1 for r in execution_result.partner_results.values if r.state.value == "completed")
            total_count = len(execution_result.partner_results)
            self._update_user_progress(
                session,
                f"正在整合 Partner 结果 ({complete_count}/{total_count} 完成)...",
            )

            response_text = None
            if self._aggregator:
                from acps_sdk.aip.aip_base_model import TaskState

                from .aggregator import DegradationInfo, PartnerOutput

                partner_outputs = []
                degradations = []

                for partner_aic, result in execution_result.partner_results.items:
                    data_items = []
                    for item in result.data_items:
                        if hasattr(item, "model_dump"):
                            data_items.append(item.model_dump)
                        elif hasattr(item, "dict"):
                            data_items.append(item.dict)
                        elif isinstance(item, dict):
                            data_items.append(item)
                        else:
                            data_items.append({"text": str(item)})

                    products = []
                    if partner_aic in execution_result.products:
                        for prod in execution_result.products[partner_aic]:
                            if hasattr(prod, "model_dump"):
                                products.append(prod.model_dump)
                            elif hasattr(prod, "dict"):
                                products.append(prod.dict)
                            elif isinstance(prod, dict):
                                products.append(prod)
                            else:
                                products.append({"text": str(prod)})

                    partner_output = PartnerOutput(
                        partner_aic=partner_aic,
                        dimension_id=result.dimension_id or "unknown",
                        state=result.state,
                        data_items=data_items,
                        products=products,
                    )
                    partner_outputs.append(partner_output)

                    if result.state in [
                        TaskState.Failed,
                        TaskState.Rejected,
                        TaskState.Canceled,
                    ]:
                        degradations.append(
                            DegradationInfo(
                                partner_aic=partner_aic,
                                dimension_id=result.dimension_id or "unknown",
                                reason=f"Partner ended with state: {result.state.value}",
                            )
                        )

                try:
                    aggregation_result = await self._aggregator.aggregate(
                        partner_outputs=partner_outputs,
                        degradations=degradations,
                        user_constraints={},
                        scenario_id=(session.expert_scenario.id if session.expert_scenario else None),
                    )
                    response_text = aggregation_result.text  # AggregationResult 使用 text 字段
                except Exception as e:
                    logger.error(f"[BackgroundExecutor] Aggregation failed (resume): {e}")
                    response_text = "结果整合失败，请查看各服务的输出。"

            aggregation_elapsed = (time.time - aggregation_start) * 1000
            logger.debug(
                f"[BackgroundExecutor] <<< LLM-6 aggregation completed in {aggregation_elapsed:.0f}ms (resume)"
            )

 # 标记完成
            total_elapsed = (time.time - task_start_time) * 1000
            self._task_execution_manager.mark_task_completed(
                task_id=task_id,
                response_text=response_text,
            )

 # 更新 Session
            if self._session_manager:
                from ..models.aip import TextDataItem
                from ..models.base import ActiveTaskStatus, UserResultType, now_iso
                from ..models.task import UserResult

                session.user_result = UserResult(
                    type=UserResultType.FINAL,
                    data_items=[TextDataItem(type="text", text=response_text or "任务已完成")],
                    updated_at=now_iso,
                )
                if session.active_task:
                    session.active_task.external_status = ActiveTaskStatus.COMPLETED
                self._session_manager.update_session(session)

            logger.info(
                f"[BackgroundExecutor] === Task {short_task_id} completed successfully (resume) in {total_elapsed:.0f}ms ==="
            )

        except Exception as e:
            total_elapsed = (time.time - task_start_time) * 1000
            logger.exception(
                f"[BackgroundExecutor] Task {short_task_id} failed (resume) after {total_elapsed:.0f}ms: {e}"
            )
            self._task_execution_manager.mark_task_failed(
                task_id=task_id,
                error_message=str(e),
                error_details={"exception_type": type(e).__name__},
            )
            if self._session_manager:
                from ..models.aip import TextDataItem
                from ..models.base import ActiveTaskStatus, UserResultType, now_iso
                from ..models.task import UserResult

                session.user_result = UserResult(
                    type=UserResultType.ERROR,
                    data_items=[TextDataItem(type="text", text=f"任务执行失败：{e!s}")],
                    updated_at=now_iso,
                )
                if session.active_task:
                    session.active_task.external_status = ActiveTaskStatus.FAILED
                self._session_manager.update_session(session)

    def submit_task(
        self,
        task_id: ActiveTaskId,
        session_id: SessionId,
        session: Session,
        planning_result: PlanningResult,
        task_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> TaskExecution:
 """
        提交任务到后台执行。

        Args:
            task_id: 任务 ID
            session_id: 会话 ID
            session: 会话对象
            planning_result: 规划结果
            task_text: 任务文本
            metadata: 元数据

        Returns:
            创建的 TaskExecution
 """
 # 序列化 planning_result
        planning_dict = None
        if planning_result:
            try:
                planning_dict = planning_result.model_dump(by_alias=True)
            except Exception as e:
                logger.warning(f"Failed to serialize planning_result: {e}")
                planning_dict = {"error": str(e)}

 # 创建任务执行记录
        task_execution = self._task_execution_manager.create_task(
            task_id=task_id,
            session_id=session_id,
            planning_result=planning_dict,
            metadata={
                **(metadata or {}),
                "task_text": task_text,
            },
        )

 # 设置 Partner 数量
        if planning_result and planning_result.selected_partners:
            total_partners = sum(len(partners) for partners in planning_result.selected_partners.values)
            self._task_execution_manager.update_task_progress(
                task_id=task_id,
                total_partners=total_partners,
            )

 # 启动后台执行任务
        bg_task = asyncio.create_task(
            self._execute_task_async(
                task_id=task_id,
                session=session,
                planning_result=planning_result,
                task_text=task_text,
            )
        )
        self._running_tasks[task_id] = bg_task

 # 任务完成后清理
        bg_task.add_done_callback(lambda t: self._running_tasks.pop(task_id, None))

        logger.info(f"[BackgroundExecutor] Task {task_id[:12]} submitted for background execution")
        return task_execution

    async def _execute_task_async(
        self,
        task_id: ActiveTaskId,
        session: Session,
        planning_result: PlanningResult,
        task_text: str,
    ) -> None:
 """
        异步执行任务的核心逻辑。

        Args:
            task_id: 任务 ID
            session: 会话对象
            planning_result: 规划结果
            task_text: 任务文本
 """
        import time

        task_start_time = time.time
        short_task_id = task_id[:12]
        logger.info(f"[BackgroundExecutor] === Task {short_task_id} execution started ===")

 # 使用共享模块的常量
        max_rounds = MAX_COMPLETION_GATE_ROUNDS

        try:
 # 标记为运行中
            self._task_execution_manager.mark_task_running(task_id)
            logger.debug(f"[BackgroundExecutor] Task {short_task_id} marked as running")

 # 创建轮询更新回调，用于实时同步 partner 状态到 session
            def on_poll_update(exec_result: ExecutionResult) -> None:
 """每轮轮询后更新 session 中的 partner_tasks"""
                self._update_partner_tasks_from_execution(
                    session,
                    exec_result,
                    planning_result,
                    task_id=task_id,
                )
                if self._session_manager:
                    self._session_manager.update_session(session)
                logger.debug(f"[BackgroundExecutor] Updated partner_tasks, phase={exec_result.phase.value}")

 # === 进度更新：开始执行 Partners ===
            partner_count = sum(len(ps) for ps in planning_result.selected_partners.values)
            self._update_user_progress(session, f"正在调度 {partner_count} 个 Partner 执行任务...")

 # 执行规划
            execution_start = time.time
            logger.debug("[BackgroundExecutor] >>> Starting partner execution...")

            if self._execute_planning_callback:
                execution_result = await self._execute_planning_callback(
                    session=session,
                    active_task_id=task_id,
                    planning_result=planning_result,
                    on_poll_update=on_poll_update,  # 传递回调以便实时更新状态
                )
            elif self._executor:
 # 从 planner 获取 ACS 缓存并更新 executor
                if self._planner:
                    acs_cache = getattr(self._planner, "_acs_cache", {})
                    self._executor.acs_cache = acs_cache
                execution_result = await self._executor.execute(
                    planning_result=planning_result,
                    session_id=session.session_id,
                    active_task_id=task_id,
                    on_poll_update=on_poll_update,  # 传递回调
                )
            else:
                raise RuntimeError("No executor configured")

            execution_elapsed = (time.time - execution_start) * 1000
            logger.debug(
                f"[BackgroundExecutor] <<< Partner execution completed in {execution_elapsed:.0f}ms, "
                f"phase={execution_result.phase.value}"
            )

 # 更新执行进度
            self._update_progress_from_execution(task_id, execution_result)

 # 最终同步 partner_tasks 到 session
            self._update_partner_tasks_from_execution(
                session,
                execution_result,
                planning_result,
                task_id=task_id,
            )
            if self._session_manager:
 # 更新 active_task 状态为 running
                if self._is_current_active_task(session, task_id):
                    from ..models.base import ActiveTaskStatus

                    session.active_task.external_status = ActiveTaskStatus.RUNNING
                self._session_manager.update_session(session)

 # 检查是否需要等待用户输入
            if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
                logger.info(f"[BackgroundExecutor] Task {short_task_id} awaiting user input")
                clarification_text = self._build_clarification_text(execution_result)

 # 序列化执行结果（包含 partner 状态信息）
                execution_dict = self._serialize_execution_result(execution_result)

                self._task_execution_manager.mark_task_awaiting_input(
                    task_id=task_id,
                    clarification_text=clarification_text,
                    execution_result=execution_dict,
                )

 # 更新 Session 的 user_result 和对话历史
                if self._session_manager:
                    from ..models.aip import TextDataItem
                    from ..models.base import IntentType, ResponseType, UserResultType, now_iso
                    from ..models.task import UserResult

 # 更新 user_result 为 clarification 状态
                    session.user_result = UserResult(
                        type=UserResultType.CLARIFICATION,
                        data_items=[
                            TextDataItem(
                                type="text",
                                text=clarification_text or "请补充更多信息",
                            )
                        ],
                        updated_at=now_iso,
                    )
                    self._session_manager.update_session(session)

                    self._session_manager.add_dialog_turn(
                        session_id=session.session_id,
                        user_query=task_text,
                        intent_type=IntentType.TASK_NEW,
                        response_type=ResponseType.CLARIFICATION,
                        response_summary=(clarification_text[:100] if clarification_text else None),
                    )
                return

 # 处理 AWAITING_COMPLETION 状态：使用共享的 LLM-5 循环处理函数
            if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
                logger.debug("[BackgroundExecutor] >>> Starting LLM-5 completion gate loop...")
                llm5_start = time.time

 # === 进度更新：开始完成度评估 ===
                self._update_user_progress(session, "正在评估 Partner 产出物完整性...")

 # 获取场景 ID
                scenario_id = session.expert_scenario.id if session.expert_scenario else None
 # 从 planner 获取 ACS 缓存
                acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

 # 创建进度更新回调 (main execution path)
                def on_llm5_progress_main(message: str) -> None:
                    self._update_user_progress(session, message)

                execution_result = await handle_awaiting_completion_with_loop(
                    session=session,
                    active_task_id=task_id,
                    execution_result=execution_result,
                    planning_result=planning_result,
                    completion_gate=self._completion_gate,
                    executor=self._executor,
                    acs_cache=acs_cache,
                    scenario_id=scenario_id,
                    max_rounds=max_rounds,
                    on_progress=on_llm5_progress_main,
                )

                llm5_elapsed = (time.time - llm5_start) * 1000
                logger.debug(
                    f"[BackgroundExecutor] <<< LLM-5 loop completed in {llm5_elapsed:.0f}ms, "
                    f"phase={execution_result.phase.value}"
                )
                self._update_progress_from_execution(task_id, execution_result)

 # 同步 LLM-5 完成后的 partner 状态到 session
                self._update_partner_tasks_from_execution(
                    session,
                    execution_result,
                    planning_result,
                    task_id=task_id,
                )
                if self._session_manager:
                    self._session_manager.update_session(session)

 # 再次检查是否需要等待用户输入（可能在 LLM-5 循环中产生）
            if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
                logger.info(f"[BackgroundExecutor] Task {short_task_id} awaiting user input (post LLM-5)")
                clarification_text = self._build_clarification_text(execution_result)
                execution_dict = self._serialize_execution_result(execution_result)

                self._task_execution_manager.mark_task_awaiting_input(
                    task_id=task_id,
                    clarification_text=clarification_text,
                    execution_result=execution_dict,
                )

                if self._session_manager:
                    from ..models.aip import TextDataItem
                    from ..models.base import IntentType, ResponseType, UserResultType, now_iso
                    from ..models.task import UserResult

 # 更新 user_result 为 clarification 状态
                    session.user_result = UserResult(
                        type=UserResultType.CLARIFICATION,
                        data_items=[
                            TextDataItem(
                                type="text",
                                text=clarification_text or "请补充更多信息",
                            )
                        ],
                        updated_at=now_iso,
                    )
                    self._session_manager.update_session(session)

                    self._session_manager.add_dialog_turn(
                        session_id=session.session_id,
                        user_query=task_text,
                        intent_type=IntentType.TASK_NEW,
                        response_type=ResponseType.CLARIFICATION,
                        response_summary=(clarification_text[:100] if clarification_text else None),
                    )
                return

 # 构建执行响应（调用 LLM-6 Aggregator）
            logger.debug("[BackgroundExecutor] >>> Starting LLM-6 aggregation...")
            aggregation_start = time.time

 # === 进度更新：开始 LLM-6 整合 ===
            complete_count = sum(1 for r in execution_result.partner_results.values if r.state.value == "completed")
            total_count = len(execution_result.partner_results)
            self._update_user_progress(
                session,
                f"正在整合 Partner 结果 ({complete_count}/{total_count} 完成)...",
            )

            response_text = None
            aggregation_dict = None

 # 注意：不使用 _build_execution_response_callback，因为它返回的 SubmitResponse
 # 不包含 response_text（新 API 设计已移除该字段）
 # 直接使用 aggregator 进行结果聚合
            if self._aggregator:
 # 构建 PartnerOutput 列表（需要从 execution_result 转换）
                from acps_sdk.aip.aip_base_model import TaskState

                from .aggregator import DegradationInfo, PartnerOutput

                partner_outputs = []
                degradations = []

                for partner_aic, result in execution_result.partner_results.items:
 # 提取数据项
                    data_items = []
                    for item in result.data_items:
                        if hasattr(item, "model_dump"):
                            data_items.append(item.model_dump)
                        elif hasattr(item, "dict"):
                            data_items.append(item.dict)
                        elif isinstance(item, dict):
                            data_items.append(item)
                        else:
                            data_items.append({"text": str(item)})

 # 从 products 中提取
                    products = []
                    if partner_aic in execution_result.products:
                        for prod in execution_result.products[partner_aic]:
                            if hasattr(prod, "model_dump"):
                                products.append(prod.model_dump)
                            elif hasattr(prod, "dict"):
                                products.append(prod.dict)
                            elif isinstance(prod, dict):
                                products.append(prod)
                            else:
                                products.append({"text": str(prod)})

                    po = PartnerOutput(
                        partner_aic=partner_aic,
                        dimension_id=result.dimension_id,
                        state=(result.state.value if hasattr(result.state, "value") else str(result.state)),
                        data_items=data_items,
                        products=products,
                        error=result.error,
                    )
                    partner_outputs.append(po)

 # 如果有失败，记录降级信息
                    if result.state in (TaskState.Failed, TaskState.Rejected):
                        degradations.append(
                            DegradationInfo(
                                dimension_id=result.dimension_id,
                                reason=result.error or "执行失败",
                                suggestion="请稍后重试或提供更多信息",
                            )
                        )

 # 获取对话摘要（如果有）
                dialog_summary = None
                if session.dialog_context and session.dialog_context.recent_turns:
                    recent_turns = session.dialog_context.recent_turns[-5:]
                    dialog_summary = "\n".join(
                        (
                            f"user: {turn.user_query[:100]}..."
                            if len(turn.user_query) > 100
                            else f"user: {turn.user_query}"
                        )
                        for turn in recent_turns
                    )

 # 获取场景 ID
                scenario_id = session.expert_scenario.id if session.expert_scenario else None

 # 调用 Aggregator
                aggregation_result = await self._aggregator.aggregate(
                    partner_outputs=partner_outputs,
                    degradations=degradations,
                    user_query=task_text,
                    dialog_summary=dialog_summary,
                    scenario_id=scenario_id,
                )
                response_text = aggregation_result.text  # AggregationResult 使用 text 字段
                try:
                    aggregation_dict = aggregation_result.model_dump(by_alias=True)
                except Exception:
                    pass

                aggregation_elapsed = (time.time - aggregation_start) * 1000
                logger.debug(f"[BackgroundExecutor] <<< LLM-6 aggregation completed in {aggregation_elapsed:.0f}ms")
            else:
 # 降级响应
                response_text = f"任务执行完成。\n\n任务 ID: {task_id[:8]}\n执行阶段: {execution_result.phase.value}"
                logger.debug("[BackgroundExecutor] Using fallback response (no aggregator)")

 # 序列化执行结果
            execution_dict = self._serialize_execution_result(execution_result)

 # 标记为完成
            self._task_execution_manager.mark_task_completed(
                task_id=task_id,
                execution_result=execution_dict,
                aggregation_result=aggregation_dict,
                response_text=response_text,
            )

 # 更新 Session 的 user_result 和对话历史
            if self._session_manager and response_text:
 # 更新 user_result，让前端能看到结果
                from ..models.aip import TextDataItem
                from ..models.base import UserResultType, now_iso
                from ..models.task import UserResult

                session.user_result = UserResult(
                    type=UserResultType.FINAL,
                    data_items=[
                        TextDataItem(
                            type="text",
                            text=response_text,
                        )
                    ],
                    updated_at=now_iso,
                )

 # 更新 active_task 状态
                if self._is_current_active_task(session, task_id):
                    from ..models.base import ActiveTaskStatus

                    session.active_task.external_status = ActiveTaskStatus.COMPLETED

 # 同步 partner_tasks 到 active_task
 # 让前端能够通过 /result 接口看到每个 partner 的执行状态
                    self._update_partner_tasks_from_execution(
                        session,
                        execution_result,
                        planning_result,
                        task_id=task_id,
                    )

 # 保存 Session 更新
                self._session_manager.update_session(session)

 # 添加对话历史
                from ..models.base import IntentType, ResponseType

                self._session_manager.add_dialog_turn(
                    session_id=session.session_id,
                    user_query=task_text,
                    intent_type=IntentType.TASK_NEW,
                    response_type=ResponseType.FINAL,
                    response_summary=response_text[:100] if response_text else None,
                )

            total_elapsed = (time.time - task_start_time) * 1000
            logger.info(
                f"[BackgroundExecutor] === Task {short_task_id} completed successfully in {total_elapsed:.0f}ms ==="
            )

        except asyncio.CancelledError:
            total_elapsed = (time.time - task_start_time) * 1000
            logger.info(f"[BackgroundExecutor] Task {short_task_id} was cancelled after {total_elapsed:.0f}ms")
            self._task_execution_manager.mark_task_cancelled(task_id)
 # 更新 Session 状态
            if self._session_manager:
                from ..models.base import ActiveTaskStatus

                if self._is_current_active_task(session, task_id):
                    session.active_task.external_status = ActiveTaskStatus.FAILED
                    self._session_manager.update_session(session)
            raise

        except Exception as e:
            total_elapsed = (time.time - task_start_time) * 1000
            logger.exception(f"[BackgroundExecutor] Task {short_task_id} failed after {total_elapsed:.0f}ms: {e}")
            self._task_execution_manager.mark_task_failed(
                task_id=task_id,
                error_message=str(e),
                error_details={"exception_type": type(e).__name__},
            )
 # 更新 Session 状态为失败
            if self._session_manager:
                from ..models.aip import TextDataItem
                from ..models.base import ActiveTaskStatus, UserResultType, now_iso
                from ..models.task import UserResult

 # 更新 user_result 通知用户任务失败
                session.user_result = UserResult(
                    type=UserResultType.ERROR,
                    data_items=[TextDataItem(type="text", text=f"任务执行失败：{e!s}")],
                    updated_at=now_iso,
                )
 # 更新 active_task 状态
                if session.active_task:
                    session.active_task.external_status = ActiveTaskStatus.FAILED
 # 保存更新
                self._session_manager.update_session(session)

    async def _poll_until_converged(
        self,
        task_id: ActiveTaskId,
        session: Session,
        planning_result: PlanningResult,
        current_result: ExecutionResult,
    ) -> ExecutionResult:
 """
        重新轮询直到所有 Partner 收敛。

        Args:
            task_id: 任务 ID
            session: 会话对象
            planning_result: 规划结果
            current_result: 当前执行结果

        Returns:
            更新后的执行结果
 """
        if not self._executor:
            return current_result

 # 从 planner 获取 ACS 缓存
        acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

 # 构建 partner_tasks 映射
        partner_tasks = {}
        for dim_id, selections in planning_result.selected_partners.items:
            for selection in selections:
                partner_aic = selection.partner_aic
 # 使用共享模块的 resolve_partner_endpoint 函数
                endpoint = resolve_partner_endpoint(
                    partner_aic=partner_aic,
                    planning_result=planning_result,
                    acs_cache=acs_cache,
                )
                if endpoint:
                    partner_tasks[partner_aic] = {
                        "dimension_id": dim_id,
                        "aip_task_id": f"{task_id}:{partner_aic}",
                        "selection": selection,
                        "endpoint": endpoint,
                    }

 # 复用 executor 的轮询逻辑
        return await self._executor._poll_until_converged(
            session_id=session.session_id,
            partner_tasks=partner_tasks,
            result=current_result,
        )

    def _update_progress_from_execution(
        self,
        task_id: ActiveTaskId,
        execution_result: ExecutionResult,
    ) -> None:
 """根据执行结果更新进度。"""
        phase = TaskExecutionPhase.EXECUTION_COMPLETED

        if execution_result.phase == ExecutionPhase.AWAITING_INPUT:
            phase = TaskExecutionPhase.EXECUTION_CLARIFICATION
        elif execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
            phase = TaskExecutionPhase.EXECUTION_COMPLETION_GATE
        elif execution_result.phase == ExecutionPhase.POLLING:
            phase = TaskExecutionPhase.EXECUTION_POLLING

        self._task_execution_manager.update_task_progress(
            task_id=task_id,
            phase=phase,
            completed_partners=len(execution_result.completed_partners),
            failed_partners=len(execution_result.failed_partners),
            awaiting_input_partners=len(execution_result.awaiting_input_partners),
        )

    def _serialize_execution_result(
        self,
        execution_result: ExecutionResult,
    ) -> dict | None:
 """
        序列化执行结果为可 JSON 化的字典。

        Args:
            execution_result: 执行结果对象

        Returns:
            序列化后的字典
 """
        try:
 # 序列化 partner_results
            partner_results_dict = {}
            for partner_aic, pr in execution_result.partner_results.items:
                partner_info = {
                    "state": pr.state.value if pr.state else None,
                    "error": pr.error,
                }
 # 尝试提取 data_items 中的文本
                if pr.data_items:
                    texts = []
                    for item in pr.data_items:
                        if hasattr(item, "text"):
                            texts.append(item.text)
                        elif isinstance(item, dict) and "text" in item:
                            texts.append(item["text"])
                    if texts:
                        partner_info["data_items_text"] = texts
                partner_results_dict[partner_aic] = partner_info

            return {
                "phase": execution_result.phase.value,
                "partner_results": partner_results_dict,
                "awaiting_input_partners": execution_result.awaiting_input_partners,
                "awaiting_completion_partners": execution_result.awaiting_completion_partners,
                "completed_partners": execution_result.completed_partners,
                "failed_partners": execution_result.failed_partners,
            }
        except Exception as e:
            logger.warning(f"Failed to serialize execution_result: {e}")
            return None

    def _build_clarification_text(
        self,
        execution_result: ExecutionResult,
    ) -> str:
 """从执行结果构建反问文本。"""
        if execution_result.questions_for_user:
            questions = []
            for item in execution_result.questions_for_user:
                if hasattr(item, "text"):
                    questions.append(f"- {item.text}")
                else:
                    questions.append(f"- {item!s}")
            if questions:
                return "为了更好地完成您的请求，请提供以下信息：\n\n" + "\n".join(questions)

        if execution_result.awaiting_input_partners:
            partners = ", ".join(execution_result.awaiting_input_partners)
            return f"以下服务需要更多信息：{partners}\n\n请补充相关信息。"

        return "请提供更多信息以继续处理您的请求。"

    def _update_partner_tasks_from_execution(
        self,
        session: Session,
        execution_result: ExecutionResult,
        planning_result: PlanningResult,
        task_id: ActiveTaskId | None = None,
    ) -> None:
 """
        根据执行结果更新 session.active_task.partner_tasks。

        这样前端通过 /result 接口可以看到每个 partner 的执行状态。

        注意：此方法更新现有的 partner_tasks，而不是替换，
        以保留 orchestrator 初始化时设置的 sub_query 等信息。

        Args:
            session: 会话对象
            execution_result: 执行结果
            planning_result: 规划结果
            task_id: 期望写入的 activeTaskId；若当前 session.active_task 已切换，则跳过写入
 """
        if not session.active_task:
            return

        if task_id and session.active_task.active_task_id != task_id:
            logger.debug(
                "[BackgroundExecutor] Skip stale partner_tasks update: task=%s current=%s",
                task_id[:12],
                session.active_task.active_task_id[:12],
            )
            return

        from acps_sdk.aip.aip_base_model import TaskState

        from ..models.base import now_iso
        from ..models.task import PartnerTask

 # 获取现有的 partner_tasks（如果已由 orchestrator 初始化）
        existing_partner_tasks = session.active_task.partner_tasks or {}
        task_id = session.active_task.active_task_id

 # 遍历执行结果中的每个 partner，更新其状态
        for partner_aic, result in execution_result.partner_results.items:
            if partner_aic in existing_partner_tasks:
 # 更新现有 partner_task 的状态
                existing_task = existing_partner_tasks[partner_aic]
                existing_task.state = result.state or TaskState.Unknown
                existing_task.last_state_changed_at = now_iso
 # 如果有 snapshot，也可以更新
                if hasattr(result, "snapshot") and result.snapshot:
                    existing_task.last_snapshot = result.snapshot
            else:
 # 新增的 partner（理论上不应该发生，但做好防御）
                dimensions = []
                for dim_id, selections in planning_result.selected_partners.items:
                    for selection in selections:
                        if selection.partner_aic == partner_aic:
                            dimensions.append(dim_id)

                partner_task = PartnerTask(
                    partner_aic=partner_aic,
                    aip_task_id=f"{task_id}:{partner_aic}",
                    dimensions=dimensions if dimensions else None,
                    state=result.state or TaskState.Unknown,
                    last_state_changed_at=now_iso,
                )
                existing_partner_tasks[partner_aic] = partner_task

 # 更新 session.active_task.partner_tasks
        session.active_task.partner_tasks = existing_partner_tasks

        logger.debug(
            f"Updated partner_tasks with {len(existing_partner_tasks)} entries, "
            f"states: {[f'{k[-8:]}={v.state.value}' for k, v in existing_partner_tasks.items]}"
        )

    def cancel_task(self, task_id: ActiveTaskId) -> bool:
 """
        取消正在执行的任务。

        Args:
            task_id: 任务 ID

        Returns:
            是否成功取消
 """
        bg_task = self._running_tasks.get(task_id)
        if bg_task and not bg_task.done:
            bg_task.cancel
            logger.info(f"Task {task_id} cancellation requested")
            return True
        return False

    def get_running_task_ids(self) -> list:
 """获取所有运行中的任务 ID。"""
        return list(self._running_tasks.keys)

    async def wait_for_task(
        self,
        task_id: ActiveTaskId,
        timeout: float | None = None,
    ) -> TaskExecution | None:
 """
        等待任务完成（用于测试）。

        Args:
            task_id: 任务 ID
            timeout: 超时时间（秒）

        Returns:
            完成后的 TaskExecution
 """
        bg_task = self._running_tasks.get(task_id)
        if bg_task:
            try:
                await asyncio.wait_for(bg_task, timeout=timeout)
            except TimeoutError:
                logger.warning(f"Timeout waiting for task {task_id}")
            except asyncio.CancelledError:
                pass

        return self._task_execution_manager.get_task(task_id)


# =============================================================================
# 单例获取函数
# =============================================================================

_background_executor: BackgroundExecutor | None = None


def get_background_executor -> BackgroundExecutor:
 """
    获取 BackgroundExecutor 单例。

    Returns:
        BackgroundExecutor 实例
 """
    global _background_executor
    if _background_executor is None:
        _background_executor = BackgroundExecutor
    return _background_executor


def reset_background_executor -> None:
 """
    重置 BackgroundExecutor 单例（仅用于测试）。
 """
    global _background_executor
    _background_executor = None
