"""
Leader · AipExecutor

TaskExecutor 的扩展版：execute 入口按 Partner ACS capabilities 自动路由：
- streaming=true                        → StreamExecutor.run（SSE 推流，等待终态）
- notification=true + notification_exec → NotificationExecutor.start_for_partner
                                          + asyncio.wait_for(future, timeout)
- 其他（或 notification 无 exec 配置）  → TaskExecutor RPC 轮询（向后兼容）

集成方法：在 Orchestrator.start 中将 TaskExecutor 替换为 AipExecutor，
并通过 notification_executor= 参数传入共享的 NotificationExecutor 实例。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from acps_sdk.aip.aip_base_model import TaskResult, TaskState

from ..models.task import PlanningResult
from .executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult, TaskExecutor
from .executor_selector import ExecutorStrategy, select_executor_strategy
from .stream_event_bus import get_stream_event_bus
from .stream_executor import StreamExecutor

if TYPE_CHECKING:
    from .notification_executor import NotificationExecutor

logger = logging.getLogger(__name__)

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.Completed, TaskState.Canceled, TaskState.Failed, TaskState.Rejected}
)

_DEFAULT_NOTIFICATION_TIMEOUT_S = 300.0  # 5 分钟


def _base_url_from_endpoint(endpoint: str) -> str:
 """从 ACS endPoint URL 提取 base URL（去掉末尾路径）。

    ACS endPoints 通常直接给出 http://host:port 形式的 base URL，
    此函数做保险处理：去掉最后一段路径（如 /rpc），返回 base。
 """
 # 若 endpoint 结尾只有 host:port（无额外路径），直接返回
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return endpoint.rstrip("/")
 # 若有路径，去掉最后一段
    parent_path = path.rsplit("/", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{parent_path}"


def _narrow_planning_result(planning_result: PlanningResult, keep_aics: set[str]) -> PlanningResult:
 """构造只保留指定 partner_aic 的 PlanningResult 副本（用于 RPC 子集执行）。"""
    narrowed_partners: dict[str, list] = {}
    for dim_id, selections in planning_result.selected_partners.items:
        filtered = [s for s in selections if s.partner_aic in keep_aics]
        if filtered:
            narrowed_partners[dim_id] = filtered

    return PlanningResult(
        scenario_id=planning_result.scenario_id,
        user_query=planning_result.user_query,
        selected_partners=narrowed_partners,
    )


def _merge_execution_result(dest: ExecutionResult, src: ExecutionResult) -> None:
 """将 src 的字段合并进 dest（覆盖 partner_results，追加列表）。"""
    for aic, per in src.partner_results.items:
        dest.partner_results[aic] = per
    dest.completed_partners.extend(src.completed_partners)
    dest.failed_partners.extend(src.failed_partners)
    dest.awaiting_input_partners.extend(src.awaiting_input_partners)
    dest.awaiting_completion_partners.extend(src.awaiting_completion_partners)
    dest.questions_for_user.extend(src.questions_for_user)
    dest.products.update(src.products)


async def _noop_event_handler(event: Any) -> None:
 """空事件处理器（on_event 占位，避免 None 调用错误）。"""


class AipExecutor(TaskExecutor):
 """TaskExecutor 的能力感知扩展。

    在 execute 入口，按每个 Partner 的 ACS capabilities 选择执行路径：
    - streaming=true:   StreamExecutor.run（SSE 驱动，等待终态）
    - notification=true + notification_executor:
                        NotificationExecutor.start_for_partner + asyncio.wait_for(future)
    - 其他:             TaskExecutor.execute（RPC 轮询，向后兼容）

    Args:
        notification_executor: 共享的 NotificationExecutor 实例（由 Orchestrator 注入）。
            未提供时 notification partners 回退 RPC。
        notification_wait_timeout_s: notification 模式下等待终态回调的最大秒数。
 """

    def __init__(
        self,
        leader_aic: str,
        config=None,
        acs_cache=None,
        ssl_context=None,
        *,
        callback_base_url: str | None = None,
        notification_executor: NotificationExecutor | None = None,
        notification_wait_timeout_s: float = _DEFAULT_NOTIFICATION_TIMEOUT_S,
        identity_binding_enabled: bool = True,
    ):
        super.__init__(
            leader_aic=leader_aic,
            config=config,
            acs_cache=acs_cache or {},
            ssl_context=ssl_context,
            identity_binding_enabled=identity_binding_enabled,
        )
        self._callback_base_url = callback_base_url
        self._notification_executor = notification_executor
        self._notification_wait_timeout_s = notification_wait_timeout_s

    async def execute(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
 """按 Partner ACS 能力位并发执行，合并结果。"""
        partner_tasks = self._build_partner_tasks(session_id, active_task_id, planning_result)

        if not partner_tasks:
            logger.warning("[AipExecutor] No active partners to execute")
            return ExecutionResult(phase=ExecutionPhase.COMPLETED)

 # 按策略分类
        stream_tasks: dict[str, dict[str, Any]] = {}
        notification_tasks: dict[str, dict[str, Any]] = {}
        rpc_tasks: dict[str, dict[str, Any]] = {}

        for aic, task_info in partner_tasks.items:
            acs_data = self.acs_cache.get(aic, {})
            strategy = select_executor_strategy(acs_data)
            if strategy == ExecutorStrategy.STREAM:
                stream_tasks[aic] = task_info
            elif strategy == ExecutorStrategy.NOTIFICATION and self._notification_executor:
                notification_tasks[aic] = task_info
            else:
                if strategy == ExecutorStrategy.NOTIFICATION and not self._notification_executor:
                    logger.warning(
                        "[AipExecutor] Partner %s supports notification but no "
                        "notification_executor configured; falling back to RPC.",
                        aic,
                    )
                rpc_tasks[aic] = task_info

        result = ExecutionResult(phase=ExecutionPhase.STARTING)

 # 并发执行 streaming + notification partners（均为 await 直到终态）
        async_tasks = []
        if stream_tasks:
            async_tasks.append(self._execute_streaming_partners(session_id, stream_tasks))
        if notification_tasks:
            async_tasks.append(self._execute_notification_partners(session_id, notification_tasks))

        if async_tasks:
            async_results = await asyncio.gather(*async_tasks)
            for exec_r in async_results:
                _merge_execution_result(result, exec_r)
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as exc:
                    logger.warning("[AipExecutor] on_poll_update raised: %s", str(exc))

 # RPC partners 走父类 execute（含 start + poll 完整流程）
        if rpc_tasks:
            narrowed = _narrow_planning_result(planning_result, set(rpc_tasks.keys))
            rpc_result = await super.execute(session_id, active_task_id, narrowed, on_poll_update)
            _merge_execution_result(result, rpc_result)
        elif stream_tasks or notification_tasks:
            result.phase = ExecutionPhase.COMPLETED

        self._classify_results(result)
        converged, phase = self._check_convergence(result)
        if converged:
            result.phase = phase

        logger.info(
            "[AipExecutor] execute done: phase=%s stream=%d notif=%d rpc=%d",
            result.phase,
            len(stream_tasks),
            len(notification_tasks),
            len(rpc_tasks),
        )
        return result

    async def _execute_notification_partners(
        self,
        session_id: str,
        notification_tasks: dict[str, dict[str, Any]],
    ) -> ExecutionResult:
 """并发为所有 notification partners 启动订阅并等待终态回调。"""
        tasks = [
            self._execute_one_notification_partner(session_id, aic, info) for aic, info in notification_tasks.items
        ]
        pairs: list[tuple[str, PartnerExecutionResult]] = list(await asyncio.gather(*tasks))

        exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
        for aic, per in pairs:
            exec_result.partner_results[aic] = per
            if per.state == TaskState.Completed:
                exec_result.completed_partners.append(aic)
                if per.data_items:
                    exec_result.products[aic] = per.data_items
            elif per.state in _TERMINAL_STATES:
                exec_result.failed_partners.append(aic)
            else:
                exec_result.failed_partners.append(aic)
        return exec_result

    async def _execute_one_notification_partner(
        self,
        session_id: str,
        aic: str,
        task_info: dict[str, Any],
    ) -> tuple[str, PartnerExecutionResult]:
 """为单个 notification partner 启动任务并 await 终态 Future。"""
        assert self._notification_executor is not None  # 已在路由时过滤

        endpoint = task_info["endpoint"]
        base_url = _base_url_from_endpoint(endpoint)
        task_id = task_info["aip_task_id"]
        selection = task_info["selection"]
        dim_id = task_info["dimension_id"]

        user_input = selection.instruction_text
        if selection.instruction_data:
            user_input += f"\n\n[结构化参数]: {selection.instruction_data}"

 # 先注册 future，再 start（避免极小概率回调比 start 先到）
        terminal_future = self._notification_executor.register_task_future(task_id)

        try:
            logger.info(
                "[AipExecutor] NotificationExecutor starting: partner=%s base_url=%s",
                aic[-8:],
                base_url,
            )
            await self._notification_executor.start_for_partner(
                partner_base_url=base_url,
                partner_aic=aic,
                session_id=session_id,
                user_input=user_input,
                task_id=task_id,
            )

 # 等待 Partner 推送终态回调
            final: TaskResult = await asyncio.wait_for(
                terminal_future,
                timeout=self._notification_wait_timeout_s,
            )
            logger.info(
                "[AipExecutor] NotificationExecutor done: partner=%s state=%s",
                aic[-8:],
                final.status.state,
            )
            return aic, PartnerExecutionResult(
                partner_aic=aic,
                dimension_id=dim_id,
                state=final.status.state,
                task=final,
                data_items=final.status.dataItems or [],
            )
        except TimeoutError:
            logger.error(
                "[AipExecutor] NotificationExecutor timed out after %.0fs: partner=%s",
                self._notification_wait_timeout_s,
                aic[-8:],
            )
            self._notification_executor.cancel_task_future(task_id)
            return aic, PartnerExecutionResult(
                partner_aic=aic,
                dimension_id=dim_id,
                state=TaskState.Failed,
                error=f"Notification callback timed out after {self._notification_wait_timeout_s}s",
            )
        except Exception as exc:
            logger.error(
                "[AipExecutor] NotificationExecutor exception: partner=%s error=%s",
                aic[-8:],
                str(exc),
            )
            self._notification_executor.cancel_task_future(task_id)
            return aic, PartnerExecutionResult(
                partner_aic=aic,
                dimension_id=dim_id,
                state=TaskState.Failed,
                error=str(exc),
            )

    async def _execute_streaming_partners(
        self,
        session_id: str,
        stream_tasks: dict[str, dict[str, Any]],
    ) -> ExecutionResult:
 """并发为所有 streaming partners 运行 StreamExecutor.run。"""
        tasks = [self._execute_one_stream_partner(session_id, aic, info) for aic, info in stream_tasks.items]
        pairs: list[tuple[str, PartnerExecutionResult]] = list(await asyncio.gather(*tasks))

        exec_result = ExecutionResult(phase=ExecutionPhase.COMPLETED)
        for aic, per in pairs:
            exec_result.partner_results[aic] = per
            state = per.state
            if state == TaskState.Completed:
                exec_result.completed_partners.append(aic)
                if per.data_items:
                    exec_result.products[aic] = per.data_items
            elif state == TaskState.AwaitingInput:
                exec_result.awaiting_input_partners.append(aic)
                exec_result.questions_for_user.extend(per.data_items)
            elif state == TaskState.AwaitingCompletion:
                exec_result.awaiting_completion_partners.append(aic)
                if per.data_items:
                    exec_result.products[aic] = per.data_items
            elif state in _TERMINAL_STATES:
                exec_result.failed_partners.append(aic)
            else:
                exec_result.failed_partners.append(aic)

        return exec_result

    async def _execute_one_stream_partner(
        self,
        session_id: str,
        aic: str,
        task_info: dict[str, Any],
    ) -> tuple[str, PartnerExecutionResult]:
 """为单个 streaming partner 运行 StreamExecutor，返回 (aic, PartnerExecutionResult)。"""
        endpoint = task_info["endpoint"]
        base_url = _base_url_from_endpoint(endpoint)
        task_id = task_info["aip_task_id"]
        selection = task_info["selection"]
        dim_id = task_info["dimension_id"]

        user_input = selection.instruction_text
        if selection.instruction_data:
            user_input += f"\n\n[结构化参数]: {selection.instruction_data}"

        executor = StreamExecutor(
            partner_base_url=base_url,
            leader_id=self.leader_aic,
            ssl_context=self._ssl_context,
            expected_partner_aic=aic,
            identity_binding_enabled=self._identity_binding_enabled,
        )
 # 将 SSE 事件推送给会话级事件总线（供 Leader API SSE 端点消费）
        bus = get_stream_event_bus

        async def _on_stream_event(event: Any) -> None:
            bus.push(session_id, event)

        try:
            logger.info(
                "[AipExecutor] StreamExecutor starting: partner=%s base_url=%s",
                aic[-8:],
                base_url,
            )
            final: TaskResult | None = await executor.run(
                session_id=session_id,
                user_input=user_input,
                task_id=task_id,
                on_event=_on_stream_event,
            )
            if final is not None:
                logger.info(
                    "[AipExecutor] StreamExecutor done: partner=%s state=%s",
                    aic[-8:],
                    final.status.state,
                )
                return aic, PartnerExecutionResult(
                    partner_aic=aic,
                    dimension_id=dim_id,
                    state=final.status.state,
                    task=final,
                    data_items=final.status.dataItems or [],
                )
            logger.warning(
                "[AipExecutor] StreamExecutor returned no terminal result: partner=%s",
                aic[-8:],
            )
            return aic, PartnerExecutionResult(
                partner_aic=aic,
                dimension_id=dim_id,
                state=TaskState.Failed,
                error="No terminal event received from SSE stream",
            )
        except Exception as exc:
            logger.error(
                "[AipExecutor] StreamExecutor exception: partner=%s error=%s",
                aic[-8:],
                str(exc),
            )
            return aic, PartnerExecutionResult(
                partner_aic=aic,
                dimension_id=dim_id,
                state=TaskState.Failed,
                error=str(exc),
            )
        finally:
            await executor.close
 # 通知订阅者该 partner 的流已结束（哨兵 None）
            bus.push(session_id, None)
