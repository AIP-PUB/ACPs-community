"""
Leader Agent Platform - Session Manager

本模块负责 Session 的生命周期管理，包括：
- Session 创建与存储
- TTL 超时清理
- DialogContext 维护
- 事件日志记录
- 群组模式下的群组生命周期管理
"""

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..models import (
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_TTL_MINUTES,
    DialogContext,
    DialogTurn,
    EventLogEntry,
    EventLogType,
    ExecutionMode,
    IntentType,
    ResponseType,
    ScenarioRuntime,
    Session,
    SessionId,
    UserResult,
    UserResultType,
    generate_session_id,
    now_iso,
)

if TYPE_CHECKING:
    from .group_manager import GroupManager

logger = logging.getLogger(__name__)


class SessionManager:
 """
    Session 管理器。

    提供线程安全的 Session 存储和生命周期管理。
    使用内存存储，支持 TTL 自动清理。

    群组模式支持：
    - 创建群组模式 Session 时自动创建群组
    - Session 关闭/过期时自动解散群组

    注意：当前实现为单节点内存存储，
    生产环境应考虑使用 Redis 等分布式存储。
 """

    def __init__(
        self,
        ttl_minutes: int = SESSION_TTL_MINUTES,
        max_sessions: int = 10000,
        cleanup_interval_seconds: int = 60,
        group_manager: GroupManager | None = None,
    ):
 """
        初始化 Session 管理器。

        Args:
            ttl_minutes: Session 超时时间（分钟）
            max_sessions: 最大 Session 数量
            cleanup_interval_seconds: 清理任务间隔（秒）
            group_manager: 群组管理器（可选，用于群组模式）
 """
        self._sessions: OrderedDict[SessionId, Session] = OrderedDict
        self._request_cache: dict[str, str] = {}  # 幂等性缓存：{session_id:client_request_id -> hash}
        self._lock = threading.RLock
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_sessions = max_sessions
        self._cleanup_interval = cleanup_interval_seconds
        self._cleanup_task: asyncio.Task | None = None
        self._running = False
        self._group_manager: GroupManager | None = group_manager

    async def start(self) -> None:
 """启动后台清理任务。"""
        if self._running:
            return

        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop)
        logger.info("SessionManager started with TTL=%s minutes", self._ttl.total_seconds / 60)

    async def stop(self) -> None:
 """停止后台清理任务。"""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("SessionManager stopped")

    def set_group_manager(self, group_manager: GroupManager) -> None:
 """设置群组管理器（用于群组模式）"""
        self._group_manager = group_manager

    async def _cleanup_loop(self) -> None:
 """后台清理循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_expired
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Session cleanup error: {e}")

    async def _cleanup_expired(self) -> None:
 """清理过期的 Session。如果是群组模式，同时解散群组。"""
        now = datetime.now(UTC)
        expired_sessions = []

        with self._lock:
            for session_id, session in list(self._sessions.items):
                try:
                    touched_at = datetime.fromisoformat(session.touched_at.replace("Z", "+00:00"))
                    if now - touched_at > self._ttl:
                        expired_sessions.append(session)
                        del self._sessions[session_id]
                        logger.debug(f"Cleaned up expired session: {session_id}")
                except Exception:
                    continue

 # 解散群组（在锁外执行异步操作）
        for session in expired_sessions:
            if session.mode == ExecutionMode.GROUP and self._resolve_group_id(session) is not None:
                if self._group_manager:
                    try:
                        await self._group_manager.dissolve_group_for_session(session.session_id)
                        logger.info(f"Dissolved group for expired session: {session.session_id}")
                    except Exception as e:
                        logger.error(f"Failed to dissolve group for session {session.session_id}: {e}")

        if expired_sessions:
            logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")

    def create_session(
        self,
        mode: ExecutionMode,
        base_scenario: ScenarioRuntime,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        group_id: str | None = None,
        user_id: str | None = None,
        principal_issuer: str | None = None,
        principal_subject: str | None = None,
        principal_username: str | None = None,
        principal_email: str | None = None,
    ) -> Session:
 """
        创建新的 Session。

        Args:
            mode: 执行模式（direct_rpc 或 group）
            base_scenario: 基础场景配置
            ttl_seconds: TTL 秒数
            group_id: 群组 ID（群组模式时由调用者传入）
            user_id: 绑定到 session 的真人 principal_id（可选）

        Returns:
            新创建的 Session
 """
        session_id = generate_session_id
        now = now_iso
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat

        session = Session(
            session_id=session_id,
            mode=mode,
            user_id=user_id,
            principal_issuer=principal_issuer,
            principal_subject=principal_subject,
            principal_username=principal_username,
            principal_email=principal_email,
            created_at=now,
            updated_at=now,
            touched_at=now,
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            base_scenario=base_scenario,
            group_id=group_id,
            user_result=UserResult(
                type=UserResultType.PENDING,
                data_items=[],
                updated_at=now,
            ),
        )

        with self._lock:
 # 如果达到上限，移除最旧的 Session
            while len(self._sessions) >= self._max_sessions:
                oldest_id = next(iter(self._sessions))
                del self._sessions[oldest_id]
                logger.warning(f"Evicted oldest session due to capacity: {oldest_id}")

            self._sessions[session_id] = session

        logger.info(f"Created new session: {session_id}")
        return session

    def get_session(self, session_id: SessionId) -> Session | None:
 """
        获取 Session。

        Args:
            session_id: Session ID

        Returns:
            Session 或 None（如果不存在或已过期）
 """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None

 # 检查是否过期
            try:
                touched_at = datetime.fromisoformat(session.touched_at.replace("Z", "+00:00"))
                if datetime.now(UTC) - touched_at > self._ttl:
                    del self._sessions[session_id]
                    logger.debug(f"Session expired on access: {session_id}")
                    return None
            except Exception:
                pass

            return session

    def update_session(self, session: Session, refresh_ttl: bool = True) -> None:
 """
        更新 Session。

        自动更新 updated_at 和 touched_at 时间戳，并可选刷新 expiresAt。

        Args:
            session: 要更新的 Session
            refresh_ttl: 是否刷新 TTL（默认 True）
 """
        with self._lock:
            if session.session_id not in self._sessions:
                logger.warning(f"Attempted to update non-existent session: {session.session_id}")
                return

            now = now_iso
            session.updated_at = now
            session.touched_at = now

 # 刷新 expiresAt（/submit 被受理时必须刷新 expiresAt）
            if refresh_ttl:
                session.expires_at = (datetime.now(UTC) + timedelta(seconds=session.ttl_seconds)).isoformat

            self._sessions[session.session_id] = session

    async def delete_session(self, session_id: SessionId) -> bool:
 """
        删除 Session。如果是群组模式，同时解散群组。

        Args:
            session_id: Session ID

        Returns:
            是否成功删除
 """
        session = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info(f"Deleted session: {session_id}")

 # 如果是群组模式且有群组管理器，解散群组
        if session and session.mode == ExecutionMode.GROUP and self._resolve_group_id(session) is not None:
            if self._group_manager:
                try:
                    await self._group_manager.dissolve_group_for_session(session_id)
                    logger.info(f"Dissolved group for session: {session_id}")
                except Exception as e:
                    logger.error(f"Failed to dissolve group for session {session_id}: {e}")

        return session is not None

    def _resolve_group_id(self, session: Session) -> str | None:
 """解析 Session 绑定的 group_id，必要时回退到 GroupManager 运行态映射。"""
        if session.group_id:
            return session.group_id

        if self._group_manager:
            return self._group_manager.get_group_id(session.session_id)

        return None

    def get_group_manager(self):
 """返回当前注入的 GroupManager（如果有）。"""
        return self._group_manager

    def touch_session(self, session_id: SessionId) -> bool:
 """
        刷新 Session 的活跃时间。

        Args:
            session_id: Session ID

        Returns:
            是否成功更新
 """
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.touched_at = now_iso
                return True
            return False

    def add_dialog_turn(
        self,
        session_id: SessionId,
        user_query: str,
        intent_type: IntentType,
        response_type: ResponseType,
        response_summary: str | None = None,
    ) -> bool:
 """
        添加对话轮次到 Session。

        Args:
            session_id: Session ID
            user_query: 用户输入
            intent_type: 意图类型
            response_type: 响应类型
            response_summary: 响应摘要（可选）

        Returns:
            是否成功添加
 """
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False

            if not session.dialog_context:
                session.dialog_context = DialogContext(
                    session_id=session_id,
                    updated_at=now_iso,
                )

            turn = DialogTurn(
                user_query=user_query,
                intent_type=intent_type,
                response_type=response_type,
                response_summary=response_summary,
                timestamp=now_iso,
            )
            session.dialog_context.recent_turns.append(turn)
            session.dialog_context.updated_at = now_iso
            session.touched_at = now_iso

            return True

    def add_event_log(
        self,
        session_id: SessionId,
        event_type: EventLogType,
        payload: Any,
        active_task_id: str | None = None,
        partner_aic: str | None = None,
        aip_task_id: str | None = None,
    ) -> bool:
 """
        添加事件日志到 Session。

        Args:
            session_id: Session ID
            event_type: 事件类型
            payload: 事件负载
            active_task_id: 关联的 activeTaskId（可选）
            partner_aic: 关联的 partnerAic（可选）
            aip_task_id: 关联的 aipTaskId（可选）

        Returns:
            是否成功添加
 """
        import uuid

        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False

            entry = EventLogEntry(
                id=str(uuid.uuid4),
                created_at=now_iso,
                type=event_type,
                session_id=session_id,
                active_task_id=active_task_id,
                partner_aic=partner_aic,
                aip_task_id=aip_task_id,
                payload=payload,
            )
            session.event_log.append(entry)

            return True

    def get_session_count(self) -> int:
 """获取当前 Session 数量。"""
        with self._lock:
            return len(self._sessions)

    def list_sessions(
        self,
        limit: int = 100,
        filter_fn: Callable[[Session], bool] | None = None,
    ) -> list[Session]:
 """
        列出 Session。

        Args:
            limit: 最大返回数量
            filter_fn: 过滤函数（可选）

        Returns:
            Session 列表
 """
        with self._lock:
            sessions = list(self._sessions.values)

            if filter_fn:
                sessions = [s for s in sessions if filter_fn(s)]

            return sessions[:limit]

 # =========================================================================
 # 幂等性保护（缓存存储在 SessionManager 级别，不污染 Session 模型）
 # =========================================================================

    def check_request_idempotency(
        self,
        session_id: SessionId,
        client_request_id: str,
        request_hash: str,
    ) -> tuple[bool, str | None]:
 """
        检查请求的幂等性。

        Args:
            session_id: Session ID
            client_request_id: 客户端请求 ID
            request_hash: 请求载荷的哈希值

        Returns:
            (is_duplicate, cached_hash):
            - (False, None): 新请求，可以处理
            - (True, cached_hash): 重复请求
              - 如果 cached_hash == request_hash：幂等重试，返回缓存结果
              - 如果 cached_hash != request_hash：409003 错误
 """
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return (False, None)

 # 使用 SessionManager 级别的缓存
            cache_key = f"{session_id}:{client_request_id}"
            cached_hash = self._request_cache.get(cache_key)
            if cached_hash is None:
                return (False, None)

            return (True, cached_hash)

    def cache_request(
        self,
        session_id: SessionId,
        client_request_id: str,
        request_hash: str,
    ) -> bool:
 """
        缓存请求以支持幂等性。

        Args:
            session_id: Session ID
            client_request_id: 客户端请求 ID
            request_hash: 请求载荷的哈希值

        Returns:
            是否成功缓存
 """
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return False

 # 使用 SessionManager 级别的缓存
            cache_key = f"{session_id}:{client_request_id}"
            self._request_cache[cache_key] = request_hash
            return True


# 模块级单例
_session_manager: SessionManager | None = None


def get_session_manager -> SessionManager:
 """获取 Session 管理器单例。"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager
    return _session_manager
