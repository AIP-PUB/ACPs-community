"""
Leader Agent Platform - API Routes

本模块实现 HTTP API 端点：
- POST /submit: 提交用户输入
- GET /result/{session_id}: 获取任务结果（支持异步执行模式）
- GET /result/{session_id}/{task_id}: 获取指定任务的执行结果
- GET /log/{session_id}: 获取事件日志
- POST /cancel/{session_id}: 取消任务

异步执行模式：/submit 在任务编排之后就返回，后续的任务执行和结果返回是异步的。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from acps_sdk.aip.aip_group_runtime import normalize_group_id
from acps_sdk.amp import AuditAction, AuditActor, AuditBody, AuditResult, AuditTarget
from acps_sdk.oidc import HumanPrincipal, audit_actor_from_principal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from leader.assistant.amp_setup import LEADER_EMITTER

from ..auth import (
    get_request_principal,
    issue_stream_token,
    oidc_enabled,
    require_leader_user,
    validate_stream_token,
)
from ..core.task_execution_manager import (
    TaskExecutionManager,
    get_task_execution_manager,
)
from ..models import ActiveTaskId, ExecutionMode, SessionClosedReason, SessionId
from ..models.exceptions import (
    ActiveTaskMismatchError,
    DuplicateRequestError,
    LeaderAgentError,
    LLMCallError,
    LLMParseError,
    ModeMismatchError,
    SessionClosedError,
    SessionExpiredError,
    SessionNotFoundError,
)
from ..security import bind_session_principal, can_manage_group, ensure_session_owner
from .schemas import (
    CancelRequest,
    CancelResponse,
    CancelResult,
    CommonError,
    GroupMemberActionResponse,
    GroupMemberActionResult,
    GroupRuntimeResponse,
    GroupRuntimeView,
    LeaderResult,
    LogResponse,
    LogResult,
    ResultResponse,
    ScenarioRuntimeView,
    StreamTokenResponse,
    StreamTokenResult,
    SubmitRequest,
    SubmitResponse,
)

# 使用 TYPE_CHECKING 避免循环导入
if TYPE_CHECKING:
    from ..core import Orchestrator, SessionManager

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/api/v1", tags=["leader"])

# 全局组件引用（由 main.py 注入）
_orchestrator: Any | None = None
_session_manager: Any | None = None
_task_execution_manager: TaskExecutionManager | None = None


def init_routes(orchestrator: Orchestrator, session_manager: SessionManager) -> None:
 """
    初始化路由组件。

    Args:
        orchestrator: 主编排器
        session_manager: Session 管理器
 """
    global _orchestrator, _session_manager, _task_execution_manager
    _orchestrator = orchestrator
    _session_manager = session_manager
    _task_execution_manager = get_task_execution_manager


def _get_orchestrator -> Orchestrator:
 """获取编排器实例。"""
    if _orchestrator is None:
        raise RuntimeError("Routes not initialized. Call init_routes first.")
    return _orchestrator


def _get_session_manager -> SessionManager:
 """获取 Session 管理器实例。"""
    if _session_manager is None:
        raise RuntimeError("Routes not initialized. Call init_routes first.")
    return _session_manager


def _get_task_execution_manager -> TaskExecutionManager:
 """获取任务执行管理器实例。"""
    global _task_execution_manager
    if _task_execution_manager is None:
        _task_execution_manager = get_task_execution_manager
    return _task_execution_manager


def _get_group_manager -> Any:
 """获取群组管理器实例。"""
    session_manager = _get_session_manager
    group_manager = session_manager.get_group_manager
    if group_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": 503001, "message": "Group manager not available"},
        )
    return group_manager


def _session_not_found(session_id: SessionId) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "SESSION_NOT_FOUND",
            "message": f"Session not found: {session_id}",
        },
    )


def _get_session_or_404(session_id: SessionId) -> Any:
    session = _get_session_manager.get_session(session_id)
    if not session:
        raise _session_not_found(session_id)
    return session


def _ensure_session_access(
    session: Any,
    principal: HumanPrincipal | None,
    *,
    allow_operator: bool = False,
) -> None:
    if not oidc_enabled:
        return
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    ensure_session_owner(session, principal, allow_operator=allow_operator)


def _ensure_group_action(session: Any, principal: HumanPrincipal | None) -> None:
    if not oidc_enabled:
        return
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if can_manage_group(principal) or getattr(session, "user_id", None) == principal.principal_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You are not allowed to manage this group",
    )


def _derive_session_group_id(session: Any) -> str | None:
 """基于 Session ID 派生稳定的 group_id，避免依赖运行态回写时序。"""
    mode = getattr(session, "mode", None)
    mode_value = getattr(mode, "value", mode)
    if mode_value != ExecutionMode.GROUP.value:
        return None

    session_id = getattr(session, "session_id", None)
    if not session_id:
        return None

    try:
        return normalize_group_id(f"group-{session_id}")
    except ValueError:
        return None


def _resolve_session_group_id(session: Any) -> str | None:
 """解析 Session 的 group_id，必要时回退到 GroupManager 运行态映射。"""
    if session.group_id:
        return session.group_id

    try:
        group_manager = _get_group_manager
    except HTTPException, RuntimeError:
        group_id = _derive_session_group_id(session)
        if group_id:
            session.group_id = group_id
        return group_id

    group_id = group_manager.get_group_id(session.session_id)
    if group_id:
        session.group_id = group_id

    if group_id:
        return group_id

    group_id = _derive_session_group_id(session)
    if group_id:
        session.group_id = group_id

    return group_id


def _session_to_leader_result(session: Any) -> LeaderResult:
 """
    将 Session 转换为 LeaderResult（API 响应视图）。

    LeaderResult 是 Session 的公开视图，不包含 eventLog 和 prompts 等内部配置。
 """
 # 转换 baseScenario
    base_scenario_view = ScenarioRuntimeView(
        id=session.base_scenario.id,
        kind=session.base_scenario.kind,
        version=session.base_scenario.version,
        loaded_at=session.base_scenario.loaded_at,
        source_path=session.base_scenario.source_path,
        config_digest=session.base_scenario.config_digest,
    )

 # 转换 expertScenario（如果存在）
    expert_scenario_view = None
    if session.expert_scenario:
        expert_scenario_view = ScenarioRuntimeView(
            id=session.expert_scenario.id,
            kind=session.expert_scenario.kind,
            version=session.expert_scenario.version,
            loaded_at=session.expert_scenario.loaded_at,
            source_path=session.expert_scenario.source_path,
            config_digest=session.expert_scenario.config_digest,
        )

    return LeaderResult(
        session_id=session.session_id,
        mode=session.mode,
        user_id=session.user_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        touched_at=session.touched_at,
        ttl_seconds=session.ttl_seconds,
        expires_at=session.expires_at,
        closed=session.closed,
        closed_at=session.closed_at,
        closed_reason=session.closed_reason.value if session.closed_reason else None,
        group_id=_resolve_session_group_id(session),
        group_routing=None,  # 脱敏，不暴露内部路由信息
        base_scenario=base_scenario_view,
        expert_scenario=expert_scenario_view,
        scenario_briefs=session.scenario_briefs,
        active_task=session.active_task,
        partners=session.partners,
        user_context=session.user_context,
        dialog_context=session.dialog_context,
        user_result=session.user_result,
    )


# =============================================================================
# POST /submit - 提交用户输入
# =============================================================================


@router.post(
    "/submit",
    response_model=SubmitResponse,
    responses={
        400: {"model": CommonError, "description": "请求参数错误"},
        404: {"model": CommonError, "description": "Session 不存在"},
        500: {"model": CommonError, "description": "服务器内部错误"},
    },
    summary="提交用户输入",
    description="提交用户输入到 Leader Agent，触发意图分析和任务处理。",
)
async def submit(
    request: SubmitRequest,
    principal: HumanPrincipal | None = Depends(require_leader_user),
) -> SubmitResponse:
 """
    提交用户输入。

    流程：
    1. 获取或创建 Session（含 mode/幂等性/activeTaskId 校验）
    2. 调用 LLM-1 分析意图
    3. 根据意图类型执行对应处理
    4. 返回响应
 """
    try:
        orchestrator = _get_orchestrator
        if oidc_enabled and principal is not None:
            request.user_id = principal.principal_id
            if request.session_id:
                session = _get_session_manager.get_session(request.session_id)
                if session is not None:
                    _ensure_session_access(session, principal, allow_operator=False)
                    bind_session_principal(session, principal)

        response = await orchestrator.handle_submit(request, principal=principal)
        return response

    except SessionNotFoundError as e:
        logger.warning(f"Session not found: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 404001, "message": str(e)},
        )

    except SessionExpiredError as e:
        logger.warning(f"Session expired: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 404001, "message": str(e)},
        )

    except SessionClosedError as e:
        logger.warning(f"Session closed: {e}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=e.to_dict,
        )

    except ModeMismatchError as e:
        logger.warning(f"Mode mismatch: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.to_dict,
        )

    except ActiveTaskMismatchError as e:
        logger.warning(f"Active task mismatch: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.to_dict,
        )

    except DuplicateRequestError as e:
        logger.warning(f"Duplicate request with different payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.to_dict,
        )

    except LLMCallError as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "LLM_CALL_ERROR", "message": str(e)},
        )

    except LLMParseError as e:
        logger.error(f"LLM parse failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "LLM_PARSE_ERROR", "message": str(e)},
        )

    except LeaderAgentError as e:
        logger.error(f"Leader agent error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "LEADER_ERROR", "message": str(e)},
        )

    except Exception as e:
        logger.exception(f"Unexpected error in submit: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
        )


# =============================================================================
# GET /result/{session_id} - 获取任务结果（异步执行模式）
# =============================================================================


@router.get(
    "/result/{session_id}",
    response_model=ResultResponse,
    responses={
        404: {"model": CommonError, "description": "Session 不存在"},
    },
    summary="获取任务结果",
 description="""
获取指定 Session 的当前任务结果。

**异步执行模式说明**：
- /submit 在任务编排之后就返回 pending 状态
- 客户端通过此 API 轮询获取任务执行状态和最终结果
- 支持通过 task_id 查询参数指定查询某个特定任务

**返回状态说明**：
- `pending`: 任务已创建，等待执行
- `running`: 任务正在执行中
- `awaiting_input`: 需要用户补充信息
- `completed`: 任务执行完成
- `failed`: 任务执行失败
- `cancelled`: 任务已取消
""",
)
async def get_result(
    session_id: SessionId,
    task_id: ActiveTaskId | None = Query(
        default=None,
        alias="taskId",
        description="可选的任务 ID，不指定则返回最新任务的状态",
    ),
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> ResultResponse:
 """
    获取任务结果（支持异步执行模式）。

    返回 LeaderResult 视图，包含当前 Session 的状态和结果。

    响应格式:
    {
        "result": LeaderResult | null,
        "error": CommonError | null
    }
 """
    try:
        session = _get_session_or_404(session_id)
        _ensure_session_access(session, principal, allow_operator=True)

 # 将 Session 转换为 LeaderResult 视图
        leader_result = _session_to_leader_result(session)

        return ResultResponse(result=leader_result)

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Error getting result: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
        )


# =============================================================================
# GET/POST/DELETE /group/... - 群组运行态与成员控制
# =============================================================================


@router.get(
    "/group/{session_id}",
    response_model=GroupRuntimeResponse,
    responses={
        404: {"model": CommonError, "description": "Session 或群组不存在"},
        503: {"model": CommonError, "description": "Group Manager 未就绪"},
    },
    summary="获取群组运行态",
    description="返回当前 session 对应群组的成员连接状态、待确认邀请和运行态快照。",
)
async def get_group_runtime(
    session_id: SessionId,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> GroupRuntimeResponse:
    try:
        session = _get_session_or_404(session_id)
        _ensure_session_access(session, principal, allow_operator=True)
        group_manager = _get_group_manager
        runtime = group_manager.get_group_runtime(session_id)
        return GroupRuntimeResponse(result=GroupRuntimeView(**runtime))
    except ValueError as e:
        logger.warning(f"Group runtime not found for session {session_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 404002, "message": str(e)},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in get_group_runtime for session {session_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": 500201, "message": "获取群组运行态失败"},
        )


@router.post(
    "/group/{session_id}/members/{partner_aic}/leave",
    response_model=GroupMemberActionResponse,
    responses={
        404: {"model": CommonError, "description": "Session 或群组不存在"},
        503: {"model": CommonError, "description": "Group Manager 未就绪"},
    },
    summary="请求成员优雅退出",
    description="通过群组管理命令请求指定 Partner 优雅退出当前群组。",
)
async def request_group_member_leave(
    session_id: SessionId,
    partner_aic: str,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> GroupMemberActionResponse:
    try:
        session = _get_session_or_404(session_id)
        _ensure_group_action(session, principal)
        group_manager = _get_group_manager
        runtime = group_manager.get_group_runtime(session_id)
        await group_manager.request_partner_leave(session_id, partner_aic)
        return GroupMemberActionResponse(
            result=GroupMemberActionResult(
                sessionId=session_id,
                groupId=runtime["group_id"],
                partnerAic=partner_aic,
                action="request-leave",
                acceptedAt=datetime.now(UTC).isoformat,
            )
        )
    except ValueError as e:
        logger.warning(f"Cannot request group member leave for session {session_id}, partner {partner_aic}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 404002, "message": str(e)},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Unexpected error requesting group member leave for session {session_id}, partner {partner_aic}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": 500202, "message": "请求成员退出群组失败"},
        )


@router.delete(
    "/group/{session_id}/members/{partner_aic}",
    response_model=GroupMemberActionResponse,
    responses={
        404: {"model": CommonError, "description": "Session 或群组不存在"},
        503: {"model": CommonError, "description": "Group Manager 未就绪"},
    },
    summary="强制移除群组成员",
 description="按 AIP v2.1.0 §6.4 执行成员强制移除：清理 ACL、队列与连接，并保留群组运行。",
)
async def force_remove_group_member(
    session_id: SessionId,
    partner_aic: str,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> GroupMemberActionResponse:
    try:
        session = _get_session_or_404(session_id)
        _ensure_group_action(session, principal)
        group_manager = _get_group_manager
        result = await group_manager.force_remove_partner(session_id, partner_aic)
        return GroupMemberActionResponse(
            result=GroupMemberActionResult(
                sessionId=result["session_id"],
                groupId=result["group_id"],
                partnerAic=result["partner_aic"],
                action="force-remove",
                acceptedAt=datetime.now(UTC).isoformat,
                queueDeleted=result["queue_deleted"],
            )
        )
    except ValueError as e:
        logger.warning(f"Cannot force remove group member for session {session_id}, partner {partner_aic}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 404002, "message": str(e)},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Unexpected error forcing group member removal for session {session_id}, partner {partner_aic}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": 500203, "message": "强制移除群组成员失败"},
        )


# =============================================================================
# GET /log/{session_id} - 获取事件日志
# =============================================================================


@router.get(
    "/log/{session_id}",
    response_model=LogResponse,
    responses={
        404: {"model": CommonError, "description": "Session 不存在"},
    },
    summary="获取事件日志",
    description="获取指定 Session 的事件日志。",
)
async def get_log(
    session_id: SessionId,
    limit: int = 100,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> LogResponse:
 """
    获取事件日志。

    返回 Session 的事件日志列表。
 """
    try:
        session = _get_session_or_404(session_id)
        _ensure_session_access(session, principal, allow_operator=True)

 # 获取事件日志
        events = session.event_log[-limit:] if limit > 0 else session.event_log

        has_more = len(session.event_log) > len(events)
        return LogResponse(
            result=LogResult(
                sessionId=session_id,
                items=events,
                nextCursor=None,
                hasMore=has_more,
            )
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Error getting log: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
        )


# =============================================================================
# POST /cancel/{session_id} - 取消任务
# =============================================================================


@router.post(
    "/cancel/{session_id}",
    response_model=CancelResponse,
    responses={
        404: {"model": CommonError, "description": "Session 不存在"},
    },
    summary="取消任务",
    description="取消指定 Session 的当前活跃任务。",
)
async def cancel_task(
    session_id: SessionId,
    request: CancelRequest | None = None,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> CancelResponse:
 """
    取消任务。

    取消当前活跃任务，并将 Session 设置为已关闭状态。
    /cancel 成功后 Session 将进入关闭态（closed=true）。
 """
    try:
        session_manager = _get_session_manager
        session = _get_session_or_404(session_id)
        _ensure_session_access(session, principal, allow_operator=True)

 # 取消活跃任务并尝试通知 Partner
        cancelled_tasks = []
        if session.active_task:
            cancelled_tasks.append(session.active_task.active_task_id)

 # 尽最大努力终止该 Session 关联的在途推进
 # 向各非终态 Partner 下发 AIP cancel
            try:
                orchestrator = _get_orchestrator
                if orchestrator and hasattr(orchestrator, "_executor"):
                    from ..models.aip import TaskState

                    for (
                        partner_aic,
                        partner_task,
                    ) in session.active_task.partner_tasks.items:
 # 只对非终态的 Partner 发送 cancel
                        terminal_states = {
                            TaskState.Completed,
                            TaskState.Failed,
                            TaskState.Rejected,
                            TaskState.Canceled,
                        }
                        if partner_task.state not in terminal_states:
 # 获取端点
                            partner_runtime = session.partners.get(partner_aic)
                            if partner_runtime and partner_runtime.resolved_endpoint:
                                endpoint = partner_runtime.resolved_endpoint.url
                                try:
                                    await orchestrator._executor.cancel_partner(
                                        session_id=session_id,
                                        partner_aic=partner_aic,
                                        aip_task_id=partner_task.aip_task_id,
                                        endpoint=endpoint,
                                    )
                                    logger.info(f"Sent cancel to partner {partner_aic}")
                                except Exception as cancel_err:
 # 记录警告但不阻塞取消流程
                                    logger.warning(f"Failed to cancel partner {partner_aic}: {cancel_err}")
            except Exception as e:
                logger.warning(f"Error during partner cancellation: {e}")

            session.active_task = None

 # /cancel 成功后 Session 进入关闭态
        now_iso_str = datetime.now(UTC).isoformat
        session.closed = True
        session.closed_at = now_iso_str
        session.closed_reason = SessionClosedReason.USER_CANCEL

 # A3: 审计——用户取消任务
        try:
            actor = (
                audit_actor_from_principal(principal)
                if principal is not None
                else AuditActor(
                    id=getattr(request, "user_id", None) or "anonymous",
                    type="human",
                )
            )
            await LEADER_EMITTER.emit(
                AuditBody(
                    actor=actor,
                    action=AuditAction(name="cancel_task", type="task_management"),
                    target=AuditTarget(type="session", id=session_id),
                    result=AuditResult(status="success"),
                ),
                trace_id=session_id,
            )
        except Exception:
            logger.warning("Audit emit A3 failed", exc_info=True)

 # 如果请求删除 Session
        if request and request.delete_session:
            session_deleted = await session_manager.delete_session(session_id)
        else:
            session_manager.update_session(session, refresh_ttl=False)
            session_deleted = False

        return CancelResponse(
            result=CancelResult(
                sessionId=session_id,
                success=True,
                cancelledTasks=cancelled_tasks,
                sessionDeleted=session_deleted,
                message=f"取消了 {len(cancelled_tasks)} 个任务，Session 已关闭",
                canceledAt=now_iso_str,
            )
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Error cancelling task: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
        )


@router.post(
    "/stream-token/{session_id}",
    response_model=StreamTokenResponse,
    responses={
        404: {"model": CommonError, "description": "Session 不存在"},
    },
    summary="签发短期 stream token",
    description="为浏览器流式订阅签发短期、仅绑定单个 session 的 stream token。",
)
async def create_stream_token(
    session_id: SessionId,
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> StreamTokenResponse:
    session = _get_session_or_404(session_id)
    _ensure_session_access(session, principal, allow_operator=True)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_at = issue_stream_token(
        session_id=session_id,
        principal=principal,
        allow_elevated_access=can_manage_group(principal)
        and getattr(session, "user_id", None) != principal.principal_id,
    )
    return StreamTokenResponse(
        result=StreamTokenResult(
            sessionId=session_id,
            streamToken=token,
            expiresAt=datetime.fromtimestamp(expires_at, tz=UTC).isoformat,
        )
    )


# =============================================================================
# GET /stream/{session_id} - 流式事件推送（SSE）
# =============================================================================

_SSE_HEARTBEAT_INTERVAL = 25  # 秒，防止代理层超时断连


@router.get(
    "/stream/{session_id}",
    response_class=StreamingResponse,
    responses={
        200: {"description": "SSE 流，每行格式 data: <json>\\n\\n"},
        404: {"model": CommonError, "description": "Session 不存在"},
    },
    summary="获取流式执行事件",
 description="""
订阅指定 Session 的实时 SSE 事件流。

适用于 Session 使用 **Streaming** 模式与 Partner 交互的场景：
当 AipExecutor 从 Partner 接收 SSE 事件时，会通过 StreamEventBus 实时推送给此端点的消费方。

**事件格式**：每条事件是 `data: <StreamResponse JSON>\\n\\n`
**心跳**：每 25s 发送一次 `: heartbeat\\n\\n` 防止连接超时
**结束信号**：`data: {"done": true}\\n\\n` 表示流已结束，客户端可主动断开
""",
)
async def stream_session_events(
    session_id: SessionId,
    request: Request,
    stream_token: str | None = Query(default=None, alias="streamToken"),
    principal: HumanPrincipal | None = Depends(get_request_principal),
) -> StreamingResponse:
 """订阅 Session 的 Streaming 事件流（Server-Sent Events）。"""
    session = _get_session_or_404(session_id)
    if oidc_enabled:
        if stream_token:
            record = validate_stream_token(session_id=session_id, token=stream_token)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid stream token",
                )
            if getattr(session, "user_id", None) != record.principal_id and not record.allow_elevated_access:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Stream token owner mismatch",
                )
        else:
            _ensure_session_access(session, principal, allow_operator=True)

    from ..core.stream_event_bus import get_stream_event_bus

    bus = get_stream_event_bus
    queue = bus.subscribe(session_id)

    async def _generate:
        try:
            while True:
                if await request.is_disconnected:
                    break
                try:
                    event = await asyncio.wait_for(queue.get, timeout=_SSE_HEARTBEAT_INTERVAL)
                except TimeoutError:
 # 发送心跳，保持连接
                    yield ": heartbeat\n\n"
                    continue

                if event is None:
 # 流结束哨兵 → 通知客户端关闭
                    yield 'data: {"done":true}\n\n'
                    break

                yield f"data: {event.model_dump_json(exclude_none=True)}\n\n"
        finally:
            bus.unsubscribe(session_id, queue)

    return StreamingResponse(
        _generate,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# 健康检查
# =============================================================================


@router.get(
    "/health",
    summary="健康检查",
    description="检查服务是否正常运行。",
)
async def health_check:
 """健康检查端点。"""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "components": {
            "orchestrator": _orchestrator is not None,
            "session_manager": _session_manager is not None,
        },
    }
