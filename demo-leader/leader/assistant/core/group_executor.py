"""
Leader Agent Platform - 群组模式任务执行器

本模块实现群组模式下的任务执行，与 Direct RPC 模式的 TaskExecutor 接口一致。

主要区别：
- 通过 RabbitMQ 消息队列发送命令，而非 RPC 调用
- Partner 邀请在第一次执行时完成
- 状态通过消息队列异步推送，而非轮询
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from acps_sdk.aip.aip_base_model import (
    TaskResult,
    TaskState,
    TextDataItem,
)

from ..models.task import (
    PartnerSelection,
    PlanningResult,
)
from .executor import (
    ExecutionPhase,
    ExecutionResult,
    PartnerExecutionResult,
)
from .group_manager import GroupManager

logger = logging.getLogger(__name__)


# =============================================================================
# 群组模式执行器配置
# =============================================================================


@dataclass
class GroupExecutorConfig:
    """群组模式执行器配置"""

    # 状态收敛配置 — ACPS_GROUP_MAX_WAIT_SECONDS overrides for slow LLM / acceptance.
    max_wait_seconds: int = int((os.environ.get("ACPS_GROUP_MAX_WAIT_SECONDS") or "600").strip() or "600")
    poll_interval_ms: int = 1000  # 状态检查间隔（毫秒）
    max_execution_rounds: int = 600  # 最大检查轮次

    # Partner 邀请配置
    invite_timeout_seconds: int = 60  # 邀请超时时间（秒）


# =============================================================================
# 群组模式任务执行器
# =============================================================================


class GroupTaskExecutor:
    """
    群组模式任务执行器

    与 TaskExecutor 接口一致，但通过 RabbitMQ 消息队列与 Partner 通信。

    工作流程：
    1. 检查是否需要邀请新 Partner
    2. 通过消息队列发送 start 命令
    3. 等待 Partner 状态更新（通过消息队列推送）
    4. 收敛后返回结果
    """

    def __init__(
        self,
        leader_aic: str,
        group_manager: GroupManager,
        config: GroupExecutorConfig | None = None,
        acs_cache: dict[str, dict[str, Any]] | None = None,
    ):
        """
        初始化群组执行器

        Args:
            leader_aic: Leader 的 AIC
            group_manager: 群组管理器实例
            config: 执行器配置
            acs_cache: ACS 缓存
        """
        self.leader_aic = leader_aic
        self.group_manager = group_manager
        self.config = config or GroupExecutorConfig()
        self.acs_cache = acs_cache or {}

        # 已邀请的 Partner 集合（session_id -> set of partner_aic）
        self._invited_partners: dict[str, set] = {}

    async def execute(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
        """
        执行规划结果（群组模式）

        流程：
        1. 邀请未加入群组的 Partner
        2. 为每个 Partner 发送 start 命令
        3. 等待所有 Partner 状态收敛

        Args:
            session_id: 会话 ID
            active_task_id: 活跃任务 ID
            planning_result: 规划结果
            on_poll_update: 每轮检查后的回调函数

        Returns:
            ExecutionResult
        """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_task = active_task_id[-12:] if len(active_task_id) > 12 else active_task_id
        exec_start_time = asyncio.get_event_loop().time()

        from assistant.message_setup import start_trace

        start_trace()

        result = ExecutionResult(phase=ExecutionPhase.STARTING)

        # 收集所有需要执行的 Partner
        partner_tasks = self._build_partner_tasks(session_id, active_task_id, planning_result)

        if not partner_tasks:
            logger.warning("[GroupExecutor:%s] No active partners to execute", short_session)
            result.phase = ExecutionPhase.COMPLETED
            return result

        partner_list = [f"...{p[-8:]}" for p in partner_tasks.keys()]
        logger.info(
            "[GroupExecutor:%s] >>> Starting execution: task=...%s, partners=%s",
            short_session,
            short_task,
            partner_list,
        )

        try:
            # Phase 0: 确保群组已创建
            logger.debug("[GroupExecutor:%s] Phase 0: Ensuring group exists...", short_session)
            group_id = await self.group_manager.create_group_for_session(session_id)
            short_group = group_id[-8:] if len(group_id) > 8 else group_id
            logger.info(
                "[GroupExecutor:%s] Group ready: group_id=...%s",
                short_session,
                short_group,
            )

            # Phase 1: 邀请未加入群组的 Partner
            logger.debug("[GroupExecutor:%s] Phase 1: Inviting partners...", short_session)
            invite_results = await self._ensure_partners_invited(session_id, partner_tasks, planning_result)

            # 检查邀请结果
            invited_count = 0
            for partner_aic, success in invite_results.items():
                if not success:
                    dim_id = partner_tasks[partner_aic]["dimension_id"]
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=TaskState.Failed,
                        error="Failed to invite partner to group",
                    )
                    result.failed_partners.append(partner_aic)
                    logger.warning(
                        "[GroupExecutor:%s] Partner invite failed: ...%s",
                        short_session,
                        partner_aic[-8:],
                    )
                else:
                    invited_count += 1

            logger.debug(
                "[GroupExecutor:%s] Phase 1 complete: %d/%d invited successfully",
                short_session,
                invited_count,
                len(partner_tasks),
            )

            # Phase 2: 发送 start 命令
            logger.debug("[GroupExecutor:%s] Phase 2: Sending START commands...", short_session)
            start_results = await self._start_all_partners(session_id, partner_tasks, result)

            # 更新结果（初始状态）
            for partner_aic, task_info in partner_tasks.items():
                if partner_aic in result.failed_partners:
                    continue  # 跳过已失败的

                dim_id = task_info["dimension_id"]
                result.partner_results[partner_aic] = PartnerExecutionResult(
                    partner_aic=partner_aic,
                    dimension_id=dim_id,
                    state=TaskState.Accepted,  # 初始假设已接受
                )

            # 调用回调通知 start 阶段完成
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(
                        "[GroupExecutor:%s] on_poll_update callback failed after start: %s",
                        short_session,
                        e,
                    )

            logger.debug(
                "[GroupExecutor:%s] Phase 2 complete, starting Phase 3: Wait for convergence...",
                short_session,
            )

            # Phase 3: 等待状态收敛
            result = await self._wait_until_converged(session_id, partner_tasks, result, on_poll_update)

            exec_elapsed_ms = (asyncio.get_event_loop().time() - exec_start_time) * 1000
            logger.info(
                "[GroupExecutor:%s] <<< Execution complete: phase=%s, completed=%d, failed=%d, elapsed=%.0fms",
                short_session,
                result.phase.name,
                len(result.completed_partners),
                len(result.failed_partners),
                exec_elapsed_ms,
            )

        except Exception as e:
            exec_elapsed_ms = (asyncio.get_event_loop().time() - exec_start_time) * 1000
            logger.error(
                "[GroupExecutor:%s] Execution FAILED: %s, elapsed=%.0fms",
                short_session,
                str(e)[:100],
                exec_elapsed_ms,
            )
            result.phase = ExecutionPhase.FAILED
            result.error = str(e)

        return result

    async def poll_partners(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
        """
        从当前状态开始等待 Partner 收敛（不发送 start）

        用于 TASK_INPUT 处理后恢复等待。

        Args:
            session_id: 会话 ID
            active_task_id: 活跃任务 ID
            planning_result: 规划结果
            on_poll_update: 每轮检查后的回调函数

        Returns:
            ExecutionResult
        """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        poll_start_time = asyncio.get_event_loop().time()

        result = ExecutionResult(phase=ExecutionPhase.POLLING)

        # 收集所有需要等待的 Partner
        partner_tasks = self._build_partner_tasks(session_id, active_task_id, planning_result)

        if not partner_tasks:
            logger.warning("[GroupExecutor:%s] No active partners to poll", short_session)
            result.phase = ExecutionPhase.COMPLETED
            return result

        partner_list = [f"...{p[-8:]}" for p in partner_tasks.keys()]
        logger.info(
            "[GroupExecutor:%s] >>> Resuming polling for partners: %s",
            short_session,
            partner_list,
        )

        try:
            # 获取当前状态初始化 result
            logger.debug("[GroupExecutor:%s] Getting current state from group...", short_session)
            self._update_result_from_group_state(session_id, partner_tasks, result)

            # 调用回调通知初始状态
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(
                        "[GroupExecutor:%s] on_poll_update callback failed: %s",
                        short_session,
                        e,
                    )

            # 等待状态收敛
            result = await self._wait_until_converged(session_id, partner_tasks, result, on_poll_update)

            poll_elapsed_ms = (asyncio.get_event_loop().time() - poll_start_time) * 1000
            logger.info(
                "[GroupExecutor:%s] <<< Polling complete: phase=%s, elapsed=%.0fms",
                short_session,
                result.phase.name,
                poll_elapsed_ms,
            )

        except Exception as e:
            poll_elapsed_ms = (asyncio.get_event_loop().time() - poll_start_time) * 1000
            logger.error(
                "[GroupExecutor:%s] Polling FAILED: %s, elapsed=%.0fms",
                short_session,
                str(e)[:100],
                poll_elapsed_ms,
            )
            result.phase = ExecutionPhase.FAILED
            result.error = str(e)

        return result

    def _build_partner_tasks(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
    ) -> dict[str, dict[str, Any]]:
        """
        从规划结果构建 Partner 任务列表

        Returns:
            Dict[partner_aic, {dimension_id, aip_task_id, selection}]
        """
        partner_tasks = {}

        for dim_id, selections in planning_result.selected_partners.items():
            for selection in selections:
                partner_aic = selection.partner_aic
                # Discovery 可能把 Leader 自身 ACS 一并召回；禁止自邀入组。
                if partner_aic and partner_aic == self.leader_aic:
                    logger.warning(
                        "[GroupExecutor] skip self-invite partner_aic=%s (leader)",
                        partner_aic[-12:],
                    )
                    continue

                # 生成 AIP Task ID
                aip_task_id = f"{active_task_id}:{partner_aic}"

                partner_tasks[partner_aic] = {
                    "dimension_id": dim_id,
                    "aip_task_id": aip_task_id,
                    "selection": selection,
                }

        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        logger.debug(
            "[GroupExecutor:%s] Built %d partner tasks from planning result",
            short_session,
            len(partner_tasks),
        )
        return partner_tasks

    async def _ensure_partners_invited(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
        planning_result: PlanningResult,
    ) -> dict[str, bool]:
        """
        确保所有 Partner 都已加入群组

        Returns:
            Dict[partner_aic, success]
        """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id

        # 获取已邀请的 Partner 集合
        invited = self._invited_partners.setdefault(session_id, set())

        # 找出未邀请的 Partner
        to_invite = []
        for partner_aic, task_info in partner_tasks.items():
            if partner_aic not in invited:
                selection = task_info["selection"]
                # 填充 acs_data
                if not selection.acs_data:
                    selection.acs_data = self.acs_cache.get(partner_aic, {})
                to_invite.append(selection)

        if not to_invite:
            # 所有 Partner 都已邀请
            logger.debug(
                "[GroupExecutor:%s] All %d partners already invited",
                short_session,
                len(partner_tasks),
            )
            return dict.fromkeys(partner_tasks, True)

        to_invite_list = [f"...{s.partner_aic[-8:]}" for s in to_invite]
        logger.info(
            "[GroupExecutor:%s] Inviting %d new partner(s): %s",
            short_session,
            len(to_invite),
            to_invite_list,
        )

        # 邀请 Partner
        invite_results = await self.group_manager.invite_partners(
            session_id=session_id,
            partner_selections=to_invite,
        )

        # 更新已邀请集合
        for partner_aic, success in invite_results.items():
            if success:
                invited.add(partner_aic)

        # 返回所有 Partner 的邀请状态
        results = {}
        for partner_aic in partner_tasks:
            if partner_aic in invited:
                results[partner_aic] = True
            else:
                results[partner_aic] = invite_results.get(partner_aic, False)

        return results

    async def _start_all_partners(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
        result: ExecutionResult,
    ) -> None:
        """
        向所有 Partner 发送 start 命令

        通过群组消息队列广播，使用 mentions 定向到目标 Partner
        """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        active_count = len(partner_tasks) - len(result.failed_partners)

        logger.info(
            "[GroupExecutor:%s] >>> Sending START commands to %d partner(s)...",
            short_session,
            active_count,
        )

        start_time = asyncio.get_event_loop().time()
        sent_count = 0

        for partner_aic, task_info in partner_tasks.items():
            short_aic = partner_aic[-8:] if len(partner_aic) > 8 else partner_aic

            if partner_aic in result.failed_partners:
                logger.debug(
                    "[GroupExecutor:%s] [Partner:...%s] Skipping (already failed)",
                    short_session,
                    short_aic,
                )
                continue  # 跳过已失败的（如邀请失败）

            selection: PartnerSelection = task_info["selection"]
            aip_task_id = task_info["aip_task_id"]

            # 构建发送给 Partner 的内容
            task_content = selection.instruction_text or ""
            if selection.instruction_data:
                task_content += f"\n\n[结构化参数]: {selection.instruction_data}"

            content_preview = task_content[:60] + "..." if len(task_content) > 60 else task_content

            try:
                logger.debug(
                    "[GroupExecutor:%s] [Partner:...%s] Sending START: content=%s",
                    short_session,
                    short_aic,
                    content_preview.replace("\n", " "),
                )

                await self.group_manager.start_task(
                    session_id=session_id,
                    task_id=aip_task_id,
                    task_content=task_content,
                    target_partners=[partner_aic],
                )

                sent_count += 1
                logger.debug(
                    "[GroupExecutor:%s] [Partner:...%s] START sent successfully",
                    short_session,
                    short_aic,
                )

            except Exception as e:
                logger.error(
                    "[GroupExecutor:%s] [Partner:...%s] START FAILED: %s",
                    short_session,
                    short_aic,
                    str(e)[:100],
                )
                dim_id = task_info["dimension_id"]
                result.partner_results[partner_aic] = PartnerExecutionResult(
                    partner_aic=partner_aic,
                    dimension_id=dim_id,
                    state=TaskState.Failed,
                    error=f"Failed to send start command: {e}",
                )
                result.failed_partners.append(partner_aic)

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        logger.info(
            "[GroupExecutor:%s] <<< START commands sent: %d/%d succeeded, elapsed=%.0fms",
            short_session,
            sent_count,
            active_count,
            elapsed_ms,
        )

    async def _wait_until_converged(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
        result: ExecutionResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
        """
        等待所有 Partner 状态收敛

        收敛条件：
        - 所有 Partner 进入终态（Completed/Failed/Rejected/Canceled）
        - 或存在 AwaitingInput（需要用户输入）
        - 或存在 AwaitingCompletion（需要 LLM-5 决策）
        """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        result.phase = ExecutionPhase.POLLING

        start_time = datetime.now(UTC)
        timeout = timedelta(seconds=self.config.max_wait_seconds)
        check_interval = self.config.poll_interval_ms / 1000.0

        round_count = 0
        logger.info(
            "[GroupExecutor:%s] Waiting for convergence: interval=%.1fs, timeout=%ds, max_rounds=%d",
            short_session,
            check_interval,
            self.config.max_wait_seconds,
            self.config.max_execution_rounds,
        )

        while round_count < self.config.max_execution_rounds:
            round_count += 1

            # 检查超时
            elapsed = datetime.now(UTC) - start_time
            if elapsed > timeout:
                logger.warning(
                    "[GroupExecutor:%s] TIMEOUT after %d rounds (%.1fs)",
                    short_session,
                    round_count,
                    elapsed.total_seconds(),
                )
                result.phase = ExecutionPhase.TIMEOUT
                result.error = "Execution timeout"
                break

            # 等待检查间隔
            await asyncio.sleep(check_interval)

            # 从群组状态更新结果
            self._update_result_from_group_state(session_id, partner_tasks, result)

            # 记录本轮状态摘要
            state_summary = (
                f"completed={len(result.completed_partners)}, "
                f"failed={len(result.failed_partners)}, "
                f"awaiting_input={len(result.awaiting_input_partners)}, "
                f"awaiting_completion={len(result.awaiting_completion_partners)}"
            )
            logger.debug(
                "[GroupExecutor:%s] Round %d: %s",
                short_session,
                round_count,
                state_summary,
            )

            # 调用回调
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(
                        "[GroupExecutor:%s] on_poll_update callback failed: %s",
                        short_session,
                        e,
                    )

            # 检查是否收敛
            converged, phase = self._check_convergence(result)
            if converged:
                result.phase = phase
                elapsed_sec = (datetime.now(UTC) - start_time).total_seconds()
                logger.info(
                    "[GroupExecutor:%s] CONVERGED after %d rounds (%.1fs): phase=%s",
                    short_session,
                    round_count,
                    elapsed_sec,
                    phase.name,
                )
                break

        return result

    def _update_result_from_group_state(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
        result: ExecutionResult,
    ) -> None:
        """
        从群组状态更新执行结果

        注意：每轮更新时需要先清空分类列表，避免重复累积。
        这与 TaskExecutor._classify_results 的做法一致。
        """
        # 先清空分类列表，避免每轮累积
        result.awaiting_input_partners = []
        result.awaiting_completion_partners = []
        result.completed_partners = []
        result.failed_partners = []
        result.questions_for_user = []

        for partner_aic, task_info in partner_tasks.items():
            # 检查是否已经在 partner_results 中标记为失败
            existing = result.partner_results.get(partner_aic)
            if existing and existing.state in (
                TaskState.Failed,
                TaskState.Rejected,
                TaskState.Canceled,
            ):
                # 保持失败状态，重新分类
                self._categorize_partner(result, partner_aic, existing.state, None)
                continue

            aip_task_id = task_info["aip_task_id"]
            dim_id = task_info["dimension_id"]

            # 获取 Partner 在该任务的状态
            states = self.group_manager.get_partner_states(session_id, aip_task_id)
            state = states.get(partner_aic)

            if not state:
                # 还没有状态更新，保持当前状态
                if partner_aic not in result.partner_results:
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=TaskState.Accepted,  # 默认假设已接受
                    )
                continue

            # 获取产出物和提示
            products = self.group_manager.get_partner_products(session_id, aip_task_id)
            prompts = self.group_manager.get_partner_prompts(session_id, aip_task_id)

            product_text = products.get(partner_aic)
            prompt_text = prompts.get(partner_aic)

            short_partner = partner_aic[-8:] if len(partner_aic) > 8 else partner_aic
            logger.info(
                f"[GroupExecutor] Data selection for {short_partner}: "
                f"state={state.value} has_product={product_text is not None} "
                f"product_len={len(product_text) if product_text else 0} "
                f"has_prompt={prompt_text is not None} "
                f"prompt_len={len(prompt_text) if prompt_text else 0}"
            )

            # 根据状态选择正确的数据来构建 DataItem
            # - AwaitingInput: 使用 prompt_text（反问内容）
            # - AwaitingCompletion/Completed: 使用 product_text（产出物）
            data_items = []
            if state == TaskState.AwaitingInput:
                # 反问状态，使用 prompt
                if prompt_text:
                    data_items.append(TextDataItem(text=prompt_text))
                    logger.info(f"[GroupExecutor] {short_partner}: using PROMPT (state=AwaitingInput)")
            elif state in (TaskState.AwaitingCompletion, TaskState.Completed):
                # 完成状态，使用 product
                if product_text:
                    data_items.append(TextDataItem(text=product_text))
                    logger.info(f"[GroupExecutor] {short_partner}: using PRODUCT (state={state.value})")
                elif prompt_text:
                    # 如果没有 product，降级使用 prompt（兜底）
                    logger.warning(
                        f"[GroupExecutor] Partner {short_partner} in {state.value} "
                        f"but has no product, falling back to prompt"
                    )
                    data_items.append(TextDataItem(text=prompt_text))
            else:
                # 其他状态（Working, Failed 等），优先用 prompt
                if prompt_text:
                    data_items.append(TextDataItem(text=prompt_text))
                    logger.info(f"[GroupExecutor] {short_partner}: using PROMPT (state={state.value})")

            # 更新结果
            result.partner_results[partner_aic] = PartnerExecutionResult(
                partner_aic=partner_aic,
                dimension_id=dim_id,
                state=state,
                data_items=data_items,
            )

            # 更新分类列表
            self._categorize_partner(result, partner_aic, state, data_items)

    def _categorize_partner(
        self,
        result: ExecutionResult,
        partner_aic: str,
        state: TaskState,
        data_items: list[Any] | None = None,
    ) -> None:
        """
        根据状态分类 Partner，并收集反问内容。

        与 TaskExecutor._classify_results 保持一致的逻辑：
        当 Partner 处于 AwaitingInput 状态时，将其 data_items 收集到
        result.questions_for_user 中，以便后续生成用户友好的反问文本。
        """
        # 初始化 questions_for_user（如果不存在）
        if not hasattr(result, "questions_for_user") or result.questions_for_user is None:
            result.questions_for_user = []

        # 先清除旧分类
        for lst in [
            result.awaiting_input_partners,
            result.awaiting_completion_partners,
            result.completed_partners,
            result.failed_partners,
        ]:
            if partner_aic in lst:
                lst.remove(partner_aic)

        # 添加新分类
        if state == TaskState.AwaitingInput:
            if partner_aic not in result.awaiting_input_partners:
                result.awaiting_input_partners.append(partner_aic)
            # 收集需要用户回答的问题（与 TaskExecutor._classify_results 保持一致）
            if data_items:
                result.questions_for_user.extend(data_items)
        elif state == TaskState.AwaitingCompletion:
            if partner_aic not in result.awaiting_completion_partners:
                result.awaiting_completion_partners.append(partner_aic)
        elif state == TaskState.Completed:
            if partner_aic not in result.completed_partners:
                result.completed_partners.append(partner_aic)
        elif state in (TaskState.Failed, TaskState.Rejected, TaskState.Canceled):
            if partner_aic not in result.failed_partners:
                result.failed_partners.append(partner_aic)

    def _check_convergence(self, result: ExecutionResult) -> tuple[bool, ExecutionPhase]:
        """
        检查是否达到收敛条件

        收敛逻辑：必须等待所有 Partner 都进入稳定状态（非 Working）后才能收敛。
        稳定状态包括：AwaitingInput, AwaitingCompletion, Completed, Failed

        收敛后的 phase 优先级：
        1. AWAITING_INPUT - 存在需要用户输入的 Partner
        2. AWAITING_COMPLETION - 存在待确认完成的 Partner
        3. COMPLETED/FAILED - 所有 Partner 进入终态

        Returns:
            (converged, phase)
        """
        total = len(result.partner_results)
        if total == 0:
            return True, ExecutionPhase.COMPLETED

        # 计算各状态数量
        awaiting_input = len(result.awaiting_input_partners)
        awaiting_completion = len(result.awaiting_completion_partners)
        completed = len(result.completed_partners)
        failed = len(result.failed_partners)
        terminal = completed + failed

        # 计算稳定状态的 Partner 数量（非 Working 状态）
        stable_count = awaiting_input + awaiting_completion + terminal

        # 必须等待所有 Partner 都进入稳定状态
        if stable_count < total:
            # 还有 Partner 在 Working 状态，未收敛
            return False, ExecutionPhase.POLLING

        # 所有 Partner 都进入稳定状态，确定收敛后的 phase
        # 优先级: AWAITING_INPUT > AWAITING_COMPLETION > COMPLETED/FAILED
        if awaiting_input > 0:
            return True, ExecutionPhase.AWAITING_INPUT

        if awaiting_completion > 0:
            return True, ExecutionPhase.AWAITING_COMPLETION

        # 所有 Partner 进入终态
        if failed == total:
            return True, ExecutionPhase.FAILED
        return True, ExecutionPhase.COMPLETED

        # 未收敛
        return False, ExecutionPhase.POLLING

    # ------------------------------------------------------------------
    # 任务控制方法（供 Orchestrator 调用）
    # ------------------------------------------------------------------

    async def continue_task(
        self,
        session_id: str,
        aip_task_id: str,
        content: str,
        target_partner: str,
    ) -> None:
        """
        发送 continue 命令

        Args:
            session_id: 会话 ID
            aip_task_id: AIP 任务 ID
            content: 补充内容
            target_partner: 目标 Partner
        """
        await self.group_manager.continue_task(
            session_id=session_id,
            task_id=aip_task_id,
            content=content,
            target_partner=target_partner,
        )

    async def complete_task(
        self,
        session_id: str,
        aip_task_id: str,
        target_partner: str,
    ) -> None:
        """
        发送 complete 命令

        Args:
            session_id: 会话 ID
            aip_task_id: AIP 任务 ID
            target_partner: 目标 Partner
        """
        await self.group_manager.complete_task(
            session_id=session_id,
            task_id=aip_task_id,
            target_partner=target_partner,
        )

    async def cancel_task(
        self,
        session_id: str,
        aip_task_id: str,
        reason: str | None = None,
        target_partner: str | None = None,
    ) -> None:
        """
        发送 cancel 命令

        Args:
            session_id: 会话 ID
            aip_task_id: AIP 任务 ID
            reason: 取消原因
            target_partner: 目标 Partner（如果只针对一个）
        """
        await self.group_manager.cancel_task(
            session_id=session_id,
            task_id=aip_task_id,
            reason=reason,
            target_partner=target_partner,
        )

    async def complete_partner(
        self,
        session_id: str,
        partner_aic: str,
        aip_task_id: str,
        endpoint: str,
    ) -> tuple[TaskResult | None, str | None]:
        """
        完成某个 Partner 的任务（响应 AwaitingCompletion）。

        在 Group 模式下，通过 RabbitMQ 发送 COMPLETE 命令，
        不会立即获得响应，需要通过轮询获取结果。

        Args:
            session_id: 会话 ID
            partner_aic: Partner AIC
            aip_task_id: AIP 任务 ID
            endpoint: Partner 端点（Group 模式不使用）

        Returns:
            (TaskResult, None) - 返回一个表示"已完成"的模拟 TaskResult
        """
        logger.info(
            f"[GroupExecutor] complete_partner: session={session_id[:8]}..., "
            f"partner={partner_aic[:8]}..., task={aip_task_id[:8]}..."
        )
        try:
            await self.complete_task(
                session_id=session_id,
                aip_task_id=aip_task_id,
                target_partner=partner_aic,
            )
            # Group 模式下，返回一个模拟的 Completed 状态 TaskResult
            import uuid
            from datetime import datetime

            from acps_sdk.aip.aip_base_model import TaskStatus

            now_iso = datetime.now(UTC).isoformat()
            mock_result = TaskResult(
                # Message 基类必填字段
                id=str(uuid.uuid4()),
                sentAt=now_iso,
                senderRole="partner",
                senderId=partner_aic,
                sessionId=session_id,
                # TaskResult 特有字段
                taskId=aip_task_id,
                status=TaskStatus(
                    state=TaskState.Completed,
                    stateChangedAt=now_iso,
                ),
            )
            logger.info("[GroupExecutor] complete_partner sent successfully, returning mock Completed state")
            return mock_result, None
        except Exception as e:
            logger.error(f"[GroupExecutor] complete_partner failed: {e}")
            return None, str(e)

    async def cancel_partner(
        self,
        session_id: str,
        partner_aic: str,
        aip_task_id: str,
        endpoint: str,
    ) -> tuple[TaskResult | None, str | None]:
        """
        取消某个 Partner 的任务（用于结束当前任务时通知非 AwaitingCompletion 状态的 Partner）。

        在 Group 模式下，通过 RabbitMQ 发送 CANCEL 命令。

        Args:
            session_id: 会话 ID
            partner_aic: Partner AIC
            aip_task_id: AIP 任务 ID
            endpoint: Partner 端点（Group 模式不使用）

        Returns:
            (TaskResult, None) - 返回一个表示"已取消"的模拟 TaskResult
        """
        logger.info(
            f"[GroupExecutor] cancel_partner: session={session_id[:8]}..., "
            f"partner={partner_aic[:8]}..., task={aip_task_id[:8]}..."
        )
        try:
            await self.cancel_task(
                session_id=session_id,
                aip_task_id=aip_task_id,
                reason=f"Task terminated by leader for partner {partner_aic}",
                target_partner=partner_aic,
            )
            import uuid
            from datetime import datetime

            from acps_sdk.aip.aip_base_model import TaskStatus

            now_iso = datetime.now(UTC).isoformat()
            mock_result = TaskResult(
                id=str(uuid.uuid4()),
                sentAt=now_iso,
                senderRole="partner",
                senderId=partner_aic,
                sessionId=session_id,
                taskId=aip_task_id,
                status=TaskStatus(
                    state=TaskState.Canceled,
                    stateChangedAt=now_iso,
                ),
            )
            logger.info("[GroupExecutor] cancel_partner sent successfully, returning mock Canceled state")
            return mock_result, None
        except Exception as e:
            logger.error(f"[GroupExecutor] cancel_partner failed: {e}")
            return None, str(e)

    async def continue_partner(
        self,
        session_id: str,
        partner_aic: str,
        aip_task_id: str,
        endpoint: str,
        user_input: str,
    ) -> tuple[TaskResult | None, str | None]:
        """
        继续某个 Partner 的任务（响应 AwaitingInput）。

        在 Group 模式下，通过 RabbitMQ 发送 CONTINUE 命令，
        不会立即获得响应，需要通过轮询获取结果。

        Args:
            session_id: 会话 ID
            partner_aic: Partner AIC
            aip_task_id: AIP 任务 ID
            endpoint: Partner 端点（Group 模式不使用）
            user_input: 用户补充输入

        Returns:
            (TaskResult, None) - 返回一个表示"正在处理中"的模拟 TaskResult，
            让 Orchestrator 知道 CONTINUE 命令已成功发送，后续通过轮询获取真实结果
        """
        logger.info(
            f"[GroupExecutor] continue_partner: session={session_id[:8]}..., "
            f"partner={partner_aic[:8]}..., task={aip_task_id[:8]}..."
        )
        try:
            await self.continue_task(
                session_id=session_id,
                aip_task_id=aip_task_id,
                content=user_input,
                target_partner=partner_aic,
            )
            # Group 模式下，返回一个模拟的 Working 状态 TaskResult
            # 这样 Orchestrator 会认为 CONTINUE 成功，并恢复后台轮询
            import uuid
            from datetime import datetime

            from acps_sdk.aip.aip_base_model import TaskStatus

            now_iso = datetime.now(UTC).isoformat()
            mock_result = TaskResult(
                # Message 基类必填字段
                id=str(uuid.uuid4()),
                sentAt=now_iso,
                senderRole="partner",
                senderId=partner_aic,
                sessionId=session_id,
                # TaskResult 特有字段
                taskId=aip_task_id,
                status=TaskStatus(
                    state=TaskState.Working,
                    stateChangedAt=now_iso,
                ),
            )
            logger.info("[GroupExecutor] continue_partner sent successfully, returning mock Working state")
            return mock_result, None
        except Exception as e:
            logger.error(f"[GroupExecutor] continue_partner failed: {e}")
            return None, str(e)

    def clear_session_cache(self, session_id: str) -> None:
        """清除 Session 相关的缓存"""
        self._invited_partners.pop(session_id, None)


# =============================================================================
# 工厂函数
# =============================================================================


def create_group_executor(
    leader_aic: str,
    group_manager: GroupManager,
    config: GroupExecutorConfig | None = None,
    acs_cache: dict[str, dict[str, Any]] | None = None,
) -> GroupTaskExecutor:
    """
    创建群组执行器

    Args:
        leader_aic: Leader 的 AIC
        group_manager: 群组管理器
        config: 执行器配置
        acs_cache: ACS 缓存

    Returns:
        GroupTaskExecutor 实例
    """
    return GroupTaskExecutor(
        leader_aic=leader_aic,
        group_manager=group_manager,
        config=config,
        acs_cache=acs_cache,
    )
