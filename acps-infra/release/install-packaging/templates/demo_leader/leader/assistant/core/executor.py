"""
Leader Agent Platform - 任务执行器

本模块是任务执行阶段，负责：
1. 并发下发 Message(command=start) 到目标 Partner
2. 轮询获取 Partner 状态
3. 根据状态进行分支处理

当前实现 Direct RPC 模式，Group 模式后续扩展。
"""

import asyncio
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, cast

from acps_sdk.aip.aip_base_model import (
    DataItem,
    TaskResult,
    TaskState,
)
from acps_sdk.aip.aip_rpc_client import AipRpcClient

from ..models.task import (
    PartnerSelection,
    PlanningResult,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 执行配置
# =============================================================================


@dataclass
class ExecutorConfig:
 """执行器配置"""

 # 轮询配置
    poll_interval_ms: int = 2000  # 默认轮询间隔（毫秒）
    max_poll_retries: int = 3  # 最大轮询重试次数

 # 超时配置
    start_timeout_ms: int = 30000  # start 命令超时（毫秒）
    get_timeout_ms: int = 10000  # get 命令超时（毫秒）

 # 执行循环配置
    max_execution_rounds: int = 50  # 最大执行轮次（防止无限循环）
    convergence_timeout_s: int = 300  # 整体收敛超时（秒）


# =============================================================================
# 执行结果
# =============================================================================


class ExecutionPhase(str, Enum):
 """执行阶段"""

    STARTING = "starting"  # 正在下发 start
    POLLING = "polling"  # 正在轮询状态
    AWAITING_INPUT = "awaiting_input"  # 等待用户输入
    AWAITING_COMPLETION = "awaiting_completion"  # 等待确认完成
    COMPLETED = "completed"  # 全部完成
    FAILED = "failed"  # 执行失败
    TIMEOUT = "timeout"  # 超时


@dataclass
class PartnerExecutionResult:
 """单个 Partner 的执行结果"""

    partner_aic: str
    dimension_id: str
    state: TaskState
    task: TaskResult | None = None
    data_items: list[DataItem] = field(default_factory=list)
    error: str | None = None


@dataclass
class ExecutionResult:
 """整体执行结果"""

    phase: ExecutionPhase
    partner_results: dict[str, PartnerExecutionResult] = field(default_factory=dict)

 # 聚合信息
    awaiting_input_partners: list[str] = field(default_factory=list)
    awaiting_completion_partners: list[str] = field(default_factory=list)
    completed_partners: list[str] = field(default_factory=list)
    failed_partners: list[str] = field(default_factory=list)

 # 需要返回给用户的问题（来自 AwaitingInput 的 Partner）
    questions_for_user: list[DataItem] = field(default_factory=list)

 # 可用于整合的产出物
    products: dict[str, list[DataItem]] = field(default_factory=dict)

    error: str | None = None


# =============================================================================
# ACS 信息提取
# =============================================================================


def extract_partner_endpoint(acs_data: dict[str, Any]) -> str | None:
 """
    从 ACS 中提取 Partner 的 RPC 端点 URL。

    优先选择 HTTPS 端点，其次 HTTP 端点。
 """
    endpoints = acs_data.get("endPoints", [])

 # 优先选择 HTTPS 端点
    for ep in endpoints:
        url = ep.get("url", "")
        if url.startswith("https://"):
            return url

 # 其次选择 HTTP 端点
    for ep in endpoints:
        url = ep.get("url", "")
        transport = ep.get("transport", "").upper
        if url and transport in ("HTTP", "JSONRPC"):
            return url

 # 如果没有找到，尝试第一个端点
    if endpoints:
        return endpoints[0].get("url")

    return None


# =============================================================================
# 执行器
# =============================================================================


class TaskExecutor:
 """
    任务执行器 - Direct RPC 模式

    负责把 PlanningResult 中的 selectedPartners 并发下发给 Partner，
    并持续轮询状态直到收敛。
 """

    def __init__(
        self,
        leader_aic: str,
        config: ExecutorConfig | None = None,
        acs_cache: dict[str, dict[str, Any]] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        identity_binding_enabled: bool = True,
    ):
 """
        初始化执行器。

        Args:
            leader_aic: Leader 的 AIC
            config: 执行器配置
            acs_cache: ACS 缓存（partner_aic -> acs_data）
            ssl_context: 可选的 SSL 上下文，用于 mTLS 客户端连接
 """
        self.leader_aic = leader_aic
        self.config = config or ExecutorConfig
        self.acs_cache = acs_cache or {}
        self._ssl_context = ssl_context
        self._identity_binding_enabled = identity_binding_enabled

        if not self._identity_binding_enabled:
            logger.warning("AIP identity binding disabled for Leader RPC executor")

 # RPC 客户端缓存
        self._rpc_clients: dict[str, AipRpcClient] = {}

    async def execute(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
 """
        执行规划结果。

        流程：
        1. 为每个 active 维度的 Partner 创建 RPC 客户端
        2. 并发下发 Message(command=start)
        3. 轮询状态直到收敛

        Args:
            session_id: 会话 ID
            active_task_id: 活跃任务 ID
            planning_result: 规划结果
            on_poll_update: 每轮轮询后的回调函数，用于实时更新状态

        Returns:
            ExecutionResult
 """
        result = ExecutionResult(phase=ExecutionPhase.STARTING)

 # 收集所有需要执行的 Partner
        partner_tasks = self._build_partner_tasks(session_id, active_task_id, planning_result)
        self._last_partner_tasks = partner_tasks

        if not partner_tasks:
            logger.warning("No active partners to execute")
            result.phase = ExecutionPhase.COMPLETED
            return result

        logger.info(f"Starting execution for {len(partner_tasks)} partner(s)")

        from assistant.access_setup import start_trace

        start_trace

        try:
 # Phase 1: 并发下发 start
            start_results = await self._start_all_partners(session_id, partner_tasks)

 # 更新结果
            for partner_aic, (task, error) in start_results.items:
                dim_id = partner_tasks[partner_aic]["dimension_id"]
                if error:
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=TaskState.Failed,
                        error=error,
                    )
                    result.failed_partners.append(partner_aic)
                else:
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=task.status.state,
                        task=task,
                        data_items=task.status.dataItems or [],
                    )

 # 调用回调通知 start 阶段完成
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(f"on_poll_update callback failed after start: {e}")

 # Phase 2: 轮询直到收敛
            result = await self._poll_until_converged(session_id, partner_tasks, result, on_poll_update)

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            result.phase = ExecutionPhase.FAILED
            result.error = str(e)

        finally:
 # 清理 RPC 客户端
            await self._cleanup_clients

        return result

    async def poll_partners(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
 """
        从当前状态开始轮询 Partner（不发送 start）。

        用于 TASK_INPUT 处理后恢复轮询。

        Args:
            session_id: 会话 ID
            active_task_id: 活跃任务 ID
            planning_result: 规划结果
            on_poll_update: 每轮轮询后的回调函数

        Returns:
            ExecutionResult
 """
        result = ExecutionResult(phase=ExecutionPhase.POLLING)

 # 收集所有需要轮询的 Partner
        partner_tasks = self._build_partner_tasks(session_id, active_task_id, planning_result)
        self._last_partner_tasks = partner_tasks

        if not partner_tasks:
            logger.warning("No active partners to poll")
            result.phase = ExecutionPhase.COMPLETED
            return result

        logger.info(f"Resuming polling for {len(partner_tasks)} partner(s)")

        from assistant.access_setup import start_trace

        start_trace

        try:
 # 先获取一次当前状态，初始化 result
            poll_results = await self._poll_all_partners(session_id, partner_tasks)

            for partner_aic, (task, error) in poll_results.items:
                dim_id = partner_tasks[partner_aic]["dimension_id"]
                if error:
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=TaskState.Failed,
                        error=error,
                    )
                    result.failed_partners.append(partner_aic)
                elif task:
                    result.partner_results[partner_aic] = PartnerExecutionResult(
                        partner_aic=partner_aic,
                        dimension_id=dim_id,
                        state=task.status.state,
                        task=task,
                        data_items=task.status.dataItems or [],
                    )

 # 调用回调通知初始状态
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(f"on_poll_update callback failed: {e}")

 # 轮询直到收敛
            result = await self._poll_until_converged(session_id, partner_tasks, result, on_poll_update)

        except Exception as e:
            logger.error(f"Polling failed: {e}")
            result.phase = ExecutionPhase.FAILED
            result.error = str(e)

        finally:
 # 清理 RPC 客户端
            await self._cleanup_clients

        return result

    def _build_partner_tasks(
        self,
        session_id: str,
        active_task_id: str,
        planning_result: PlanningResult,
    ) -> dict[str, dict[str, Any]]:
 """
        从规划结果构建 Partner 任务列表。

        Returns:
            Dict[partner_aic, {dimension_id, aip_task_id, selection, endpoint}]
 """
        partner_tasks = {}

        for dim_id, selections in planning_result.selected_partners.items:
            for selection in selections:
                partner_aic = selection.partner_aic

 # 获取 Partner 端点
                acs_data = self.acs_cache.get(partner_aic, {})
                endpoint = extract_partner_endpoint(acs_data)

                if not endpoint:
                    logger.error(
                        f"No endpoint in ACS for Partner {partner_aic}, skipping (check ACS endPoints configuration)"
                    )
                    continue

 # 生成 AIP Task ID
                aip_task_id = f"{active_task_id}:{partner_aic}"

                if partner_aic in partner_tasks:
                    existing = partner_tasks[partner_aic]
                    existing_dimensions = existing.setdefault("dimension_ids", [existing["dimension_id"]])
                    if dim_id not in existing_dimensions:
                        existing_dimensions.append(dim_id)

                    existing_selection = cast("PartnerSelection", existing["selection"])
                    if existing_selection.instruction_text != selection.instruction_text:
                        logger.warning(
                            f"Partner {partner_aic[-8:]} selected for both "
                            f"dimension '{existing['dimension_id']}' and '{dim_id}' with "
                            "different instructions; using the latest normalized instruction"
                        )

                    existing["selection"] = selection
                    existing["endpoint"] = endpoint
                    continue

                partner_tasks[partner_aic] = {
                    "dimension_id": dim_id,
                    "dimension_ids": [dim_id],
                    "aip_task_id": aip_task_id,
                    "selection": selection,
                    "endpoint": endpoint,
                }

        return partner_tasks

    async def _get_or_create_client(self, partner_aic: str, endpoint: str) -> AipRpcClient:
 """获取或创建 RPC 客户端"""
        if partner_aic not in self._rpc_clients:
            from assistant.access_setup import (
                LEADER_ACCESS_EMITTER,
                LEADER_SERVICE_NAME,
                get_current_trace,
            )

 # 对 HTTPS 端点传入 ssl_context，HTTP 端点不传
            ctx = self._ssl_context if endpoint.startswith("https://") else None
            callee_service = self._resolve_callee_service(partner_aic)
            self._rpc_clients[partner_aic] = AipRpcClient(
                partner_url=endpoint,
                leader_id=self.leader_aic,
                ssl_context=ctx,
                expected_partner_aic=partner_aic,
                identity_binding_enabled=self._identity_binding_enabled,
                access_emitter=LEADER_ACCESS_EMITTER,
                callee_aic=partner_aic,
                caller_service=LEADER_SERVICE_NAME,
                callee_service=callee_service,
                trace_context_provider=get_current_trace,
            )
            if ctx:
                self._log_peer_cert(partner_aic, endpoint)
        return self._rpc_clients[partner_aic]

    def _resolve_callee_service(self, partner_aic: str) -> str:
 """从已构建的 partner_tasks 或 AIC 派生 callee serviceName。"""
        info = getattr(self, "_last_partner_tasks", {}).get(partner_aic)
        if info:
            selection = info.get("selection")
            skill_name = getattr(selection, "skill_name", None) if selection else None
            skill_id = getattr(selection, "skill_id", None) if selection else None
            suffix = skill_name or (skill_id.split(".")[-1] if skill_id else partner_aic[-8:])
            return f"demo-partner-{suffix}"
        return f"demo-partner-{partner_aic[-8:]}"

    def _log_peer_cert(self, partner_aic: str, endpoint: str):
 """连接 Partner HTTPS 端点，记录其服务端证书信息。"""
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(endpoint)
        host = parsed.hostname or "localhost"
        port = parsed.port or 443

        if not self._ssl_context:
            return

        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                with self._ssl_context.wrap_socket(sock, server_hostname=host) as tls:
                    peer = tls.getpeercert
                    if peer:
                        subject = dict(x[0] for x in peer.get("subject", ))
                        issuer = dict(x[0] for x in peer.get("issuer", ))
                        cn = subject.get("commonName", "N/A")
                        issuer_cn = issuer.get("commonName", "N/A")
                        not_after = peer.get("notAfter", "N/A")
                        logger.info(
                            f"[mTLS] Partner {partner_aic} cert verified: "
                            f"CN={cn}, Issuer={issuer_cn}, NotAfter={not_after}"
                        )
                    else:
                        logger.info(
                            f"[mTLS] Partner {partner_aic}: HTTPS connected, server cert not available for inspection"
                        )
        except ssl.SSLCertVerificationError as e:
            logger.warning(f"[mTLS] Partner {partner_aic} cert verification FAILED: {e}")
        except Exception as e:
            logger.warning(f"[mTLS] Could not inspect Partner {partner_aic} cert: {e}")

    async def _start_all_partners(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
    ) -> dict[str, tuple[TaskResult | None, str | None]]:
 """
        并发启动所有 Partner。

        Returns:
            Dict[partner_aic, (task, error)]
 """
        import time as time_module

        overall_start = time_module.time
        logger.debug(f"[Partner] >>> Starting {len(partner_tasks)} partner(s) concurrently...")

        async def start_one(partner_aic: str, task_info: dict[str, Any]):
            start_time = time_module.time
            short_aic = partner_aic[-8:]
            try:
                client = await self._get_or_create_client(partner_aic, task_info["endpoint"])
                selection: PartnerSelection = task_info["selection"]

 # 构建发送给 Partner 的内容
                user_input = selection.instruction_text
                if selection.instruction_data:
 # 附加结构化数据
                    user_input += f"\n\n[结构化参数]: {selection.instruction_data}"

                logger.debug(f"[Partner:{short_aic}] Sending START command to {task_info['endpoint']}")

                task = await client.start_task(
                    session_id=session_id,
                    user_input=user_input,
                    task_id=task_info["aip_task_id"],
                )

                elapsed_ms = (time_module.time - start_time) * 1000
                logger.debug(
                    f"[Partner:{short_aic}] START completed: state={task.status.state}, elapsed={elapsed_ms:.0f}ms"
                )
                return partner_aic, (task, None)

            except Exception as e:
                elapsed_ms = (time_module.time - start_time) * 1000
                logger.error(f"[Partner:{short_aic}] START failed after {elapsed_ms:.0f}ms: {e}")
                from assistant.system_setup import LEADER_SYSTEM_EMITTER

                try:
                    LEADER_SYSTEM_EMITTER.emit_sync(
                        {
                            "message": f"Partner start failed: aic={short_aic}, error={type(e).__name__}",
                            "category": "error",
                            "component": "executor",
                            "module": "start_partner",
                            "tags": {"partner_aic": partner_aic, "error_type": type(e).__name__},
                            "partner_aic": partner_aic,
                            "endpoint": task_info.get("endpoint", ""),
                            "elapsed_ms": int(elapsed_ms),
                            "error_type": type(e).__name__,
                            "error_message": str(e)[:500],
                        },
                        severity_number=17,
                        severity_text="ERROR",
                        correlation_id=session_id,
                    )
                except Exception:
                    logger.warning("System emit S-L1 failed", exc_info=True)
                return partner_aic, (None, str(e))

 # 并发执行
        tasks = [start_one(partner_aic, task_info) for partner_aic, task_info in partner_tasks.items]
        results = await asyncio.gather(*tasks)

        overall_elapsed = (time_module.time - overall_start) * 1000
        success_count = sum(1 for _, (task, err) in results if task is not None)
        logger.debug(
            f"[Partner] <<< All START commands completed in {overall_elapsed:.0f}ms, success={success_count}/{len(results)}"
        )

        return dict(results)

    async def _poll_until_converged(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
        result: ExecutionResult,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult:
 """
        轮询直到所有 Partner 收敛。

        收敛条件：
        - 所有 Partner 进入终态（Completed/Failed/Rejected/Canceled）
        - 或存在 AwaitingInput（需要用户输入）
        - 或存在 AwaitingCompletion（需要 LLM-5 决策）

        Args:
            session_id: 会话 ID
            partner_tasks: Partner 任务映射
            result: 当前执行结果
            on_poll_update: 每轮轮询后的回调函数，用于实时更新状态
 """
        import time as time_module

        result.phase = ExecutionPhase.POLLING

        start_time = datetime.now(UTC)
        polling_start = time_module.time
        timeout = timedelta(seconds=self.config.convergence_timeout_s)
        poll_interval = self.config.poll_interval_ms / 1000.0

        round_count = 0
        logger.debug(
            f"[Polling] >>> Starting polling loop, interval={poll_interval}s, timeout={self.config.convergence_timeout_s}s"
        )

        while round_count < self.config.max_execution_rounds:
            round_count += 1

 # 检查超时
            if datetime.now(UTC) - start_time > timeout:
                logger.warning(f"[Polling] Execution timeout after {round_count} rounds")
                result.phase = ExecutionPhase.TIMEOUT
                result.error = "Execution timeout"
                from assistant.system_setup import LEADER_SYSTEM_EMITTER

                try:
                    _total_elapsed = int((time_module.time - polling_start) * 1000)
                    LEADER_SYSTEM_EMITTER.emit_sync(
                        {
                            "message": f"Execution timeout: round_count={round_count}, session={session_id}",
                            "category": "capacity",
                            "component": "executor",
                            "module": "poll",
                            "tags": {"session_id": session_id[:16] if session_id else ""},
                            "round_count": round_count,
                            "elapsed_ms": _total_elapsed,
                            "convergence_timeout_s": self.config.convergence_timeout_s,
                            "session_id": session_id,
                        },
                        severity_number=13,
                        severity_text="WARN",
                        correlation_id=session_id,
                    )
                except Exception:
                    logger.warning("System emit S-L2 failed", exc_info=True)
                break

 # 等待轮询间隔
            await asyncio.sleep(poll_interval)

 # 并发获取状态
            round_start = time_module.time
            poll_results = await self._poll_all_partners(session_id, partner_tasks)
            round_elapsed = (time_module.time - round_start) * 1000

 # 更新结果
            state_summary = {}
            for partner_aic, (task, error) in poll_results.items:
                if partner_aic not in result.partner_results:
                    continue

                pr = result.partner_results[partner_aic]

                if error:
                    pr.error = error
                    continue

                if task:
                    pr.task = task
                    pr.state = task.status.state
                    pr.data_items = task.status.dataItems or []

 # 详细日志：输出轮询获取的 Task 信息
                    def _truncate(text, max_len=150):
                        if not text:
                            return "<empty>"
                        text = str(text)
                        return text[:max_len] + "..." if len(text) > max_len else text

                    products_info = "None"
                    if task.products:
                        products_info = f"{len(task.products)} product(s)"
                        for i, prod in enumerate(task.products):
                            if prod.dataItems:
                                for j, di in enumerate(prod.dataItems):
                                    text_val = getattr(di, "text", str(di))
                                    products_info += f"\n      [prod{i}.item{j}]: {_truncate(text_val)}"

                    data_items_info = "None"
                    if task.status.dataItems:
                        data_items_info = f"{len(task.status.dataItems)} item(s)"
                        for i, di in enumerate(task.status.dataItems):
                            text_val = getattr(di, "text", str(di))
                            data_items_info += f"\n      [item{i}]: {_truncate(text_val, 100)}"

                    logger.debug(
                        f"[Polling] Partner {partner_aic[-8:]} response:\n"
                        f"    state={task.status.state}\n"
                        f"    products={products_info}\n"
                        f"    dataItems={data_items_info}"
                    )

 # 统计状态
                    state_name = (
                        task.status.state.value if hasattr(task.status.state, "value") else str(task.status.state)
                    )
                    state_summary[state_name] = state_summary.get(state_name, 0) + 1

 # 每5轮或有状态变化时输出日志
            if round_count % 5 == 1 or round_count <= 3:
                total_elapsed = (time_module.time - polling_start) * 1000
                logger.debug(
                    f"[Polling] Round {round_count}: states={state_summary}, "
                    f"round_time={round_elapsed:.0f}ms, total_time={total_elapsed:.0f}ms"
                )

 # 调用轮询更新回调（如果提供）
            if on_poll_update:
                try:
                    on_poll_update(result)
                except Exception as e:
                    logger.warning(f"on_poll_update callback failed: {e}")

 # 检查收敛
            converged, phase = self._check_convergence(result)

            if converged:
                result.phase = phase
                break

            logger.debug(f"Poll round {round_count}: not yet converged")

 # 最终分类
        self._classify_results(result)

        return result

    async def _poll_all_partners(
        self,
        session_id: str,
        partner_tasks: dict[str, dict[str, Any]],
    ) -> dict[str, tuple[TaskResult | None, str | None]]:
 """并发获取所有 Partner 状态"""

        async def poll_one(partner_aic: str, task_info: dict[str, Any]):
            try:
                client = await self._get_or_create_client(partner_aic, task_info["endpoint"])

                task = await client.get_task(
                    task_id=task_info["aip_task_id"],
                    session_id=session_id,
                )

                return partner_aic, (task, None)

            except Exception as e:
                logger.warning(f"Failed to poll {partner_aic}: {e}")
                return partner_aic, (None, str(e))

        tasks = [poll_one(partner_aic, task_info) for partner_aic, task_info in partner_tasks.items]
        results = await asyncio.gather(*tasks)

        return dict(results)

    def _check_convergence(self, result: ExecutionResult) -> tuple[bool, ExecutionPhase]:
 """
        检查是否收敛。

        收敛规则：
        - Leader 需要等待所有 Partner 都进入"稳定态"后才收敛
        - 稳定态包括：AwaitingInput、AwaitingCompletion、Completed、Failed、Rejected、Canceled
        - Working、Accepted 是非稳定态，需要继续轮询

        Returns:
            (is_converged, phase)
 """
        states = [pr.state for pr in result.partner_results.values]

 # 稳定态集合（不再变化，需要外部干预才能继续）
        stable_states = {
            TaskState.AwaitingInput,
            TaskState.AwaitingCompletion,
            TaskState.Completed,
            TaskState.Failed,
            TaskState.Rejected,
            TaskState.Canceled,
        }

 # 终态集合
        terminal_states = {
            TaskState.Completed,
            TaskState.Failed,
            TaskState.Rejected,
            TaskState.Canceled,
        }

 # 检查是否所有 Partner 都进入稳定态
        if not all(s in stable_states for s in states):
 # 还有 Working/Accepted 状态，继续轮询
            return False, ExecutionPhase.POLLING

 # 所有 Partner 都进入稳定态，开始判断收敛类型
 # 优先级：AwaitingInput > AwaitingCompletion > 终态

 # 如果有任何 AwaitingInput，需要暂停等待用户
        if TaskState.AwaitingInput in states:
            return True, ExecutionPhase.AWAITING_INPUT

 # 如果有任何 AwaitingCompletion，需要 LLM-5 决策
        if TaskState.AwaitingCompletion in states:
            return True, ExecutionPhase.AWAITING_COMPLETION

 # 如果所有 Partner 都进入终态
        if all(s in terminal_states for s in states):
 # 检查是否全部成功
            if all(s == TaskState.Completed for s in states):
                return True, ExecutionPhase.COMPLETED
            return True, ExecutionPhase.FAILED

 # 理论上不应该到达这里
        return False, ExecutionPhase.POLLING

    def _classify_results(self, result: ExecutionResult):
 """对结果进行分类汇总"""
        result.awaiting_input_partners = []
        result.awaiting_completion_partners = []
        result.completed_partners = []
        result.failed_partners = []
        result.questions_for_user = []
        result.products = {}

        for partner_aic, pr in result.partner_results.items:
            if pr.state == TaskState.AwaitingInput:
                result.awaiting_input_partners.append(partner_aic)
 # 收集需要用户回答的问题
                result.questions_for_user.extend(pr.data_items)

            elif pr.state == TaskState.AwaitingCompletion:
                result.awaiting_completion_partners.append(partner_aic)
 # 收集产出物
                result.products[partner_aic] = pr.data_items

            elif pr.state == TaskState.Completed:
                result.completed_partners.append(partner_aic)
 # 收集产出物
                if pr.task and pr.task.products:
                    for product in pr.task.products:
                        result.products.setdefault(partner_aic, []).extend(product.dataItems)

            elif pr.state in (TaskState.Failed, TaskState.Rejected, TaskState.Canceled):
                result.failed_partners.append(partner_aic)

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

        Returns:
            (task, error)
 """
        try:
            client = await self._get_or_create_client(partner_aic, endpoint)
            task = await client.continue_task(
                task_id=aip_task_id,
                session_id=session_id,
                user_input=user_input,
            )
            return task, None
        except Exception as e:
            logger.error(f"Failed to continue task for {partner_aic}: {e}")
            return None, str(e)

    async def complete_partner(
        self,
        session_id: str,
        partner_aic: str,
        aip_task_id: str,
        endpoint: str,
    ) -> tuple[TaskResult | None, str | None]:
 """
        完成某个 Partner 的任务（响应 AwaitingCompletion）。

        Returns:
            (task, error)
 """
        try:
            client = await self._get_or_create_client(partner_aic, endpoint)
            task = await client.complete_task(
                task_id=aip_task_id,
                session_id=session_id,
            )
            return task, None
        except Exception as e:
            logger.error(f"Failed to complete task for {partner_aic}: {e}")
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

        Returns:
            (task, error)
 """
        try:
            client = await self._get_or_create_client(partner_aic, endpoint)
            task = await client.cancel_task(
                task_id=aip_task_id,
                session_id=session_id,
            )
            return task, None
        except Exception as e:
            logger.error(f"Failed to cancel task for {partner_aic}: {e}")
            return None, str(e)

    async def _cleanup_clients(self):
 """清理所有 RPC 客户端"""
        for client in self._rpc_clients.values:
            try:
                await client.close
            except Exception as e:
                logger.warning(f"Error closing RPC client: {e}")
        self._rpc_clients.clear
