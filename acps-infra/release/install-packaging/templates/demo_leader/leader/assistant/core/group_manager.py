"""
Leader Agent Platform - 群组管理器

本模块负责群组模式下的群组生命周期管理，包括：
- 群组创建与解散
- Partner 邀请与状态监控
- 与 SDK GroupLeader 的集成
"""

import asyncio
import logging
import ssl
from collections.abc import Callable
from typing import Any

from acps_sdk.aip.aip_base_model import (
    TaskState,
)
from acps_sdk.aip.aip_group_leader import (
    GroupLeader,
    GroupLeaderSession,
    PartnerInviteError,
    PartnerNetworkError,
    PartnerResponseError,
)
from acps_sdk.aip.aip_group_model import (
    ACSObject,
)
from acps_sdk.amp import MessageEmitter
from acps_sdk.amp.trace_context import TraceContext

from ..models.task import PartnerSelection

logger = logging.getLogger(__name__)

GROUP_CREATION_RETRY_DELAY_SECONDS = 5.0


# =============================================================================
# 配置类
# =============================================================================


class GroupConfig:
 """群组模式配置"""

    def __init__(
        self,
        enabled: bool = True,
        status_probe_interval: int = 30,
        max_wait_seconds: int = 300,
        partner_join_timeout: int = 60,
        max_retry_count: int = 3,
    ):
        self.enabled = enabled
        self.status_probe_interval = status_probe_interval
        self.max_wait_seconds = max_wait_seconds
        self.partner_join_timeout = partner_join_timeout
        self.max_retry_count = max_retry_count

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> GroupConfig:
 """从配置字典创建"""
        return cls(
            enabled=config.get("enabled", True),
            status_probe_interval=config.get("status_probe_interval", 30),
            max_wait_seconds=config.get("max_wait_seconds", 300),
            partner_join_timeout=config.get("partner_join_timeout", 60),
            max_retry_count=config.get("max_retry_count", 3),
        )


class RabbitMQConfig:
 """RabbitMQ 配置"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5671,
        user: str | None = None,
        password: str | None = None,
        vhost: str = "acps",
        auth_service_url: str | None = None,
        management_host: str | None = None,
        management_port: int | None = None,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.vhost = vhost
        self.auth_service_url = auth_service_url
        self.management_host = management_host or host
        self.management_port = management_port or 15672

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> RabbitMQConfig:
 """从配置字典创建"""
        management = config.get("management", {})
        return cls(
            host=config.get("host", "localhost"),
            port=config.get("port", 5671),
            user=config.get("user"),
            password=config.get("password"),
            vhost=config.get("vhost", "acps"),
            auth_service_url=config.get("auth_service_url"),
            management_host=management.get("host"),
            management_port=management.get("port"),
        )


# =============================================================================
# 消息处理回调类型
# =============================================================================

# 任务状态更新回调: (session_id, partner_aic, task_id, state, product_text, awaiting_prompt) -> None
TaskUpdateCallback = Callable[[str, str, str, TaskState, str | None, str | None], None]


# =============================================================================
# 群组管理器
# =============================================================================


class GroupManager:
 """
    群组管理器

    负责：
    - 创建和管理群组会话
    - 邀请 Partner 加入群组
    - 监控 Partner 状态
    - 处理群组消息
    - Session 关闭时解散群组
 """

    def __init__(
        self,
        leader_aic: str,
        rabbitmq_config: RabbitMQConfig,
        group_config: GroupConfig,
        ssl_context: ssl.SSLContext | None = None,
        identity_binding_enabled: bool = True,
        message_emitter: MessageEmitter | None = None,
        trace_context_provider: Callable[[], TraceContext | None] | None = None,
    ):
 """
        初始化群组管理器

        Args:
            leader_aic: Leader 的 AIC 标识
            rabbitmq_config: RabbitMQ 配置
            group_config: 群组模式配置
            ssl_context: 可选的 SSL 上下文，用于 mTLS 客户端连接
 """
        self.leader_aic = leader_aic
        self.rabbitmq_config = rabbitmq_config
        self.group_config = group_config
        self.ssl_context = ssl_context
        self.identity_binding_enabled = identity_binding_enabled
        self._message_emitter = message_emitter
        self._trace_context_provider = trace_context_provider

 # SDK GroupLeader 实例
        self._group_leader: GroupLeader | None = None

 # Session ID -> 群组 ID 映射
        self._session_group_map: dict[str, str] = {}

 # 任务状态更新回调
        self._task_update_callback: TaskUpdateCallback | None = None

 # 状态监控任务
        self._status_monitor_task: asyncio.Task | None = None

        self._started = False

    @property
    def is_enabled(self) -> bool:
 """群组模式是否启用"""
        return self.group_config.enabled

    async def start(self) -> None:
 """启动群组管理器"""
        if self._started:
            logger.debug("[GroupManager] Already started, skipping")
            return

        if not self.is_enabled:
            logger.info("[GroupManager] Disabled by config, skipping start")
            return

        logger.info("[GroupManager] Starting...")
        logger.debug(
            "[GroupManager] >>> Config: rabbitmq=%s:%d, vhost=%s, auth_service=%s",
            self.rabbitmq_config.host,
            self.rabbitmq_config.port,
            self.rabbitmq_config.vhost,
            self.rabbitmq_config.auth_service_url,
        )
        logger.debug(
            "[GroupManager] >>> Management API: %s:%d",
            self.rabbitmq_config.management_host or self.rabbitmq_config.host,
            self.rabbitmq_config.management_port or 15672,
        )
        logger.debug(
            "[GroupManager] >>> Group config: join_timeout=%ds, status_interval=%ds",
            self.group_config.partner_join_timeout,
            self.group_config.status_probe_interval,
        )

 # 创建 GroupLeader 实例
        self._group_leader = GroupLeader(
            leader_aic=self.leader_aic,
            rabbitmq_config={
                "host": self.rabbitmq_config.host,
                "port": self.rabbitmq_config.port,
                "user": self.rabbitmq_config.user,
                "password": self.rabbitmq_config.password,
                "vhost": self.rabbitmq_config.vhost,
                "auth_service_url": self.rabbitmq_config.auth_service_url,
            },
            ssl_context=self.ssl_context,
            invitation_timeout_seconds=self.group_config.partner_join_timeout,
            identity_binding_enabled=self.identity_binding_enabled,
            message_emitter=self._message_emitter,
            trace_context_provider=self._trace_context_provider,
        )

        self._started = True
        short_aic = self.leader_aic[-12:] if len(self.leader_aic) > 12 else self.leader_aic
        logger.info(
            "[GroupManager] Started successfully: leader_aic=...%s",
            short_aic,
        )

    async def stop(self) -> None:
 """停止群组管理器"""
        if not self._started:
            logger.debug("[GroupManager] Not started, skipping stop")
            return

        logger.info("[GroupManager] Stopping...")

 # 停止状态监控
        if self._status_monitor_task:
            logger.debug("[GroupManager] >>> Cancelling status monitor task...")
            self._status_monitor_task.cancel
            try:
                await self._status_monitor_task
            except asyncio.CancelledError:
                pass
            self._status_monitor_task = None
            logger.debug("[GroupManager] >>> Status monitor task cancelled")

 # 关闭 GroupLeader
        if self._group_leader:
            logger.debug("[GroupManager] >>> Closing GroupLeader...")
            await self._group_leader.close
            self._group_leader = None
            logger.debug("[GroupManager] >>> GroupLeader closed")

        session_count = len(self._session_group_map)
        self._session_group_map.clear
        self._started = False
        logger.info("[GroupManager] Stopped, cleared %d session mappings", session_count)

    def set_task_update_callback(self, callback: TaskUpdateCallback) -> None:
 """设置任务状态更新回调"""
        self._task_update_callback = callback

 # ------------------------------------------------------------------
 # 群组会话管理
 # ------------------------------------------------------------------

    async def create_group_for_session(self, session_id: str) -> str:
 """
        为 Session 创建群组

        Args:
            session_id: Session ID

        Returns:
            群组 ID
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id

        if not self._started or not self._group_leader:
            logger.error(
                "[Group:%s] Cannot create group - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        if session_id in self._session_group_map:
            existing_group = self._session_group_map[session_id]
            short_group = existing_group[-8:] if len(existing_group) > 8 else existing_group
            logger.debug(
                "[Group:%s] Group already exists: group_id=...%s",
                short_session,
                short_group,
            )
            return existing_group

        logger.info("[Group:%s] Creating new group...", short_session)

        max_attempts = max(1, self.group_config.max_retry_count)
        group_session: GroupLeaderSession | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                group_session = await self._group_leader.create_group_session(
                    session_id=session_id,
                    initial_partners=[],
                )
                break
            except Exception as e:
                if attempt >= max_attempts:
                    logger.error(
                        "[Group:%s] Group creation failed after %d attempt(s): %s",
                        short_session,
                        attempt,
                        str(e),
                    )
                    raise

                logger.warning(
                    "[Group:%s] Group creation attempt %d/%d failed: %s; retrying in %.1fs",
                    short_session,
                    attempt,
                    max_attempts,
                    str(e),
                    GROUP_CREATION_RETRY_DELAY_SECONDS,
                )
 # aio-pika 的 robust connection 默认在 5 秒后重连；等待一个周期后再重试建组。
                await asyncio.sleep(GROUP_CREATION_RETRY_DELAY_SECONDS)

        if group_session is None:
            raise RuntimeError(f"Failed to create group session: {session_id}")

        group_id = group_session.group_id
        self._session_group_map[session_id] = group_id
        short_group = group_id[-8:] if len(group_id) > 8 else group_id

 # 启动状态监控（如果还没启动）
        if self._status_monitor_task is None or self._status_monitor_task.done:
            logger.debug("[Group:%s] Starting status monitor task...", short_session)
            self._status_monitor_task = asyncio.create_task(self._status_monitor_loop)

        logger.info(
            "[Group:%s] Group created: group_id=...%s",
            short_session,
            short_group,
        )
        return group_id

    async def dissolve_group_for_session(self, session_id: str) -> None:
 """
        解散 Session 关联的群组

        Args:
            session_id: Session ID
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id

        if not self._group_leader:
            logger.debug("[Group:%s] Cannot dissolve - GroupLeader not available", short_session)
            return

        group_id = self._session_group_map.pop(session_id, None)
        if not group_id:
            logger.debug("[Group:%s] No group to dissolve", short_session)
            return

        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        logger.info("[Group:%s] Dissolving group_id=...%s", short_session, short_group)

        try:
            await self._group_leader.dissolve_group_session(session_id)
            logger.info(
                "[Group:%s] Group dissolved successfully: group_id=...%s",
                short_session,
                short_group,
            )
        except Exception as e:
            logger.warning(
                "[Group:%s] Failed to dissolve group: error=%s",
                short_session,
                str(e),
            )

    def get_group_id(self, session_id: str) -> str | None:
 """获取 Session 关联的群组 ID"""
        return self._session_group_map.get(session_id)

    def get_group_session(self, session_id: str) -> GroupLeaderSession | None:
 """获取群组会话对象"""
        if not self._group_leader:
            return None
        return self._group_leader.group_sessions.get(session_id)

    def get_group_runtime(self, session_id: str) -> dict[str, Any]:
 """获取群组运行态快照，用于运维和验收。"""
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        if not self._started or not self._group_leader:
            logger.error(
                "[Group:%s] Cannot inspect group runtime - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        runtime = self._group_leader.get_group_runtime(session_id)
        logger.debug(
            "[Group:%s] Runtime snapshot: state=%s, connected=%d/%d, pending=%d",
            short_session,
            runtime["state"],
            runtime["connected_members"],
            runtime["total_members"],
            len(runtime["pending_invitations"]),
        )
        return runtime

 # ------------------------------------------------------------------
 # Partner 邀请
 # ------------------------------------------------------------------

    async def invite_partners(
        self,
        session_id: str,
        partner_selections: list[PartnerSelection],
    ) -> dict[str, bool]:
 """
        邀请 Partner 列表加入群组

        Args:
            session_id: Session ID
            partner_selections: Partner 选择列表

        Returns:
            Dict[partner_aic, success]: 邀请结果
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id

        if not self._group_leader:
            logger.error("[Group:%s] Cannot invite - GroupManager not started", short_session)
            raise RuntimeError("GroupManager not started")

        if session_id not in self._session_group_map:
            logger.error("[Group:%s] No group found for session", short_session)
            raise ValueError(f"No group found for session: {session_id}")

        logger.info(
            "[Group:%s] >>> Inviting %d partner(s) to group...",
            short_session,
            len(partner_selections),
        )

        results: dict[str, bool] = {}
        start_time = asyncio.get_event_loop.time

        async def invite_one(i: int, selection: PartnerSelection) -> tuple[str, bool]:
            partner_aic = selection.partner_aic
            short_aic = partner_aic[-8:] if len(partner_aic) > 8 else partner_aic
            rpc_url = self._extract_rpc_url(selection)
            if not rpc_url and not selection.acs_data:
                logger.warning(
                    "[Group:%s] [Partner:...%s] No invitation endpoint data, skipping invitation (%d/%d)",
                    short_session,
                    short_aic,
                    i,
                    len(partner_selections),
                )
                return partner_aic, False

            logger.debug(
                "[Group:%s] [Partner:...%s] Sending invitation (rpc_fallback=%s) (%d/%d)",
                short_session,
                short_aic,
                rpc_url,
                i,
                len(partner_selections),
            )

 # 构造 ACS 对象
            partner_acs = ACSObject(aic=partner_aic)

 # 邀请 Partner（带重试）
            success = await self._invite_partner_with_retry(
                session_id=session_id,
                partner_acs=partner_acs,
                rpc_url=rpc_url,
                partner_acs_data=selection.acs_data,
            )

            return partner_aic, success

 # inbox 邀请会等待 Partner 完成 join ack；必须并发发出，避免首个 invite 阻塞后续成员。
        invite_results = await asyncio.gather(
            *(invite_one(i, selection) for i, selection in enumerate(partner_selections, 1))
        )
        results = dict(invite_results)

        elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
        success_count = sum(1 for v in results.values if v)
        logger.info(
            "[Group:%s] <<< Invitation completed: %d/%d succeeded, elapsed=%.0fms",
            short_session,
            success_count,
            len(partner_selections),
            elapsed_ms,
        )

        return results

    async def request_partner_leave(self, session_id: str, partner_aic: str) -> None:
 """请求指定 Partner 优雅退出当前群组。"""
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_partner = partner_aic[-8:] if len(partner_aic) > 8 else partner_aic
        if not self._group_leader:
            logger.error(
                "[Group:%s] Cannot request leave - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        logger.info(
            "[Group:%s] Requesting graceful leave for partner ...%s",
            short_session,
            short_partner,
        )
        await self._group_leader.request_partner_leave(
            partner_aic=partner_aic,
            session_id=session_id,
        )

    async def force_remove_partner(self, session_id: str, partner_aic: str) -> dict[str, Any]:
 """强制移除指定 Partner，并执行队列/ACL/连接清理。"""
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_partner = partner_aic[-8:] if len(partner_aic) > 8 else partner_aic
        if not self._group_leader:
            logger.error(
                "[Group:%s] Cannot force remove - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        logger.info(
            "[Group:%s] Force removing partner ...%s",
            short_session,
            short_partner,
        )
        result = await self._group_leader.force_remove_partner(
            partner_aic=partner_aic,
            session_id=session_id,
        )
        logger.info(
            "[Group:%s] Force removed partner ...%s (queue_deleted=%s)",
            short_session,
            short_partner,
            result["queue_deleted"],
        )
        return result

    async def _invite_partner_with_retry(
        self,
        session_id: str,
        partner_acs: ACSObject,
        rpc_url: str | None,
        partner_acs_data: dict[str, Any] | None = None,
    ) -> bool:
 """带重试的 Partner 邀请"""
        max_retries = self.group_config.max_retry_count
 # timeout 参数暂时未使用，预留给后续超时控制
        _ = self.group_config.partner_join_timeout

        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_aic = partner_acs.aic[-8:] if len(partner_acs.aic) > 8 else partner_acs.aic
        group_leader = self._group_leader
        if group_leader is None:
            raise RuntimeError("GroupManager not started")

        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(
                    "[Group:%s] [Partner:...%s] Invite attempt %d/%d...",
                    short_session,
                    short_aic,
                    attempt,
                    max_retries,
                )
                start_time = asyncio.get_event_loop.time

                await group_leader.invite_partner(
                    session_id=session_id,
                    partner_acs=partner_acs,
                    partner_rpc_url=rpc_url,
                    partner_acs_data=partner_acs_data,
                )

                elapsed_ms = (asyncio.get_event_loop.time - start_time) * 1000
                logger.info(
                    "[Group:%s] [Partner:...%s] Invite SUCCESS: elapsed=%.0fms",
                    short_session,
                    short_aic,
                    elapsed_ms,
                )
                return True

            except PartnerNetworkError as e:
                logger.warning(
                    "[Group:%s] [Partner:...%s] Network error (attempt %d/%d): %s",
                    short_session,
                    short_aic,
                    attempt,
                    max_retries,
                    str(e)[:100],
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)  # 简单退避

            except PartnerResponseError as e:
                logger.error(
                    "[Group:%s] [Partner:...%s] Invite REFUSED: %s",
                    short_session,
                    short_aic,
                    str(e)[:100],
                )
                return False  # Partner 明确拒绝，不重试

            except PartnerInviteError as e:
                logger.error(
                    "[Group:%s] [Partner:...%s] Invite FAILED: %s",
                    short_session,
                    short_aic,
                    str(e)[:100],
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)

        logger.error(
            "[Group:%s] [Partner:...%s] Invite FAILED after %d attempts",
            short_session,
            short_aic,
            max_retries,
        )
        return False

    def _extract_rpc_url(self, selection: PartnerSelection) -> str | None:
 """从 PartnerSelection 提取群组 RPC URL

        群组 RPC 端点与直连 RPC 端点的约定：
        - 直连端点: /rpc
        - 群组端点: /group/rpc

        本方法将 ACS 中的直连端点转换为群组端点。
 """
 # 尝试从 ACS 数据中获取
        acs_data = selection.acs_data
        if not acs_data:
            logger.debug("[Group] No ACS data for partner: %s", selection.partner_aic[-8:])
            return None

        endpoints = acs_data.get("endPoints", [])
        rpc_url = None

        for ep in endpoints:
            url = ep.get("url")
            transport = ep.get("transport", "").upper
            if url and transport in ("HTTP", "JSONRPC"):
                rpc_url = url
                break

 # 如果没有找到，尝试第一个端点
        if not rpc_url and endpoints:
            rpc_url = endpoints[0].get("url")

        if not rpc_url:
            logger.debug("[Group] No endpoints for partner: %s", selection.partner_aic[-8:])
            return None

 # 将直连 RPC URL 转换为群组 RPC URL
 # 例如: /rpc -> /group/rpc
        if rpc_url.endswith("/rpc"):
            group_url = rpc_url[:-4] + "/group/rpc"
        else:
 # 如果不是标准格式，直接追加 /group/rpc
            group_url = rpc_url.rstrip("/") + "/group/rpc"

        logger.debug(
            "[Group] Converted RPC URL for partner: %s -> %s (from %s)",
            selection.partner_aic[-8:],
            group_url,
            rpc_url,
        )
        return group_url

 # ------------------------------------------------------------------
 # 任务控制
 # ------------------------------------------------------------------

    async def start_task(
        self,
        session_id: str,
        task_id: str,
        task_content: str,
        target_partners: list[str] | None = None,
    ) -> None:
 """
        在群组中启动任务

        Args:
            session_id: Session ID
            task_id: AIP 任务 ID
            task_content: 任务内容
            target_partners: 目标 Partner 列表（mentions）
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_task = task_id[-16:] if len(task_id) > 16 else task_id
        content_preview = task_content[:80] + "..." if len(task_content) > 80 else task_content

        if not self._group_leader:
            logger.error("[Group:%s] Cannot start task - GroupManager not started", short_session)
            raise RuntimeError("GroupManager not started")

        if target_partners:
            target_list = [f"...{t[-8:]}" for t in target_partners]
            target_info = f"targets={target_list}"
        else:
            target_info = "broadcast"
        logger.info(
            "[Group:%s] START task: task_id=...%s, %s",
            short_session,
            short_task,
            target_info,
        )
        logger.debug(
            "[Group:%s] START content: %s",
            short_session,
            content_preview.replace("\n", " "),
        )

        await self._group_leader.start_task(
            session_id=session_id,
            task_content=task_content,
            task_id=task_id,
            target_partners=target_partners,
        )

        logger.debug("[Group:%s] START task sent successfully", short_session)

    async def continue_task(
        self,
        session_id: str,
        task_id: str,
        content: str,
        target_partner: str | None = None,
    ) -> None:
 """
        继续任务（发送 continue 命令）

        Args:
            session_id: Session ID
            task_id: AIP 任务 ID
            content: 补充内容
            target_partner: 目标 Partner（如果只针对一个）
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_task = task_id[-16:] if len(task_id) > 16 else task_id
        content_preview = content[:80] + "..." if len(content) > 80 else content

        if not self._group_leader:
            logger.error(
                "[Group:%s] Cannot continue task - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        target_info = f"target=...{target_partner[-8:]}" if target_partner else "broadcast"
        logger.info(
            "[Group:%s] CONTINUE task: task_id=...%s, %s",
            short_session,
            short_task,
            target_info,
        )
        logger.debug(
            "[Group:%s] CONTINUE content: %s",
            short_session,
            content_preview.replace("\n", " "),
        )

        await self._group_leader.continue_task(
            session_id=session_id,
            task_id=task_id,
            content=content,
            target_partner=target_partner,
        )

        logger.debug("[Group:%s] CONTINUE task sent successfully", short_session)

    async def complete_task(
        self,
        session_id: str,
        task_id: str,
        target_partner: str | None = None,
    ) -> None:
 """
        完成任务（发送 complete 命令）

        Args:
            session_id: Session ID
            task_id: AIP 任务 ID
            target_partner: 目标 Partner（如果只针对一个）
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_task = task_id[-16:] if len(task_id) > 16 else task_id

        if not self._group_leader:
            logger.error(
                "[Group:%s] Cannot complete task - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        target_info = f"target=...{target_partner[-8:]}" if target_partner else "broadcast"
        logger.info(
            "[Group:%s] COMPLETE task: task_id=...%s, %s",
            short_session,
            short_task,
            target_info,
        )

        await self._group_leader.complete_task(
            session_id=session_id,
            task_id=task_id,
            target_partner=target_partner,
        )

        logger.debug("[Group:%s] COMPLETE task sent successfully", short_session)

    async def cancel_task(
        self,
        session_id: str,
        task_id: str,
        reason: str | None = None,
        target_partner: str | None = None,
    ) -> None:
 """
        取消任务

        Args:
            session_id: Session ID
            task_id: AIP 任务 ID
            reason: 取消原因
            target_partner: 目标 Partner（如果只针对一个）
 """
        short_session = session_id[-8:] if len(session_id) > 8 else session_id
        short_task = task_id[-16:] if len(task_id) > 16 else task_id

        if not self._group_leader:
            logger.error(
                "[Group:%s] Cannot cancel task - GroupManager not started",
                short_session,
            )
            raise RuntimeError("GroupManager not started")

        target_info = f"target=...{target_partner[-8:]}" if target_partner else "broadcast"
        reason_info = f"reason={reason[:50]}..." if reason and len(reason) > 50 else f"reason={reason}"
        logger.info(
            "[Group:%s] CANCEL task: task_id=...%s, %s, %s",
            short_session,
            short_task,
            target_info,
            reason_info,
        )

        await self._group_leader.cancel_task(
            session_id=session_id,
            task_id=task_id,
            reason=reason,
            target_partner=target_partner,
        )

        logger.debug("[Group:%s] CANCEL task sent successfully", short_session)

 # ------------------------------------------------------------------
 # 状态查询
 # ------------------------------------------------------------------

    def get_partner_states(self, session_id: str, task_id: str) -> dict[str, TaskState]:
 """
        获取群组中所有 Partner 的任务状态

        Args:
            session_id: Session ID
            task_id: 任务 ID

        Returns:
            Dict[partner_aic, TaskState]
 """
        group_session = self.get_group_session(session_id)
        if not group_session:
            return {}

        states = group_session.task_states.get(task_id, {})
        if states:
            short_session = session_id[-8:] if len(session_id) > 8 else session_id
            state_summary = ", ".join(f"...{k[-8:]}={v.name}" for k, v in states.items)
            logger.debug("[Group:%s] Partner states for task: %s", short_session, state_summary)
        return states

    def get_partner_products(self, session_id: str, task_id: str) -> dict[str, str]:
 """
        获取群组中所有 Partner 的产出物文本

        Args:
            session_id: Session ID
            task_id: 任务 ID

        Returns:
            Dict[partner_aic, product_text]
 """
        group_session = self.get_group_session(session_id)
        if not group_session:
            return {}

        products = group_session.task_products.get(task_id, {})
        if products:
            short_session = session_id[-8:] if len(session_id) > 8 else session_id
            product_summary = ", ".join(f"...{k[-8:]}={len(v)}chars" for k, v in products.items)
            logger.debug(
                "[Group:%s] Partner products for task: %s",
                short_session,
                product_summary,
            )
        return products

    def get_partner_prompts(self, session_id: str, task_id: str) -> dict[str, str | None]:
 """
        获取群组中所有 Partner 的 AwaitingInput 提示

        Args:
            session_id: Session ID
            task_id: 任务 ID

        Returns:
            Dict[partner_aic, prompt]
 """
        group_session = self.get_group_session(session_id)
        if not group_session:
            return {}

        prompts = group_session.task_prompts.get(task_id, {})
        if prompts:
            short_session = session_id[-8:] if len(session_id) > 8 else session_id
            prompt_summary = ", ".join(f"...{k[-8:]}={len(v or '')}chars" for k, v in prompts.items)
            logger.debug("[Group:%s] Partner prompts for task: %s", short_session, prompt_summary)
        return prompts

    def get_task_summary(self, session_id: str, task_id: str) -> dict[str, Any]:
 """获取任务摘要"""
        group_session = self.get_group_session(session_id)
        if not group_session:
            return {}

        summary = group_session.get_task_summary(task_id)
        if summary:
            short_session = session_id[-8:] if len(session_id) > 8 else session_id
            logger.debug(
                "[Group:%s] Task summary: partners=%d, completed=%d, failed=%d",
                short_session,
                summary.get("total_partners", 0),
                summary.get("completed", 0),
                summary.get("failed", 0),
            )
        return summary

 # ------------------------------------------------------------------
 # 状态监控
 # ------------------------------------------------------------------

    async def _status_monitor_loop(self) -> None:
 """定期检查 Partner 状态"""
        interval = self.group_config.status_probe_interval
        logger.info("[GroupManager] Status monitor started: interval=%ds", interval)

        check_count = 0
        while True:
            try:
                await asyncio.sleep(interval)
                check_count += 1
                logger.debug(
                    "[GroupManager] Status check #%d, sessions=%d",
                    check_count,
                    len(self._session_group_map),
                )
                await self._check_all_partners_status
            except asyncio.CancelledError:
                logger.info("[GroupManager] Status monitor stopped after %d checks", check_count)
                break
            except Exception as e:
                logger.error("[GroupManager] Status monitor error: %s", str(e))
                await asyncio.sleep(10)  # 出错后短暂等待

    async def _check_all_partners_status(self) -> None:
 """检查所有会话中的 Partner 状态"""
        if not self._group_leader:
            return

        for session_id in list(self._session_group_map.keys):
            short_session = session_id[-8:] if len(session_id) > 8 else session_id
            try:
                logger.debug("[Group:%s] Checking partner status...", short_session)
                await self._group_leader.check_partner_status(
                    partner_aic=None,  # 检查所有
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning(
                    "[Group:%s] Failed to check partner status: %s",
                    short_session,
                    str(e)[:100],
                )


# =============================================================================
# 工厂函数
# =============================================================================

_group_manager: GroupManager | None = None


def create_group_manager(
    leader_aic: str,
    rabbitmq_config: dict[str, Any] | RabbitMQConfig,
    group_config: dict[str, Any] | GroupConfig,
    ssl_context: ssl.SSLContext | None = None,
    identity_binding_enabled: bool = True,
    message_emitter: MessageEmitter | None = None,
    trace_context_provider: Callable[[], TraceContext | None] | None = None,
) -> GroupManager:
 """
    创建群组管理器

    Args:
        leader_aic: Leader 的 AIC
        rabbitmq_config: RabbitMQ 配置（字典或 RabbitMQConfig 对象）
        group_config: 群组模式配置（字典或 GroupConfig 对象）
        ssl_context: 可选的 SSL 上下文，用于 mTLS 客户端连接

    Returns:
        GroupManager 实例
 """
    global _group_manager

    logger.info("[GroupManager] Creating group manager...")
    logger.debug(
        "[GroupManager] leader_aic=%s",
        leader_aic[:16] + "..." if len(leader_aic) > 16 else leader_aic,
    )

 # 处理 rabbitmq_config
    if isinstance(rabbitmq_config, dict):
        logger.debug(
            "[GroupManager] rabbitmq_config=%s",
            {k: v for k, v in rabbitmq_config.items if k != "password"},
        )
        rmq_config = RabbitMQConfig.from_dict(rabbitmq_config)
    else:
        logger.debug(
            "[GroupManager] rabbitmq_config: host=%s:%d",
            rabbitmq_config.host,
            rabbitmq_config.port,
        )
        rmq_config = rabbitmq_config

 # 处理 group_config
    if isinstance(group_config, dict):
        logger.debug("[GroupManager] group_config=%s", group_config)
        grp_config = GroupConfig.from_dict(group_config)
    else:
        logger.debug("[GroupManager] group_config: enabled=%s", group_config.enabled)
        grp_config = group_config

    _group_manager = GroupManager(
        leader_aic=leader_aic,
        rabbitmq_config=rmq_config,
        group_config=grp_config,
        ssl_context=ssl_context,
        identity_binding_enabled=identity_binding_enabled,
        message_emitter=message_emitter,
        trace_context_provider=trace_context_provider,
    )

    logger.info("[GroupManager] Group manager created")
    return _group_manager


def get_group_manager -> GroupManager | None:
 """获取群组管理器单例"""
    return _group_manager
