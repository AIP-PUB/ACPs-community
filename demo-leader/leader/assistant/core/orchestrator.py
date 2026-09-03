"""
Leader Agent Platform - Orchestrator

本模块实现主编排器，负责处理 /submit 请求的完整流程：
1. Session 管理（创建/获取）
2. 意图分析（LLM-1）
3. 任务调度（LLM-2 到 LLM-6，当前版本 stub）
4. 响应组装

注意：当前版本只实现 LLM-1 相关功能，其他 LLM 调用点为 stub。
"""

import json
import logging
import ssl
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acps_sdk.amp import AuditAction, AuditActor, AuditBody, AuditResult, AuditTarget
from acps_sdk.oidc import HumanPrincipal, audit_actor_from_principal

from leader.assistant.amp_setup import LEADER_AIC, LEADER_EMITTER
from leader.runtime_paths import resolve_leader_dir

from ..api.schemas import (
    CommonError,
    SubmitRequest,
    SubmitResponse,
    SubmitResult,
)
from ..models import (
    ActiveTask,
    ActiveTaskStatus,
    EventLogType,
    ExecutionMode,
    IntentDecision,
    IntentType,
    ResponseType,
    ScenarioRuntime,
    Session,
    SessionId,
    UserResultType,
    generate_active_task_id,
    now_iso,
)
from ..models.clarification import (
    ClarificationMergeInput,
    MergedClarification,
    PartnerClarificationItem,
    extract_clarification_from_task_status,
)
from ..models.exceptions import (
    ActiveTaskMismatchError,
    DuplicateRequestError,
    ModeMismatchError,
    SessionClosedError,
    SessionNotFoundError,
)
from ..models.input_routing import (
    InputRoutingRequest,
    InputRoutingResult,
    PartnerGapInfo,
)
from ..security import bind_session_principal
from ..services.scenario_loader import ScenarioLoader
from .aggregator import AggregationResult, Aggregator, DegradationInfo, PartnerOutput
from .background_executor import BackgroundExecutor, get_background_executor
from .clarification_merger import ClarificationMerger, get_clarification_merger
from .completion_gate import (
    CompletionGate,
)
from .completion_gate_handler import (
    handle_awaiting_completion_with_loop,
    resolve_partner_endpoint,
)
from .executor import ExecutionPhase, ExecutionResult, ExecutorConfig, TaskExecutor
from .history_compressor import HistoryCompressor, get_history_compressor
from .input_router import InputRouter, get_input_router
from .intent_analyzer import IntentAnalyzer
from .planner import Planner
from .session_manager import SessionManager
from .task_execution_manager import TaskExecutionManager, get_task_execution_manager

if TYPE_CHECKING:
    from .group_executor import GroupTaskExecutor
    from .group_manager import GroupManager

logger = logging.getLogger(__name__)


def _build_client_ssl_context(
    settings: dict[str, Any],
) -> ssl.SSLContext | None:
    """
    根据 config.toml 中 [mtls] 配置构建客户端 SSL 上下文。

    如果证书文件不存在或加载失败，返回 None（降级为无客户端证书连接）。
    """
    mtls_cfg = settings.get("mtls", {})
    if not mtls_cfg:
        logger.info("No [mtls] config found, client certs disabled")
        return None

    cert_file = mtls_cfg.get("cert_file", "")
    key_file = mtls_cfg.get("key_file", "")
    ca_file = mtls_cfg.get("ca_file", "")

    if not cert_file or not key_file or not ca_file:
        logger.info("Incomplete [mtls] config, client certs disabled")
        return None

    # 基于 leader/ 目录解析相对路径
    leader_dir = resolve_leader_dir()
    cert_path = leader_dir / cert_file
    key_path = leader_dir / key_file
    ca_path = leader_dir / ca_file

    for p, desc in [
        (cert_path, "cert_file"),
        (key_path, "key_file"),
        (ca_path, "ca_file"),
    ]:
        if not p.is_file():
            logger.warning(f"mTLS {desc} not found: {p}, client certs disabled")
            return None

    try:
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=str(ca_path),
        )
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        logger.info(f"mTLS client SSL context created: cert={cert_path.name}, ca={ca_path.name}")
        return ctx
    except Exception as e:
        logger.warning(f"Failed to load mTLS client certs: {e}, client certs disabled")
        return None


class Orchestrator:
    """
    主编排器。

    协调各组件处理用户请求，是业务逻辑的核心入口点。

    支持两种初始化方式：
    1. 无参数构造 + start()/stop() 生命周期方法（用于 FastAPI lifespan）
    2. 依赖注入构造（用于测试和自定义场景）
    """

    def __init__(
        self,
        session_manager: SessionManager | None = None,
        scenario_loader: ScenarioLoader | None = None,
        intent_analyzer: IntentAnalyzer | None = None,
        planner: Planner | None = None,
        executor: TaskExecutor | None = None,
        group_executor: GroupTaskExecutor | None = None,
        group_manager: GroupManager | None = None,
        clarification_merger: ClarificationMerger | None = None,
        input_router: InputRouter | None = None,
        completion_gate: CompletionGate | None = None,
        aggregator: Aggregator | None = None,
        history_compressor: HistoryCompressor | None = None,
        task_execution_manager: TaskExecutionManager | None = None,
        background_executor: BackgroundExecutor | None = None,
        leader_aic: str | None = None,
        async_execution: bool = True,
        identity_binding_enabled: bool | None = None,
    ):
        """
        初始化编排器。

        如果不传入依赖，将在 start() 中自动创建。

        Args:
            session_manager: Session 管理器
            scenario_loader: 场景加载器
            intent_analyzer: 意图分析器（LLM-1）
            planner: 全量规划器（LLM-2），可选
            executor: 任务执行器（Direct RPC），可选
            group_executor: 群组任务执行器（Group Mode），可选
            group_manager: 群组管理器（Group Mode），可选
            clarification_merger: 反问合并器（LLM-3），可选
            input_router: 输入路由器（LLM-4），可选
            completion_gate: 完成闸门（LLM-5），可选
            aggregator: 结果整合器（LLM-6），可选
            history_compressor: 历史压缩器（LLM-7），可选
            task_execution_manager: 任务执行管理器，可选
            background_executor: 后台执行器，可选
            leader_aic: Leader 的 AIC，用于创建 executor
            async_execution: 是否启用异步执行模式（默认 True）
        """
        self._session_manager = session_manager
        self._scenario_loader = scenario_loader
        self._intent_analyzer = intent_analyzer
        self._planner = planner
        self._executor = executor
        self._group_executor = group_executor
        self._group_manager = group_manager
        self._clarification_merger = clarification_merger
        self._input_router = input_router
        self._completion_gate = completion_gate
        self._aggregator = aggregator
        self._history_compressor = history_compressor
        self._task_execution_manager = task_execution_manager
        # 共享 NotificationExecutor 实例（callback_base_url 配置时创建）
        self._notification_executor = None
        self._background_executor = background_executor
        self._leader_aic = leader_aic
        self._async_execution = async_execution
        self._identity_binding_enabled = identity_binding_enabled
        self._started = False
        # 用于防止历史压缩竞态的锁
        self._compression_locks: dict = {}

    @property
    def notification_executor(self):
        """返回共享 NotificationExecutor 实例（未配置 callback_base_url 时为 None）。"""
        return self._notification_executor

    async def start(self):
        """
        启动编排器（用于 FastAPI lifespan）。

        自动创建未提供的依赖组件。
        """
        if self._started:
            return

        from ..config import settings
        from ..llm.client import get_llm_client
        from ..services.scenario_loader import get_scenario_loader

        if self._identity_binding_enabled is None:
            self._identity_binding_enabled = bool(settings.get("app", {}).get("identity_binding_enabled", True))

        # 获取 leader_aic
        if not self._leader_aic:
            self._leader_aic = settings.get("app", {}).get("leader_aic", "unknown-leader")

        # 创建 ScenarioLoader
        if not self._scenario_loader:
            self._scenario_loader = get_scenario_loader()

        # 创建 SessionManager
        if not self._session_manager:
            self._session_manager = SessionManager(scenario_loader=self._scenario_loader)

        # 创建 IntentAnalyzer
        if not self._intent_analyzer:
            llm_client = get_llm_client("default")
            self._intent_analyzer = IntentAnalyzer(
                llm_client=llm_client,
                scenario_loader=self._scenario_loader,
            )

        # 创建 Planner
        if not self._planner:
            from .planner import get_planner

            self._planner = get_planner(scenario_loader=self._scenario_loader)

        # 创建 Executor（AipExecutor 按 Partner ACS 能力位路由到 Stream / RPC / Notification）
        if not self._executor:
            ssl_context = _build_client_ssl_context(settings)
            callback_base_url = settings.get("app", {}).get("callback_base_url") or None

            # 若配置了 callback_base_url，创建共享 NotificationExecutor 并供 AipExecutor 使用
            if callback_base_url and not self._notification_executor:
                from .notification_executor import NotificationExecutor

                leader_aic = settings.get("app", {}).get("leader_aic", "unknown-leader")
                self._notification_executor = NotificationExecutor(
                    partner_base_url="http://placeholder",  # per-task 时用 start_for_partner 覆盖
                    leader_id=leader_aic,
                    callback_base_url=callback_base_url,
                    ssl_context=ssl_context,
                    identity_binding_enabled=self._identity_binding_enabled,
                )
                logger.info("NotificationExecutor created with callback_base_url=%s", callback_base_url)

            from .aip_executor import AipExecutor

            self._executor = AipExecutor(
                leader_aic=self._leader_aic,
                config=ExecutorConfig(),
                ssl_context=ssl_context,
                callback_base_url=callback_base_url,
                notification_executor=self._notification_executor,
                identity_binding_enabled=self._identity_binding_enabled,
            )

        # 创建 ClarificationMerger (LLM-3)
        if not self._clarification_merger:
            self._clarification_merger = get_clarification_merger()

        # 创建 InputRouter (LLM-4)
        if not self._input_router:
            self._input_router = get_input_router(
                scenario_loader=self._scenario_loader,
            )

        # 创建 CompletionGate (LLM-5)
        if not self._completion_gate:
            from .completion_gate import get_completion_gate

            self._completion_gate = get_completion_gate(
                scenario_loader=self._scenario_loader,
            )

        # 创建 Aggregator (LLM-6)
        if not self._aggregator:
            from .aggregator import get_aggregator

            self._aggregator = get_aggregator(
                scenario_loader=self._scenario_loader,
            )

        # 创建 HistoryCompressor (LLM-7)
        if not self._history_compressor:
            self._history_compressor = get_history_compressor(
                scenario_loader=self._scenario_loader,
            )

        # 创建 TaskExecutionManager（异步执行模式）
        if not self._task_execution_manager:
            self._task_execution_manager = get_task_execution_manager()
            await self._task_execution_manager.start()

        # 创建 BackgroundExecutor（异步执行模式）
        if not self._background_executor:
            self._background_executor = get_background_executor()
            self._background_executor.set_components(
                executor=self._executor,
                group_executor=self._group_executor,
                aggregator=self._aggregator,
                clarification_merger=self._clarification_merger,
                completion_gate=self._completion_gate,
                session_manager=self._session_manager,
                planner=self._planner,
            )
            # 设置回调函数以复用 orchestrator 的执行逻辑
            self._background_executor.set_callbacks(
                execute_planning=self._execute_planning,
                build_execution_response=self._build_execution_response,
            )

        self._started = True
        logger.info(f"Orchestrator started with leader_aic={self._leader_aic}, async_execution={self._async_execution}")

    # =========================================================================
    # 辅助方法：构建 SubmitResponse
    # =========================================================================

    def _build_submit_response(
        self,
        session: Session,
        active_task_id: str | None = None,
    ) -> SubmitResponse:
        """
        构建 SubmitResponse。

        Args:
            session: 当前会话
            active_task_id: 活跃任务 ID（可选，默认从 session 获取）

        Returns:
            SubmitResponse（包含 result 字段）
        """
        from ..models import ActiveTaskStatus

        # 获取活跃任务信息
        task_id = active_task_id
        external_status = ActiveTaskStatus.PENDING

        if session.active_task:
            task_id = task_id or session.active_task.active_task_id
            external_status = session.active_task.external_status

        # 如果没有活跃任务，生成一个临时 ID
        if not task_id:
            task_id = generate_active_task_id()

        return SubmitResponse(
            result=SubmitResult(
                session_id=session.session_id,
                mode=session.mode or ExecutionMode.DIRECT_RPC,
                active_task_id=task_id,
                accepted_at=now_iso(),
                external_status=external_status,
            )
        )

    # =========================================================================
    # 辅助方法：场景信息提取
    # =========================================================================

    def _get_scenario_id(self, session: Session) -> str | None:
        """获取当前场景 ID（expert 场景 ID 或 None）。"""
        return session.expert_scenario.id if session.expert_scenario else None

    def _get_scenario_kind(self, session: Session) -> str:
        """获取当前场景类型（expert 或 base）。"""
        return session.expert_scenario.kind if session.expert_scenario else "base"

    def _get_executor_for_session(self, session: Session) -> TaskExecutor | None:
        """
        根据 Session 的执行模式获取对应的执行器。

        Args:
            session: 会话对象

        Returns:
            对应的执行器（TaskExecutor 或 GroupTaskExecutor）
        """
        if session.mode == ExecutionMode.GROUP:
            if self._group_executor:
                return self._group_executor
            logger.warning(
                f"Session {session.session_id} is in GROUP mode but no "
                "group_executor available, falling back to direct executor"
            )
        return self._executor

    def _sync_group_session_state(self, session: Session) -> None:
        """将 GroupManager 运行态 group_id 回写到 Session。"""
        if session.mode != ExecutionMode.GROUP or not self._group_manager:
            return

        group_id = self._group_manager.get_group_id(session.session_id)
        if not group_id or session.group_id == group_id:
            return

        session.group_id = group_id
        short_session = session.session_id[-8:] if len(session.session_id) > 8 else session.session_id
        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        logger.debug(
            "[Orchestrator:%s] Synced session group_id=...%s",
            short_session,
            short_group,
        )

    async def stop(self):
        """
        停止编排器（用于 FastAPI lifespan）。
        """
        if not self._started:
            return

        # 清理资源
        if self._task_execution_manager:
            await self._task_execution_manager.stop()

        if self._executor:
            await self._executor._cleanup_clients()

        # 清理群组管理器
        if self._group_manager:
            await self._group_manager.stop()

        self._started = False
        logger.info("Orchestrator stopped")

    async def handle_submit(
        self,
        request: SubmitRequest,
        *,
        principal: HumanPrincipal | None = None,
    ) -> SubmitResponse:
        """
        处理 /submit 请求。

        完整流程：
        1. 获取或创建 Session（含 mode/幂等性/activeTaskId 校验）
        2. 调用 LLM-1 分析意图
        3. 根据意图类型执行对应处理
        4. 组装响应

        Args:
            request: 提交请求

        Returns:
            SubmitResponse

        Raises:
            SessionNotFoundError: Session 不存在
            SessionExpiredError: Session 已过期
            ModeMismatchError: Session mode 与请求不一致
            ActiveTaskMismatchError: activeTaskId 与当前不一致
            DuplicateRequestError: 重复请求但载荷不一致
        """
        import time

        request_start_time = time.time()
        logger.debug(f"[Orchestrator] >>> handle_submit started, query='{request.query[:50]}...'")

        # 计算请求载荷哈希（用于幂等性校验）
        request_hash = self._compute_request_hash(request)

        # 1. 获取或创建 Session（含校验）
        session, is_new_session = self._get_or_create_session_with_validation(
            session_id=request.session_id,
            mode=request.mode,
            client_request_id=request.client_request_id,
            request_hash=request_hash,
            active_task_id=request.active_task_id,
            principal=principal,
        )
        session_id = session.session_id
        logger.debug(f"[Orchestrator] Session: {session_id[:16]}, new={is_new_session}")
        self._ensure_session_scenario_metadata(session)

        if principal is not None:
            bind_session_principal(session, principal)

        # 2. 记录事件日志（用户输入作为事件记录，对话轮次在最终响应时记录）
        self._session_manager.add_event_log(
            session_id=session_id,
            event_type=EventLogType.USER_SUBMIT,
            payload={
                "query": request.query,
                **({"principal_id": principal.principal_id} if principal is not None else {}),
            },
        )

        # 4. 获取当前活跃任务
        active_task = self._get_active_task(session)

        # 5. 调用 LLM-1 分析意图
        llm1_start = time.time()
        logger.debug("[Orchestrator] >>> Starting LLM-1 intent analysis...")

        intent_decision = await self._intent_analyzer.analyze(
            user_query=request.query,
            session=session,
            active_task=active_task,
        )

        llm1_elapsed = (time.time() - llm1_start) * 1000
        logger.debug(
            f"[Orchestrator] <<< LLM-1 completed in {llm1_elapsed:.0f}ms, intent={intent_decision.intent_type.value}"
        )

        # A1: 审计——用户提交请求，意图分析完成
        try:
            actor = (
                audit_actor_from_principal(principal)
                if principal is not None
                else AuditActor(
                    id=request.user_id or "anonymous",
                    type="human",
                    name=request.user_id,
                )
            )
            await LEADER_EMITTER.emit(
                AuditBody(
                    actor=actor,
                    action=AuditAction(name="submit_request", type="task_management"),
                    target=AuditTarget(type="session", id=session.session_id),
                    result=AuditResult(
                        status="success",
                        reason=f"intent={intent_decision.intent_type.value}",
                    ),
                ),
                trace_id=session.session_id,
                correlation_id=active_task.active_task_id if active_task else None,
            )
        except Exception:
            logger.warning("Audit emit A1 failed", exc_info=True)

        # 6. 记录意图决策
        self._session_manager.add_event_log(
            session_id=session_id,
            event_type=EventLogType.INTENT_DECISION,
            payload={
                "intent_type": intent_decision.intent_type.value,
                "target_scenario": intent_decision.target_scenario,
            },
        )

        # 7. 根据意图类型处理
        handle_start = time.time()
        response = await self._handle_intent(
            session=session,
            intent=intent_decision,
            user_query=request.query,
        )
        handle_elapsed = (time.time() - handle_start) * 1000
        logger.debug(f"[Orchestrator] Intent handling completed in {handle_elapsed:.0f}ms")

        # 8. 更新 Session
        self._session_manager.update_session(session)

        # 9. 缓存请求以支持幂等性（仅当提供了 client_request_id）
        if request.client_request_id:
            self._session_manager.cache_request(
                session_id=session_id,
                client_request_id=request.client_request_id,
                request_hash=request_hash,
            )

        # 10. 检查是否需要历史压缩（异步执行，不阻塞响应）
        import asyncio

        if self._should_compress_history(session):
            compression_task = asyncio.create_task(self._compress_history_async(session_id))
            # 保存任务引用防止被垃圾回收，任务完成后自动移除
            compression_task.add_done_callback(lambda t: None)

        total_elapsed = (time.time() - request_start_time) * 1000
        logger.info(
            f"[Orchestrator] <<< handle_submit completed in {total_elapsed:.0f}ms "
            f"(LLM-1: {llm1_elapsed:.0f}ms, handle: {handle_elapsed:.0f}ms)"
        )

        return response

    def _ensure_session_scenario_metadata(self, session: Session) -> None:
        """确保 session 持有最新的 expert scenario 摘要列表。"""
        if session.scenario_briefs:
            return

        session.scenario_briefs = list(self._scenario_loader.scenario_briefs)

    def _compute_request_hash(self, request: SubmitRequest) -> str:
        """
        计算请求载荷的哈希值（用于幂等性校验）。

        载荷字段：mode、user_query、active_task_id
        """
        import hashlib
        import json

        payload = {
            "mode": request.mode,
            "user_query": request.query,
            "active_task_id": request.active_task_id,
        }
        payload_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(payload_str.encode()).hexdigest()[:16]

    def _get_or_create_session_with_validation(
        self,
        session_id: SessionId | None,
        mode: str | None,
        client_request_id: str | None,
        request_hash: str,
        active_task_id: str | None,
        principal: HumanPrincipal | None = None,
    ) -> tuple[Session, bool]:
        """
        获取或创建 Session，包含完整校验逻辑。

        校验项：
        1. Mode 一致性（409001）
        2. 幂等性检查（409003）
        3. activeTaskId 乐观并发（409002）

        Args:
            session_id: Session ID（可选）
            mode: 执行模式
            client_request_id: 客户端请求 ID
            request_hash: 请求载荷哈希
            active_task_id: 活跃任务 ID

        Returns:
            (session, is_new_session)

        Raises:
            SessionNotFoundError: Session 不存在
            ModeMismatchError: mode 不一致
            DuplicateRequestError: 重复请求但载荷不一致
            ActiveTaskMismatchError: activeTaskId 不匹配
        """
        if session_id:
            # 获取已有 Session
            session = self._session_manager.get_session(session_id)
            if not session:
                raise SessionNotFoundError(session_id)

            # 校验 0: Session 关闭状态检查
            # /cancel 成功后 Session 进入关闭态，不再接受新的 /submit
            if session.closed:
                raise SessionClosedError(
                    session_id=session_id,
                    closed_reason=(session.closed_reason.value if session.closed_reason else None),
                )

            # 校验 1: Mode 一致性（409001）
            if session.mode and mode and session.mode != mode:
                raise ModeMismatchError(
                    session_id=session_id,
                    expected_mode=session.mode,
                    actual_mode=mode,
                )

            # 校验 2: 幂等性检查（409003）
            if client_request_id:
                is_duplicate, cached_hash = self._session_manager.check_request_idempotency(
                    session_id=session_id,
                    client_request_id=client_request_id,
                    request_hash=request_hash,
                )
                if is_duplicate:
                    if cached_hash != request_hash:
                        raise DuplicateRequestError(
                            session_id=session_id,
                            client_request_id=client_request_id,
                        )
                    # 幂等重试：载荷一致，让请求继续执行（返回相同结果）
                    logger.debug(
                        f"Idempotent retry detected for session {session_id}, client_request_id {client_request_id}"
                    )

            # 校验 3: activeTaskId 乐观并发校验（409002）
            if active_task_id:
                current_active_task = self._get_active_task(session)
                current_task_id = current_active_task.active_task_id if current_active_task else None
                if current_task_id != active_task_id:
                    raise ActiveTaskMismatchError(
                        session_id=session_id,
                        expected_task_id=active_task_id,
                        actual_task_id=current_task_id,
                    )

            return session, False

        # 创建新 Session（使用 base scenario）
        execution_mode = ExecutionMode(mode) if mode else ExecutionMode.DIRECT_RPC
        return (
            self._session_manager.create_session(
                mode=execution_mode,
                base_scenario=self._scenario_loader.base_scenario,
                user_id=principal.principal_id if principal is not None else None,
                principal_issuer=principal.issuer if principal is not None else None,
                principal_subject=principal.subject if principal is not None else None,
                principal_username=principal.username if principal is not None else None,
                principal_email=principal.email if principal is not None else None,
            ),
            True,
        )

    def _get_or_create_session(
        self,
        session_id: SessionId | None,
    ) -> Session:
        """获取或创建 Session（兼容旧接口）。"""
        if session_id:
            session = self._session_manager.get_session(session_id)
            if not session:
                raise SessionNotFoundError(session_id)
            return session

        # 创建新 Session（使用 base scenario）
        return self._session_manager.create_session(
            mode=ExecutionMode.DIRECT_RPC,
            base_scenario=self._scenario_loader.base_scenario,
        )

    def _get_active_task(self, session: Session) -> ActiveTask | None:
        """获取当前活跃任务。"""
        # Session 现在使用单个 active_task 而非 active_tasks 字典
        return session.active_task

        return None

    # =========================================================================
    # 历史压缩（LLM-7）
    # =========================================================================

    def _should_compress_history(self, session: Session) -> bool:
        """
        判断是否需要执行历史压缩。

        触发条件：dialog_context.recent_turns 数量达到阈值

        Args:
            session: 当前会话

        Returns:
            是否需要压缩
        """
        from ..models.history_compression import COMPRESSION_THRESHOLD

        if not session.dialog_context:
            return False

        turns_count = len(session.dialog_context.recent_turns)
        return turns_count >= COMPRESSION_THRESHOLD

    async def _compress_history_async(self, session_id: SessionId) -> None:
        """
        异步执行历史压缩。

        使用 session 级锁避免并发压缩同一会话。

        Args:
            session_id: Session ID
        """
        import asyncio

        from ..models.history_compression import (
            TURNS_TO_KEEP,
            CompressionTurn,
            HistoryCompressionRequest,
        )

        # 获取或创建 session 级锁
        if session_id not in self._compression_locks:
            self._compression_locks[session_id] = asyncio.Lock()

        lock = self._compression_locks[session_id]

        # 尝试获取锁，如果已经有压缩在进行中则跳过
        if lock.locked():
            logger.debug(f"Compression already in progress for session {session_id}")
            return

        async with lock:
            try:
                # 重新获取 session（可能已被其他请求修改）
                session = self._session_manager.get_session(session_id)
                if not session:
                    logger.warning(f"Session {session_id} not found for compression")
                    return

                # 双重检查是否仍需要压缩
                if not self._should_compress_history(session):
                    return

                # 准备压缩请求
                turns = session.dialog_context.recent_turns
                turns_to_compress_count = len(turns) - TURNS_TO_KEEP

                if turns_to_compress_count <= 0:
                    return

                # 转换为 CompressionTurn 格式
                turns_to_compress = [
                    CompressionTurn(
                        role="user",  # DialogTurn 使用 user_query
                        content=t.user_query,
                        intent=t.intent_type.value if t.intent_type else None,
                        timestamp=t.timestamp,
                    )
                    for t in turns[:turns_to_compress_count]
                ]

                request = HistoryCompressionRequest(
                    session_id=session_id,
                    scenario_id=(session.expert_scenario.id if session.expert_scenario else None),
                    existing_summary=session.dialog_context.history_summary or None,
                    existing_turn_count=session.dialog_context.history_turn_count or 0,
                    turns_to_compress=turns_to_compress,
                    turns_to_keep=TURNS_TO_KEEP,
                )

                # 调用 LLM-7 压缩
                result = await self._history_compressor.compress(request)

                # 更新 session 的 dialog_context
                session.dialog_context.history_summary = result.new_summary
                session.dialog_context.recent_turns = turns[turns_to_compress_count:]
                session.dialog_context.history_turn_count = (
                    session.dialog_context.history_turn_count or 0
                ) + result.compressed_turn_count

                # 保存更新
                self._session_manager.update_session(session)

                logger.info(f"Compressed {result.compressed_turn_count} turns for session {session_id}")

            except Exception as e:
                logger.error(f"History compression failed for session {session_id}: {e}")

    async def _handle_intent(
        self,
        session: Session,
        intent: IntentDecision,
        user_query: str,
    ) -> SubmitResponse:
        """
        根据意图类型处理请求。

        Args:
            session: 当前会话
            intent: 意图决策
            user_query: 用户输入

        Returns:
            SubmitResponse
        """
        if intent.intent_type == IntentType.CHIT_CHAT:
            return await self._handle_chit_chat(session, intent, user_query)

        if intent.intent_type == IntentType.TASK_NEW:
            return await self._handle_task_new(session, intent, user_query)

        if intent.intent_type == IntentType.TASK_INPUT:
            return await self._handle_task_input(session, intent, user_query)

        logger.error(f"Unknown intent type: {intent.intent_type}")
        return self._build_error_response(
            session=session,
            message="无法理解您的请求，请换一种方式描述。",
        )

    async def _handle_chit_chat(
        self,
        session: Session,
        intent: IntentDecision,
        user_query: str,
    ) -> SubmitResponse:
        """
        处理闲聊/通用问答。

        当前简化实现：使用 responseGuide 直接返回。
        完整实现应调用 LLM 生成自然语言回复。
        """
        from ..models.aip import TextDataItem
        from ..models.task import UserResult

        response_text = intent.response_guide or "您好！我是智能助手，有什么可以帮您？"

        # 更新 user_result，让前端能获取回复
        session.user_result = UserResult(
            type=UserResultType.FINAL,
            data_items=[
                TextDataItem(
                    type="text",
                    text=response_text,
                )
            ],
            updated_at=now_iso(),
        )

        # 闲聊不需要任务，清除 active_task
        session.active_task = None

        # 保存更新
        self._session_manager.update_session(session)

        # 记录对话轮次
        self._session_manager.add_dialog_turn(
            session_id=session.session_id,
            user_query=user_query,
            intent_type=IntentType.CHIT_CHAT,
            response_type=ResponseType.FINAL,
            response_summary=(response_text[:100] if len(response_text) > 100 else response_text),
        )

        return self._build_submit_response(session)

    async def _terminate_current_task(self, session: Session, active_task: ActiveTask) -> None:
        """
        终止当前活跃任务。

        当 LLM-1 判定为 TASK_NEW 时，需要先结束当前正在执行的任务：
        - AwaitingCompletion 状态的 Partner：发送 complete 消息
        - Working/Accepted/AwaitingInput 状态的 Partner：发送 cancel 消息
        - 已终态的 Partner（Completed/Failed/Rejected/Canceled）：跳过

        失败的 RPC 调用会记录警告日志但不会阻断流程。

        Args:
            session: 当前会话
            active_task: 需要终止的活跃任务
        """
        from acps_sdk.aip.aip_base_model import TaskState

        logger.info(f"Terminating current task {active_task.active_task_id[:8]} before starting new task")

        # 根据 session 的执行模式选择正确的 executor
        executor = self._get_executor_for_session(session)
        if not executor:
            logger.warning("No executor available for termination")
            return

        # 获取 ACS 缓存以查找 endpoint
        acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

        # 终态集合，这些状态的 Partner 不需要发送任何消息
        terminal_states = {
            TaskState.Completed,
            TaskState.Failed,
            TaskState.Rejected,
            TaskState.Canceled,
        }

        for partner_aic, partner_task in active_task.partner_tasks.items():
            state = partner_task.state

            # 跳过终态的 Partner
            if state in terminal_states:
                logger.debug(f"Partner {partner_aic} already in terminal state {state}, skipping")
                continue

            # 获取 endpoint
            acs_data = acs_cache.get(partner_aic, {})
            from .executor import extract_partner_endpoint

            endpoint = extract_partner_endpoint(acs_data)

            # 尝试从已保存的 partner_task 获取
            saved_endpoint = getattr(partner_task, "endpoint", None)
            if not endpoint and isinstance(saved_endpoint, str) and saved_endpoint:
                endpoint = saved_endpoint

            if not endpoint:
                logger.warning(f"No endpoint in ACS for {partner_aic}, cannot terminate")
                continue

            aip_task_id = partner_task.aip_task_id

            try:
                if state == TaskState.AwaitingCompletion:
                    # AwaitingCompletion 状态：发送 complete
                    logger.info(f"Completing partner {partner_aic} (state: {state})")
                    _, error = await executor.complete_partner(
                        session_id=session.session_id,
                        partner_aic=partner_aic,
                        aip_task_id=aip_task_id,
                        endpoint=endpoint,
                    )
                    if error:
                        logger.warning(f"Failed to complete {partner_aic}: {error}, continuing")
                else:
                    # Working/Accepted/AwaitingInput 状态：发送 cancel
                    logger.info(f"Canceling partner {partner_aic} (state: {state})")
                    _, error = await executor.cancel_partner(
                        session_id=session.session_id,
                        partner_aic=partner_aic,
                        aip_task_id=aip_task_id,
                        endpoint=endpoint,
                    )
                    if error:
                        logger.warning(f"Failed to cancel {partner_aic}: {error}, continuing")
            except Exception as e:
                logger.warning(f"Exception while terminating {partner_aic}: {e}, continuing")

        logger.info(f"Task {active_task.active_task_id[:8]} termination complete")

    async def _handle_task_new(
        self,
        session: Session,
        intent: IntentDecision,
        user_query: str,
    ) -> SubmitResponse:
        """
        处理新任务请求。

        流程：
        1. 如果需要切换场景，更新 Session
        2. 调用 LLM-2 进行全量规划
        3. 创建 ActiveTask 并保存规划结果
        4. 【异步模式】提交到后台执行，立即返回 pending 状态
        5. 【同步模式】等待执行完成后返回 final 状态

        异步模式说明：
        > "/submit 在任务编排之后就返回，后续的任务执行和结果返回是异步的。"

        用户通过 /result API 轮询获取任务执行状态和最终结果。

        Args:
            session: 当前会话
            intent: 意图决策
            user_query: 用户输入

        Returns:
            SubmitResponse
        """
        # 处理场景切换
        if intent.target_scenario:
            # 加载 expert 场景配置并设置到 session
            expert_runtime = self._scenario_loader.get_expert_scenario(intent.target_scenario)
            if expert_runtime:
                session.expert_scenario = expert_runtime
            else:
                # 创建最小化的 ScenarioRuntime
                session.expert_scenario = ScenarioRuntime(
                    id=intent.target_scenario,
                    kind="expert",
                    loaded_at=now_iso(),
                    prompts={},
                )

            self._session_manager.add_event_log(
                session_id=session.session_id,
                event_type=EventLogType.INTENT_DECISION,
                payload={"new_scenario": intent.target_scenario},
            )

        task_text = intent.task_instruction.text if intent.task_instruction else user_query

        # 终止当前活跃任务
        active_task = self._get_active_task(session)
        if active_task:
            if self._background_executor:
                await self._background_executor.cancel_task_and_wait(active_task.active_task_id)
            await self._terminate_current_task(session, active_task)

        # 创建新的活跃任务
        new_task_id = generate_active_task_id()

        # 获取当前场景 ID
        current_scenario_id = session.expert_scenario.id if session.expert_scenario else None

        # 调用 LLM-2 进行全量规划
        import time

        if self._planner and current_scenario_id:
            try:
                llm2_start = time.time()
                logger.debug(f"[Orchestrator] >>> Starting LLM-2 planning for task {new_task_id[:8]}...")

                planning_result = await self._planner.plan(
                    user_query=user_query,
                    session=session,
                    intent=intent,
                )

                llm2_elapsed = (time.time() - llm2_start) * 1000
                active_dims = [dim_id for dim_id, partners in planning_result.selected_partners.items() if partners]
                partner_count = sum(len(partners) for partners in planning_result.selected_partners.values())
                logger.debug(
                    f"[Orchestrator] <<< LLM-2 planning completed in {llm2_elapsed:.0f}ms, "
                    f"dims={active_dims}, partners={partner_count}"
                )

                # 记录规划事件
                self._session_manager.add_event_log(
                    session_id=session.session_id,
                    event_type=EventLogType.PLANNING_RESULT,
                    payload={
                        "active_task_id": new_task_id,
                        "scenario_id": planning_result.scenario_id,
                        "active_dimensions": active_dims,
                    },
                )

                # 构建规划概要响应文本
                response_text = (
                    f"我理解您的需求是：{task_text}\n\n"
                    f"已创建任务（ID: {new_task_id[:8]}）\n\n"
                    f"**规划概要：**\n"
                    f"- 涉及维度：{', '.join(active_dims)}\n"
                    f"- 分配 Partner 数：{partner_count}\n\n"
                    f"正在分发任务给各 Partner，请稍候..."
                )

                # 记录对话轮次（规划阶段）
                self._session_manager.add_dialog_turn(
                    session_id=session.session_id,
                    user_query=user_query,
                    intent_type=IntentType.TASK_NEW,
                    response_type=ResponseType.PENDING,
                    response_summary=response_text[:100],
                )

                # ======================================================
                # 异步执行模式：提交到后台执行，立即返回
                # ======================================================
                if self._async_execution and self._background_executor:
                    # 关键：在提交任务前，设置 session 的 active_task 和 user_result
                    # 这样前端轮询 /result 时能看到正确的状态
                    from acps_sdk.aip.aip_base_model import TaskState

                    from ..models.aip import TextDataItem
                    from ..models.task import PartnerTask, UserResult

                    # 从 planning_result 构建初始的 partner_tasks
                    # 让前端能在 LLM-2 完成后立即看到规划的 partner 列表
                    initial_partner_tasks = {}

                    # 获取 ACS 缓存以便获取 partner 名称
                    acs_cache = {}
                    if self._planner:
                        acs_cache = getattr(self._planner, "_acs_cache", {})

                    for dim_id, selections in planning_result.selected_partners.items():
                        for selection in selections:
                            partner_aic = selection.partner_aic
                            # 查找该 partner 分配的所有维度
                            partner_dimensions = []
                            for d_id, sels in planning_result.selected_partners.items():
                                for sel in sels:
                                    if sel.partner_aic == partner_aic:
                                        partner_dimensions.append(d_id)

                            # 从 ACS 缓存获取 partner 名称
                            partner_name = None
                            if partner_aic in acs_cache:
                                acs_data = acs_cache[partner_aic]
                                partner_name = acs_data.get("name")

                            # 避免重复添加同一个 partner
                            if partner_aic not in initial_partner_tasks:
                                initial_partner_tasks[partner_aic] = PartnerTask(
                                    partner_aic=partner_aic,
                                    partner_name=partner_name,
                                    aip_task_id=f"{new_task_id}:{partner_aic}",
                                    dimensions=partner_dimensions,
                                    state=TaskState.Accepted,  # 初始状态：已接受待执行
                                    last_state_changed_at=now_iso(),
                                    sub_query=selection.instruction_text,  # PartnerSelection 使用 instruction_text 字段
                                )

                    session.active_task = ActiveTask(
                        active_task_id=new_task_id,
                        created_at=now_iso(),
                        external_status=ActiveTaskStatus.PENDING,
                        partner_tasks=initial_partner_tasks,
                        # planning 不在这里设置，由 background_executor 管理
                    )
                    session.user_result = UserResult(
                        type=UserResultType.PENDING,
                        data_items=[TextDataItem(type="text", text=response_text)],
                        updated_at=now_iso(),
                    )
                    # 保存 session 更新
                    self._session_manager.update_session(session)

                    # A2: 审计——规划完成，partner 已选定
                    _partner_aics = sorted(
                        {sel.partner_aic for sels in planning_result.selected_partners.values() for sel in sels}
                    )
                    try:
                        await LEADER_EMITTER.emit(
                            AuditBody(
                                actor=AuditActor(id=LEADER_AIC, type="agent"),
                                action=AuditAction(
                                    name="plan_partners_selected",
                                    type="partner_orchestration",
                                ),
                                target=AuditTarget(type="task", id=new_task_id),
                                result=AuditResult(
                                    status="success",
                                    reason=f"partners={_partner_aics}",
                                ),
                            ),
                            trace_id=session.session_id,
                            correlation_id=new_task_id,
                        )
                    except Exception:
                        logger.warning("Audit emit A2 failed", exc_info=True)

                    # 提交任务到后台执行
                    self._background_executor.submit_task(
                        task_id=new_task_id,
                        session_id=session.session_id,
                        session=session,
                        planning_result=planning_result,
                        task_text=task_text,
                        metadata={
                            "scenario_kind": self._get_scenario_kind(session),
                            "scenario_id": self._get_scenario_id(session),
                        },
                    )

                    logger.info(f"Task {new_task_id} submitted for async execution, returning pending response")

                    # 立即返回 pending 状态
                    return self._build_submit_response(session, new_task_id)

                # ======================================================
                # 同步执行模式：等待执行完成后返回
                # ======================================================
                execution_result = await self._execute_planning(
                    session=session,
                    active_task_id=new_task_id,
                    planning_result=planning_result,
                )

                # 根据执行结果构建响应（调用 LLM-6 Aggregator）
                return await self._build_execution_response(
                    session=session,
                    active_task_id=new_task_id,
                    task_text=task_text,
                    planning_result=planning_result,
                    execution_result=execution_result,
                )

            except Exception as e:
                logger.error(f"Planning failed: {e}", exc_info=True)
                # 降级处理：返回 stub 响应
                response_text = (
                    f"我理解您的需求是：{task_text}\n\n"
                    f"已创建任务（ID: {new_task_id[:8]}）\n\n"
                    f"⚠️ 规划过程遇到问题，正在使用降级策略处理..."
                )

        else:
            # 没有 Planner 或没有场景 ID 时的 stub 响应
            response_text = (
                f"我理解您的需求是：{task_text}\n\n"
                f"已创建任务（ID: {new_task_id[:8]}），正在规划中...\n\n"
                f"【注意】当前版本仅实现了意图分析（LLM-1），"
                f"完整的任务规划功能（LLM-2 到 LLM-6）将在后续版本实现。"
            )

        # 记录对话轮次
        self._session_manager.add_dialog_turn(
            session_id=session.session_id,
            user_query=user_query,
            intent_type=IntentType.TASK_NEW,
            response_type=ResponseType.PENDING,
            response_summary=response_text[:100],
        )

        return self._build_submit_response(session, new_task_id)

    async def _handle_task_input(
        self,
        session: Session,
        intent: IntentDecision,
        user_query: str,
    ) -> SubmitResponse:
        """
        处理任务补充输入（LLM-4 增量更新流程）。

        完整流程：
        1. 获取当前活跃任务和处于 AwaitingInput 状态的 Partner
        2. 调用 LLM-4 (InputRouter) 分析用户输入、提取字段值
        3. 验证信息完整性
        4. 如果充分，对各 Partner 执行 continue
        5. 如果不充分，调用 LLM-3 进入反问闭环

        Args:
            session: 当前会话
            intent: 意图决策
            user_query: 用户输入

        Returns:
            SubmitResponse
        """
        # 1. 获取当前活跃任务
        active_task = self._get_active_task(session)
        if not active_task:
            logger.warning("No active task found for TASK_INPUT intent")
            return self._build_error_response(
                session=session,
                message="当前没有活跃任务，无法处理补充输入。",
            )

        active_task_id = active_task.active_task_id

        # 2. 检查是否有 InputRouter
        if not self._input_router:
            logger.warning("InputRouter not configured, using fallback")
            return self._build_submit_response(session, active_task_id)

        # 3. 获取 AwaitingInput 信息
        # 从 active_task 的 partner_tasks 中获取等待输入的 Partner
        last_clarification = None
        partner_gaps = []

        # 从 active_task 重建 gap 信息
        if active_task.partner_tasks:
            from acps_sdk.aip.aip_base_model import TaskState

            for partner_aic, partner_task in active_task.partner_tasks.items():
                # 直接检查 partner_task.state，而不是 last_snapshot.status.state
                # state 字段直接存储了最新的状态
                current_state = partner_task.state
                logger.debug(
                    f"[TASK_INPUT] Partner {partner_aic}: state={current_state}, "
                    f"has_snapshot={partner_task.last_snapshot is not None}"
                )

                if current_state == TaskState.AwaitingInput:
                    # 从 data_items 提取 gap（如果有 last_snapshot）
                    awaiting_fields = []
                    question_text = None

                    # 尝试从 last_snapshot 获取详细的 gap 信息
                    if partner_task.last_snapshot and partner_task.last_snapshot.status:
                        status = partner_task.last_snapshot.status
                        data_items = getattr(status, "data_items", None)
                        if data_items is None:
                            data_items = getattr(status, "dataItems", [])
                        for item in data_items or []:
                            item_dict = item.model_dump() if hasattr(item, "model_dump") else item
                            if item_dict.get("type") == "text":
                                text = item_dict.get("text", "")
                                if text and not question_text:
                                    question_text = text
                            elif item_dict.get("type") == "data":
                                data = item_dict.get("data", {})
                                if "requiredFields" in data:
                                    for fd in data["requiredFields"]:
                                        from ..models.clarification import RequiredField

                                        rf = RequiredField(
                                            field_name=fd.get("name", fd.get("fieldName", "")),
                                            field_label=fd.get("label", fd.get("fieldLabel", "")),
                                            field_type=fd.get("type", "string"),
                                            description=fd.get("description"),
                                            required=fd.get("required", True),
                                            constraints=fd.get("constraints"),
                                            example=fd.get("example"),
                                        )
                                        awaiting_fields.append(rf)

                    # 获取 dimensions 第一个作为 dimension_id
                    dimension_id = partner_task.dimensions[0] if partner_task.dimensions else "unknown"

                    # 即使没有 last_snapshot，也要为 AwaitingInput 状态的 Partner 创建 gap
                    # 这样 InputRouter 可以将用户输入路由到正确的 Partner
                    gap = PartnerGapInfo(
                        partner_aic=partner_aic,
                        partner_name=partner_task.partner_name or partner_aic,
                        dimension_id=dimension_id,
                        aip_task_id=partner_task.aip_task_id,
                        awaiting_fields=awaiting_fields,
                        question_text=question_text,
                    )
                    partner_gaps.append(gap)
                    logger.debug(
                        f"[TASK_INPUT] Created gap for {partner_aic}: "
                        f"fields={len(awaiting_fields)}, question={bool(question_text)}"
                    )

        if not partner_gaps:
            logger.warning("No awaiting partner gaps found")
            return self._build_error_response(
                session=session,
                message="没有找到等待输入的 Partner，请重新开始任务。",
            )

        # 4. 构建 InputRoutingRequest 并调用 LLM-4
        user_context = {}
        if hasattr(session, "user_context") and session.user_context:
            user_context = session.user_context

        routing_request = InputRoutingRequest(
            user_input=user_query,
            partner_gaps=partner_gaps,
            active_task_id=active_task_id,
            last_clarification=last_clarification,
            user_context=user_context,
            scenario_id=self._get_scenario_id(session),
        )

        try:
            routing_result = await self._input_router.route(routing_request)

            logger.info(
                f"InputRouter result: isSufficient={routing_result.is_sufficient}, "
                f"patches={len(routing_result.patches_by_partner)}, "
                f"missing={len(routing_result.missing_fields)}"
            )

            # 记录路由事件
            self._session_manager.add_event_log(
                session_id=session.session_id,
                event_type=EventLogType.USER_RESULT_UPDATE,
                payload={
                    "action": "input_routing",
                    "is_sufficient": routing_result.is_sufficient,
                    "patched_partners": list(routing_result.patches_by_partner.keys()),
                    "missing_fields": [f.field_name for f in routing_result.missing_fields],
                    "summary": routing_result.routing_summary,
                },
            )

        except Exception as e:
            logger.error(f"InputRouter failed: {e}", exc_info=True)
            return self._build_error_response(
                session=session,
                message=f"处理补充输入时出错：{e!s}",
            )

        # 5. 根据路由结果决定下一步
        if routing_result.is_sufficient:
            # 信息充分，对各 Partner 执行 continue
            return await self._execute_continue_for_partners(
                session=session,
                active_task=active_task,
                routing_result=routing_result,
                user_query=user_query,
            )
        # 信息不充分，进入反问闭环 (LLM-3)
        return await self._handle_insufficient_input(
            session=session,
            active_task=active_task,
            routing_result=routing_result,
            partner_gaps=partner_gaps,
            user_query=user_query,
        )

    async def _execute_continue_for_partners(
        self,
        session: Session,
        active_task: ActiveTask,
        routing_result: InputRoutingResult,
        user_query: str,
    ) -> SubmitResponse:
        """
        对各 Partner 执行 continue 操作。

        Args:
            session: 会话
            active_task: 活跃任务
            routing_result: 路由结果
            user_query: 用户输入

        Returns:
            SubmitResponse
        """
        # 根据 session 的执行模式选择正确的 executor
        executor = self._get_executor_for_session(session)
        if not executor:
            logger.warning("No executor configured, cannot continue partners")
            response_text = (
                f"✅ 已收到您的补充信息\n\n"
                f"**路由摘要：** {routing_result.routing_summary}\n\n"
                f"⚠️ 执行器未配置，无法发送 continue 指令。"
            )
            self._session_manager.add_dialog_turn(
                session_id=session.session_id,
                user_query=user_query,
                intent_type=IntentType.TASK_INPUT,
                response_type=ResponseType.PENDING,
                response_summary=response_text[:100],
            )
            return self._build_submit_response(session, active_task.active_task_id)

        # 执行 continue
        continue_results = []
        acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

        for partner_aic, patch in routing_result.patches_by_partner.items():
            # 获取 endpoint
            acs_data = acs_cache.get(partner_aic, {})
            from .executor import extract_partner_endpoint

            endpoint = extract_partner_endpoint(acs_data)

            # 尝试从 active_task 获取
            if not endpoint and active_task.partner_tasks:
                pt = active_task.partner_tasks.get(partner_aic)
                if pt and pt.endpoint:
                    endpoint = pt.endpoint

            if not endpoint:
                logger.warning(f"No endpoint for {partner_aic}, skipping continue")
                continue_results.append(
                    {
                        "partner": partner_aic[:8],
                        "status": "skipped",
                        "reason": "no endpoint",
                    }
                )
                continue

            try:
                # 构建 continue 输入（文本 + 结构化数据）
                continue_input = patch.patch_text
                if patch.patch_data:
                    continue_input += f"\n\n[结构化数据]: {json.dumps(patch.patch_data, ensure_ascii=False)}"

                task, error = await executor.continue_partner(
                    session_id=session.session_id,
                    partner_aic=partner_aic,
                    aip_task_id=patch.aip_task_id,
                    endpoint=endpoint,
                    user_input=continue_input,
                )

                if task:
                    # 更新 active_task 中的 partner_task
                    if active_task.partner_tasks and partner_aic in active_task.partner_tasks:
                        pt = active_task.partner_tasks[partner_aic]
                        pt.last_snapshot = task
                        pt.state = task.status.state

                    continue_results.append(
                        {
                            "partner": partner_aic[:8],
                            "status": "success",
                            "new_state": (
                                task.status.state.value
                                if hasattr(task.status.state, "value")
                                else str(task.status.state)
                            ),
                        }
                    )
                    logger.info(f"Partner {partner_aic[:8]}... continued, new state: {task.status.state}")
                else:
                    continue_results.append(
                        {
                            "partner": partner_aic[:8],
                            "status": "failed",
                            "error": error or "Unknown error",
                        }
                    )
                    logger.error(f"Failed to continue {partner_aic}: {error}")

            except Exception as e:
                logger.error(f"Continue failed for {partner_aic}: {e}", exc_info=True)
                continue_results.append(
                    {
                        "partner": partner_aic[:8],
                        "status": "error",
                        "error": str(e),
                    }
                )

        # 构建响应
        success_count = sum(1 for r in continue_results if r["status"] == "success")
        total_count = len(routing_result.patches_by_partner)

        response_text = (
            f"✅ 已收到您的补充信息并转发给相关服务\n\n"
            f"**路由摘要：** {routing_result.routing_summary}\n\n"
            f"**执行结果：** 成功 {success_count}/{total_count} 个 Partner\n\n"
        )

        # 如果有失败的，添加说明
        failed = [r for r in continue_results if r["status"] != "success"]
        if failed:
            response_text += "**注意：** 部分 Partner 处理失败\n"

        response_text += "正在等待各服务返回结果..."

        self._session_manager.add_dialog_turn(
            session_id=session.session_id,
            user_query=user_query,
            intent_type=IntentType.TASK_INPUT,
            response_type=ResponseType.PENDING,
            response_summary=response_text[:100],
        )

        # 更新 user_result 为 pending 状态（清除之前的 clarification）
        from ..models.aip import TextDataItem
        from ..models.base import UserResultType
        from ..models.task import UserResult

        session.user_result = UserResult(
            type=UserResultType.PENDING,
            data_items=[TextDataItem(type="text", text=response_text)],
            updated_at=now_iso(),
        )
        self._session_manager.update_session(session)

        # 关键：如果有成功的 continue，恢复后台轮询以监控 Partner 后续状态变化
        if success_count > 0 and self._background_executor:
            logger.info(f"[TASK_INPUT] Resuming background polling for task {active_task.active_task_id[:12]}")
            self._background_executor.resume_after_continue(
                task_id=active_task.active_task_id,
                session=session,
                planning_result=None,  # 从 session 重建
            )

        return self._build_submit_response(session, active_task.active_task_id)

    async def _handle_insufficient_input(
        self,
        session: Session,
        active_task: ActiveTask,
        routing_result: InputRoutingResult,
        partner_gaps: list[PartnerGapInfo],
        user_query: str,
    ) -> SubmitResponse:
        """
        处理信息不充分的情况：进入反问闭环。

        Args:
            session: 会话
            active_task: 活跃任务
            routing_result: 路由结果（包含仍缺失的字段）
            partner_gaps: Partner 缺口列表
            user_query: 用户输入

        Returns:
            SubmitResponse（类型为 clarification）
        """
        # 根据 session 的执行模式选择正确的 executor
        executor = self._get_executor_for_session(session)

        # 如果有部分 Partner 可以 continue，先执行
        if routing_result.patches_by_partner and executor:
            # 对有补丁的 Partner 执行 continue
            acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

            for partner_aic, patch in routing_result.patches_by_partner.items():
                acs_data = acs_cache.get(partner_aic, {})
                from .executor import extract_partner_endpoint

                endpoint = extract_partner_endpoint(acs_data)

                if not endpoint and active_task.partner_tasks:
                    pt = active_task.partner_tasks.get(partner_aic)
                    if pt and pt.endpoint:
                        endpoint = pt.endpoint

                if endpoint:
                    try:
                        continue_input = patch.patch_text
                        if patch.patch_data:
                            continue_input += f"\n\n[结构化数据]: {json.dumps(patch.patch_data, ensure_ascii=False)}"

                        await executor.continue_partner(
                            session_id=session.session_id,
                            partner_aic=partner_aic,
                            aip_task_id=patch.aip_task_id,
                            endpoint=endpoint,
                            user_input=continue_input,
                        )
                        logger.info(f"Partial continue for {partner_aic[:8]}...")
                    except Exception as e:
                        logger.warning(f"Partial continue failed for {partner_aic}: {e}")

        # 使用 LLM-3 生成新的反问
        if self._clarification_merger and routing_result.missing_fields:
            # 从 missing_fields 重建 partner_items
            from ..models.clarification import (
                ClarificationMergeInput,
                PartnerClarificationItem,
            )

            # 过滤出仍有缺失字段的 Partner
            remaining_partner_items = []
            for gap in partner_gaps:
                remaining_fields = [
                    f
                    for f in gap.awaiting_fields
                    if any(mf.field_name == f.field_name for mf in routing_result.missing_fields)
                ]
                if remaining_fields:
                    item = PartnerClarificationItem(
                        partner_aic=gap.partner_aic,
                        partner_name=gap.partner_name,
                        dimension_id=gap.dimension_id,
                        aip_task_id=gap.aip_task_id,
                        question_text=gap.question_text,
                        required_fields=remaining_fields,
                    )
                    remaining_partner_items.append(item)

            if remaining_partner_items:
                merge_input = ClarificationMergeInput(
                    partner_items=remaining_partner_items,
                    user_query=user_query,
                    user_context=session.user_context if session.user_context else None,
                    scenario_id=self._get_scenario_id(session),
                )

                try:
                    clarification = await self._clarification_merger.merge(merge_input)

                    # 记录 clarification 信息到日志（用于调试）
                    logger.debug(f"Clarification generated: {clarification.question_text[:100]}...")

                    response_text = clarification.question_text
                    response_type = "clarification"

                    # 记录事件
                    self._session_manager.add_event_log(
                        session_id=session.session_id,
                        event_type=EventLogType.USER_RESULT_UPDATE,
                        payload={
                            "action": "clarification_needed",
                            "missing_fields": [f.field_name for f in routing_result.missing_fields],
                            "partial_patches": list(routing_result.patches_by_partner.keys()),
                        },
                    )

                except Exception as e:
                    logger.error(f"ClarificationMerger failed: {e}", exc_info=True)
                    # 降级处理
                    missing_labels = [f.field_label or f.field_name for f in routing_result.missing_fields]
                    response_text = (
                        f"感谢您的补充信息！还需要您告诉我以下信息：\n\n"
                        f"- {', '.join(missing_labels)}\n\n"
                        f"请提供这些信息，我会继续为您服务。"
                    )
                    response_type = "clarification"
            else:
                # 没有剩余 Partner 需要澄清
                response_text = "已收到您的信息，正在处理中..."
                response_type = "pending"
        else:
            # 没有 ClarificationMerger 或没有缺失字段
            missing_labels = [f.field_label or f.field_name for f in routing_result.missing_fields]
            response_text = (
                f"还需要以下信息才能继续：{', '.join(missing_labels)}" if missing_labels else "请提供更多信息。"
            )
            response_type = "clarification"

        # 映射 response_type 到 ResponseType 枚举
        response_type_enum = ResponseType.CLARIFICATION if response_type == "clarification" else ResponseType.PENDING

        self._session_manager.add_dialog_turn(
            session_id=session.session_id,
            user_query=user_query,
            intent_type=IntentType.TASK_INPUT,
            response_type=response_type_enum,
            response_summary=response_text[:100],
        )

        return self._build_submit_response(session, active_task.active_task_id)

    def _build_error_response(
        self,
        session: Session,
        message: str,
    ) -> SubmitResponse:
        """构建错误响应。"""
        return SubmitResponse(
            error=CommonError(
                code=500000,
                message=message,
            )
        )

    async def _execute_planning(
        self,
        session: Session,
        active_task_id: str,
        planning_result,
        on_poll_update: Callable[[ExecutionResult], None] | None = None,
    ) -> ExecutionResult | None:
        """
        执行规划结果。

        调用 TaskExecutor 将任务分发给各 Partner。
        如果有 Partner 进入 AwaitingCompletion 状态，调用 LLM-5 完成闸门进行决策。

        Args:
            session: 会话对象
            active_task_id: 任务 ID
            planning_result: 规划结果
            on_poll_update: 每轮轮询后的回调函数，用于实时更新状态
        """
        # 根据 session 的执行模式选择对应的执行器
        executor = self._get_executor_for_session(session)
        if not executor:
            logger.warning("No executor configured, skipping execution")
            return None

        try:
            # 获取 ACS 缓存（从 planner 获取）
            acs_cache = {}
            if self._planner:
                acs_cache = getattr(self._planner, "_acs_cache", {})

            # 更新 executor 的 ACS 缓存
            executor.acs_cache = acs_cache

            poll_update_callback = on_poll_update
            if session.mode == ExecutionMode.GROUP:

                def poll_update_callback(exec_result):
                    self._sync_group_session_state(session)
                    if on_poll_update:
                        on_poll_update(exec_result)

            # 执行
            execution_result = await executor.execute(
                session_id=session.session_id,
                active_task_id=active_task_id,
                planning_result=planning_result,
                on_poll_update=poll_update_callback,  # 传递回调以便实时更新状态
            )
            self._sync_group_session_state(session)

            # 记录执行事件
            self._session_manager.add_event_log(
                session_id=session.session_id,
                event_type=EventLogType.AIP_REQUEST,
                payload={
                    "active_task_id": active_task_id,
                    "phase": execution_result.phase.value,
                    "completed_partners": execution_result.completed_partners,
                    "awaiting_input_partners": execution_result.awaiting_input_partners,
                    "failed_partners": execution_result.failed_partners,
                },
            )

            # 如果有 Partner 进入 AwaitingCompletion 状态，调用 LLM-5 循环处理
            # 循环最多执行 MAX_COMPLETION_GATE_ROUNDS 次
            if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
                acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}
                execution_result = await handle_awaiting_completion_with_loop(
                    session=session,
                    active_task_id=active_task_id,
                    execution_result=execution_result,
                    planning_result=planning_result,
                    completion_gate=self._completion_gate,
                    executor=executor,  # 传入正确的 executor
                    acs_cache=acs_cache,
                    scenario_id=self._get_scenario_id(session),
                )

            return execution_result

        except Exception as e:
            logger.error(f"Execution failed: {e}", exc_info=True)
            return None

    async def _aggregate_results(
        self,
        session: Session,
        task_text: str,
        execution_result: ExecutionResult,
        planning_result,
    ) -> AggregationResult:
        """
        调用 Aggregator (LLM-6) 整合多个 Partner 的产出。

        Args:
            session: 会话对象
            task_text: 任务描述
            execution_result: 执行结果
            planning_result: 规划结果

        Returns:
            AggregationResult
        """
        from acps_sdk.aip.aip_base_model import TaskState

        if not self._aggregator:
            # 没有 Aggregator，返回简单拼接
            return self._fallback_aggregation(
                task_text=task_text,
                execution_result=execution_result,
            )

        try:
            # 构建 PartnerOutput 列表
            partner_outputs: list[PartnerOutput] = []
            degradations: list[DegradationInfo] = []

            for partner_aic, result in execution_result.partner_results.items():
                # 提取数据项
                data_items = []
                for item in result.data_items:
                    if hasattr(item, "model_dump"):
                        data_items.append(item.model_dump())
                    elif hasattr(item, "dict"):
                        data_items.append(item.dict())
                    elif isinstance(item, dict):
                        data_items.append(item)
                    else:
                        data_items.append({"text": str(item)})

                # 从 products 中提取
                products = []
                if partner_aic in execution_result.products:
                    for prod in execution_result.products[partner_aic]:
                        if hasattr(prod, "model_dump"):
                            products.append(prod.model_dump())
                        elif hasattr(prod, "dict"):
                            products.append(prod.dict())
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

            # 获取用户原始查询
            user_query = task_text

            # 获取对话摘要（如果有）
            dialog_summary = None
            if session.dialog_context and session.dialog_context.recent_turns:
                # 简单摘要：取前几轮对话
                recent_turns = session.dialog_context.recent_turns[-5:]
                dialog_summary = "\n".join(
                    (f"user: {turn.user_query[:100]}..." if len(turn.user_query) > 100 else f"user: {turn.user_query}")
                    for turn in recent_turns
                )

            # 调用 Aggregator
            logger.info(
                f"Calling Aggregator with {len(partner_outputs)} partner outputs, {len(degradations)} degradations"
            )

            result = await self._aggregator.aggregate(
                partner_outputs=partner_outputs,
                degradations=degradations,
                user_query=user_query,
                dialog_summary=dialog_summary,
                scenario_id=self._get_scenario_id(session),
            )

            logger.info(f"Aggregation completed: type={result.type}")
            return result

        except Exception as e:
            logger.error(f"Aggregation failed: {e}", exc_info=True)
            return self._fallback_aggregation(
                task_text=task_text,
                execution_result=execution_result,
            )

    def _fallback_aggregation(
        self,
        task_text: str,
        execution_result: ExecutionResult,
    ) -> AggregationResult:
        """
        降级整合：当 Aggregator 不可用时直接拼接结果。
        """
        from datetime import datetime

        text_parts = []
        text_parts.append("✅ 任务已完成！\n\n")
        text_parts.append(f"**任务概要：** {task_text}\n\n")
        text_parts.append("**执行结果：**\n")
        text_parts.append(f"- 完成的 Partner 数：{len(execution_result.completed_partners)}\n")

        # 添加产出物摘要
        if execution_result.products:
            text_parts.append("\n**各 Partner 结果：**\n")
            for partner_aic, data_items in execution_result.products.items():
                text_parts.append(f"- {partner_aic[:8]}...: {len(data_items)} 项输出\n")

        return AggregationResult(
            type="final",
            text="".join(text_parts),
            structured={
                "fallback": True,
                "partner_count": len(execution_result.completed_partners),
            },
            created_at=datetime.now(UTC).isoformat(),
        )

    async def _try_auto_fill_from_context(
        self,
        session: Session,
        execution_result: ExecutionResult,
        planning_result,
        user_context: dict[str, Any],
    ) -> list[str]:
        """
        尝试用 userContext 自动补全 AwaitingInput 的 Partner 缺口。

        设计：
        > AwaitingInput：先用 userContext 与已知约束自动补全并 continue

        逻辑：
        1. 遍历每个 AwaitingInput 的 Partner
        2. 检查其 dataItems 中的字段是否可以从 userContext 中获取
        3. 如果可以完全补全，发送 continue 消息

        Args:
            session: 当前 Session
            execution_result: 执行结果
            planning_result: 规划结果
            user_context: 用户上下文

        Returns:
            成功自动补全的 Partner AIC 列表
        """
        # 根据 session 的执行模式选择正确的 executor
        executor = self._get_executor_for_session(session)
        if not executor:
            logger.warning("No executor available for auto-fill")
            return []

        auto_filled_partners = []

        # 获取 ACS 缓存
        acs_cache = getattr(self._planner, "_acs_cache", {}) if self._planner else {}

        for partner_aic in list(execution_result.awaiting_input_partners):
            partner_result = execution_result.partner_results.get(partner_aic)
            if not partner_result:
                continue

            # 提取需要补全的字段
            required_fields = self._extract_required_fields(partner_result)
            if not required_fields:
                continue

            # 尝试从 userContext 匹配
            auto_fill_data = self._match_context_to_fields(
                required_fields=required_fields,
                user_context=user_context,
            )

            if not auto_fill_data:
                # 无法从 userContext 补全
                continue

            # 检查是否所有必填字段都已补全
            missing_fields = [f for f in required_fields if f not in auto_fill_data]
            if missing_fields:
                logger.debug(f"Partner {partner_aic[:8]} still missing fields: {missing_fields}")
                continue

            # 可以完全补全，发送 continue
            logger.info(f"Auto-filling Partner {partner_aic[:8]} with context: {list(auto_fill_data.keys())}")

            try:
                # 获取 endpoint
                endpoint = resolve_partner_endpoint(
                    partner_aic=partner_aic,
                    planning_result=planning_result,
                    acs_cache=acs_cache,
                )

                if not endpoint:
                    continue

                # 构建补全消息
                fill_text = "系统根据您的偏好自动补全了以下信息：\n"
                for field, value in auto_fill_data.items():
                    fill_text += f"- {field}: {value}\n"

                aip_task_id = partner_result.task.id if partner_result.task else f"task:{partner_aic}"

                # 发送 continue
                task, error = await executor.continue_partner(
                    session_id=session.session_id,
                    partner_aic=partner_aic,
                    aip_task_id=aip_task_id,
                    endpoint=endpoint,
                    user_input=fill_text,
                )

                if task and not error:
                    # 更新 Partner 状态
                    partner_result.state = task.status.state
                    partner_result.task = task

                    # 从 awaiting_input 列表中移除
                    if partner_aic in execution_result.awaiting_input_partners:
                        execution_result.awaiting_input_partners.remove(partner_aic)

                    auto_filled_partners.append(partner_aic)

                    logger.info(f"Auto-fill continue sent to {partner_aic[:8]}, new state: {task.status.state}")
                else:
                    logger.warning(f"Auto-fill continue failed for {partner_aic[:8]}: {error}")

            except Exception as e:
                logger.warning(f"Failed to auto-fill Partner {partner_aic[:8]}: {e}")

        return auto_filled_partners

    def _extract_required_fields(self, partner_result) -> list[str]:
        """
        从 Partner 结果中提取需要补全的字段名。

        Args:
            partner_result: Partner 执行结果

        Returns:
            字段名列表
        """
        required_fields = []

        data_items = []
        if partner_result.task and partner_result.task.status.dataItems:
            data_items = partner_result.task.status.dataItems
        elif partner_result.data_items:
            data_items = partner_result.data_items

        for item in data_items:
            # 尝试提取字段名
            if hasattr(item, "key"):
                required_fields.append(item.key)
            elif hasattr(item, "field"):
                required_fields.append(item.field)
            elif hasattr(item, "name"):
                required_fields.append(item.name)
            elif isinstance(item, dict):
                if "key" in item:
                    required_fields.append(item["key"])
                elif "field" in item:
                    required_fields.append(item["field"])
                elif "name" in item:
                    required_fields.append(item["name"])

        return required_fields

    def _match_context_to_fields(
        self,
        required_fields: list[str],
        user_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        将 userContext 中的值匹配到所需字段。

        支持的匹配规则：
        1. 精确匹配（字段名相同）
        2. 常见别名匹配（如 city -> destination_city）

        Args:
            required_fields: 需要补全的字段列表
            user_context: 用户上下文

        Returns:
            匹配到的字段值映射
        """
        # 常见字段别名映射
        field_aliases = {
            "city": ["destination_city", "arrival_city", "target_city", "城市"],
            "date": ["travel_date", "arrival_date", "check_in_date", "日期"],
            "budget": ["price_range", "max_price", "预算"],
            "people": ["guests", "guest_count", "num_guests", "travelers", "人数"],
            "name": ["guest_name", "traveler_name", "姓名"],
            "phone": ["contact_phone", "mobile", "电话"],
        }

        matched = {}

        for field in required_fields:
            field_lower = field.lower()

            # 1. 精确匹配
            if field in user_context:
                matched[field] = user_context[field]
                continue

            if field_lower in user_context:
                matched[field] = user_context[field_lower]
                continue

            # 2. 别名匹配
            for base_name, aliases in field_aliases.items():
                if field_lower in aliases or base_name in field_lower:
                    # 检查 user_context 中是否有对应的基础名称
                    if base_name in user_context:
                        matched[field] = user_context[base_name]
                        break
                    # 检查别名
                    for alias in aliases:
                        if alias in user_context:
                            matched[field] = user_context[alias]
                            break

        return matched

    async def _handle_awaiting_input(
        self,
        session: Session,
        execution_result: ExecutionResult,
        planning_result,
        user_query: str | None = None,
    ) -> MergedClarification:
        """
        处理 AwaitingInput 状态：使用 LLM-3 合并多个 Partner 的反问需求。

        设计：
        > AwaitingInput：先用 userContext 与已知约束自动补全并 continue；
        > 仍缺则生成"待用户补充问题"并挂起等待下一次 /submit

        Args:
            session: 当前 Session
            execution_result: 执行结果（包含 AwaitingInput 的 Partner 信息）
            planning_result: 规划结果（用于获取 Partner 名称）
            user_query: 用户原始查询（用于上下文）

        Returns:
            MergedClarification: 合并后的反问结果
        """
        logger.info(f"Handling awaiting input for {len(execution_result.awaiting_input_partners)} partners")

        # 0. 尝试用 userContext 自动补全
        user_context = session.user_context if session.user_context else {}
        # 根据 session 的执行模式选择正确的 executor
        executor = self._get_executor_for_session(session)
        if user_context and executor:
            auto_filled_partners = await self._try_auto_fill_from_context(
                session=session,
                execution_result=execution_result,
                planning_result=planning_result,
                user_context=user_context,
            )

            if auto_filled_partners:
                logger.info(f"Auto-filled {len(auto_filled_partners)} partner(s) from userContext")

                # 如果所有 AwaitingInput 的 Partner 都被自动补全了，不需要反问
                if not execution_result.awaiting_input_partners:
                    logger.info("All AwaitingInput partners auto-filled, no clarification needed")
                    # 返回一个空的 MergedClarification
                    return MergedClarification(
                        question_text="",
                        merged_fields=[],
                        source_partners=[],
                        auto_filled=True,
                    )

        # 1. 从 execution_result 提取各 Partner 的澄清需求
        partner_items: list[PartnerClarificationItem] = []

        for partner_aic in execution_result.awaiting_input_partners:
            partner_result = execution_result.partner_results.get(partner_aic)
            if not partner_result:
                logger.warning(f"No partner result found for {partner_aic}")
                continue

            # 获取 Partner 名称（从 planning_result 或 ACS）
            partner_name = None
            if planning_result and hasattr(planning_result, "selected_partners"):
                # 尝试从 selected_partners 获取名称
                for dim_id, partners in planning_result.selected_partners.items():
                    for p in partners:
                        if hasattr(p, "aic") and p.aic == partner_aic:
                            partner_name = getattr(p, "name", None)
                            break
                    if partner_name:
                        break

            # 从 Task.status.dataItems 提取澄清需求
            if partner_result.task and partner_result.task.status.dataItems:
                data_items_raw = [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in partner_result.task.status.dataItems
                ]
            else:
                # 使用 questions_for_user 中的数据
                data_items_raw = []
                for data_item in execution_result.questions_for_user:
                    if hasattr(data_item, "model_dump"):
                        data_items_raw.append(data_item.model_dump())
                    elif isinstance(data_item, dict):
                        data_items_raw.append(data_item)

            # 从 partner_result 获取维度 ID
            dimension_id = partner_result.dimension_id or "unknown"

            # 提取 task_id
            aip_task_id = partner_result.task.id if partner_result.task else f"task-{partner_aic[:8]}"

            # 提取澄清需求
            clarification_item = extract_clarification_from_task_status(
                partner_aic=partner_aic,
                partner_name=partner_name,
                dimension_id=dimension_id,
                aip_task_id=aip_task_id,
                data_items=data_items_raw,
            )
            partner_items.append(clarification_item)

        # 2. 构建 LLM-3 输入
        user_context = session.user_context if session.user_context else {}

        merge_input = ClarificationMergeInput(
            partner_items=partner_items,
            user_query=user_query,
            user_context=user_context,
            scenario_id=self._get_scenario_id(session),
        )

        # 3. 调用 ClarificationMerger (LLM-3)
        merged_result = await self._clarification_merger.merge(merge_input)

        logger.info(
            f"Clarification merged: {len(merged_result.merged_fields)} fields from "
            f"{len(merged_result.source_partners)} partners"
        )

        # 4. 记录事件日志
        self._session_manager.add_event_log(
            session_id=session.session_id,
            event_type=EventLogType.USER_RESULT_UPDATE,
            payload={
                "action": "clarification_merged",
                "source_partners": merged_result.source_partners,
                "merged_fields": [f.field_name for f in merged_result.merged_fields],
            },
        )

        return merged_result

    async def _build_execution_response(
        self,
        session: Session,
        active_task_id: str,
        task_text: str,
        planning_result,
        execution_result: ExecutionResult | None,
    ) -> SubmitResponse:
        """
        根据执行结果构建响应。

        在 COMPLETED 阶段调用 Aggregator (LLM-6) 进行结果整合。
        """
        active_dims = [dim_id for dim_id, partners in planning_result.selected_partners.items() if partners]
        partner_count = sum(len(partners) for partners in planning_result.selected_partners.values())

        # 如果没有执行结果，返回规划概要
        if not execution_result:
            response_text = (
                f"我理解您的需求是：{task_text}\n\n"
                f"已创建任务（ID: {active_task_id[:8]}）\n\n"
                f"**规划概要：**\n"
                f"- 涉及维度：{', '.join(active_dims)}\n"
                f"- 分配 Partner 数：{partner_count}\n\n"
                f"⚠️ 执行器未配置，任务未分发。"
            )
            response_type = "pending"
            aggregation_result = None
            clarification_result = None

        elif execution_result.phase == ExecutionPhase.COMPLETED:
            # 所有 Partner 完成，调用 Aggregator (LLM-6) 整合结果
            aggregation_result = await self._aggregate_results(
                session=session,
                task_text=task_text,
                execution_result=execution_result,
                planning_result=planning_result,
            )

            response_text = aggregation_result.text
            response_type = "final"
            clarification_result = None

        elif execution_result.phase == ExecutionPhase.AWAITING_INPUT:
            # 有 Partner 等待用户输入，调用 LLM-3 合并反问
            aggregation_result = None
            clarification_result = None

            try:
                # 调用 ClarificationMerger (LLM-3) 合并多个 Partner 的反问
                clarification_result = await self._handle_awaiting_input(
                    session=session,
                    execution_result=execution_result,
                    planning_result=planning_result,
                    user_query=task_text,
                )
                response_text = clarification_result.question_text
                response_type = "clarification"

            except Exception as e:
                # 降级处理：直接拼接原始问题
                logger.error(f"ClarificationMerger failed: {e}", exc_info=True)
                response_text = (
                    f"任务执行中，有 {len(execution_result.awaiting_input_partners)} 个 Partner 需要更多信息：\n\n"
                )
                for data_item in execution_result.questions_for_user:
                    if hasattr(data_item, "text"):
                        response_text += f"❓ {data_item.text}\n"
                response_type = "clarification"

        elif execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
            # 有 Partner 等待确认完成 - 理论上后台执行器已处理，这里做降级整合
            # 仍然调用 Aggregator 整合已有结果
            aggregation_result = await self._aggregate_results(
                session=session,
                task_text=task_text,
                execution_result=execution_result,
                planning_result=planning_result,
            )

            response_text = aggregation_result.text
            response_type = "final"
            clarification_result = None

        elif execution_result.phase == ExecutionPhase.FAILED:
            # 执行失败
            aggregation_result = None
            clarification_result = None
            failed_count = len(execution_result.failed_partners)
            response_text = (
                f"⚠️ 任务执行部分失败\n\n"
                f"- 失败的 Partner 数：{failed_count}\n"
                f"- 错误信息：{execution_result.error or '未知错误'}\n"
            )
            response_type = "final"

        else:
            # 其他状态（超时等）
            aggregation_result = None
            clarification_result = None
            response_text = f"任务执行状态：{execution_result.phase.value}\n\n{execution_result.error or ''}"
            response_type = "pending"

        # 映射 response_type 到 ResponseType 枚举
        response_type_enum = {
            "final": ResponseType.FINAL,
            "clarification": ResponseType.CLARIFICATION,
            "pending": ResponseType.PENDING,
        }.get(response_type, ResponseType.PENDING)

        # 记录对话轮次
        self._session_manager.add_dialog_turn(
            session_id=session.session_id,
            user_query=task_text,  # 使用任务指令作为 user_query
            intent_type=IntentType.TASK_NEW,
            response_type=response_type_enum,
            response_summary=response_text[:100],
        )

        return self._build_submit_response(session, active_task_id)


# 模块级工厂函数
def create_orchestrator(
    session_manager: SessionManager,
    scenario_loader: ScenarioLoader,
    intent_analyzer: IntentAnalyzer,
    planner: Planner | None = None,
    executor: TaskExecutor | None = None,
    group_executor: GroupTaskExecutor | None = None,
    group_manager: GroupManager | None = None,
    clarification_merger: ClarificationMerger | None = None,
    input_router: InputRouter | None = None,
    completion_gate: CompletionGate | None = None,
    aggregator: Aggregator | None = None,
    history_compressor: HistoryCompressor | None = None,
    leader_aic: str | None = None,
) -> Orchestrator:
    """创建编排器实例。"""
    return Orchestrator(
        session_manager=session_manager,
        scenario_loader=scenario_loader,
        intent_analyzer=intent_analyzer,
        planner=planner,
        executor=executor,
        group_executor=group_executor,
        group_manager=group_manager,
        clarification_merger=clarification_merger,
        input_router=input_router,
        completion_gate=completion_gate,
        aggregator=aggregator,
        history_compressor=history_compressor,
        leader_aic=leader_aic,
    )
