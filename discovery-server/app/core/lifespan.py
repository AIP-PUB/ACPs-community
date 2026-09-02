"""应用生命周期与后台任务装配。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.core.config import settings
from app.core.database import close_db, get_async_session_context
from app.core.logging_config import get_logger
from app.discovery.semantic_matcher import SemanticAgentMatcher
from app.discovery.semantic_matcher_holder import set_matcher
from app.discovery.service import start_health_check_task, stop_health_check_task
from app.heartbeat_sync.exception import AliveSyncError as AliveSyncTransportError
from app.heartbeat_sync.holder import clear_alive_reader, set_alive_reader
from app.heartbeat_sync.runtime import start_alive_sync, stop_alive_sync
from app.heartbeat_sync.store import PostgresAliveSyncStore
from app.sync.client import start_dsp_sync, stop_dsp_sync
from app.sync.exception import SyncOperationError
from app.sync.model import Agent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

logger = get_logger(__name__)

AVAILABLE_AGENTS_RUNTIME_TABLE = "available_agents_runtime"
SEMANTIC_MATCHER_STARTUP_ERRORS = (OSError, RuntimeError, ValueError, TypeError)
SEMANTIC_MATCHER_SHUTDOWN_ERRORS = (OSError, RuntimeError, ValueError, TypeError)
DSP_SYNC_SHUTDOWN_ERRORS = (SyncOperationError, OSError, RuntimeError, ValueError, TypeError)
FORWARDER_HEALTH_CHECK_SHUTDOWN_ERRORS = (OSError, RuntimeError, ValueError, TypeError)
ALIVE_SYNC_SHUTDOWN_ERRORS = (AliveSyncTransportError, OSError, RuntimeError, ValueError, TypeError)
POLLING_CYCLE_ERRORS = (httpx.HTTPError, SQLAlchemyError, KeyError, TypeError, ValueError)
TRUNCATE_AVAILABLE_AGENTS_RUNTIME_SQL = text("TRUNCATE TABLE available_agents_runtime")
INSERT_AVAILABLE_AGENTS_RUNTIME_SQL = text(
    """
    INSERT INTO available_agents_runtime (aic, is_available, checked_at)
    VALUES (:aic, :is_available, :checked_at)
    """
)


@dataclass(slots=True)
class BackgroundServiceStatus:
    """后台服务运行状态。"""

    running: bool = False
    last_error: str | None = None


@dataclass(slots=True)
class RuntimeServicesState:
    """运行时后台服务状态快照。"""

    semantic_matcher: BackgroundServiceStatus = field(default_factory=BackgroundServiceStatus)
    dsp_sync: BackgroundServiceStatus = field(default_factory=BackgroundServiceStatus)
    forwarder_health_check: BackgroundServiceStatus = field(default_factory=BackgroundServiceStatus)
    available_agents_polling: BackgroundServiceStatus = field(default_factory=BackgroundServiceStatus)
    alive_sync: BackgroundServiceStatus = field(default_factory=BackgroundServiceStatus)
    available_agents_last_updated: str = ""
    total_active_agents: int = 0
    available_agents_count: int = 0

    def snapshot(self) -> dict[str, object]:
        """返回适合序列化的状态字典。"""

        return asdict(self)


class SupportsRuntimeSnapshot(Protocol):
    """可导出运行时快照的对象协议。"""

    def snapshot(self) -> dict[str, object]: ...


class RuntimeCoordinator:
    """统一编排 discovery-server 的后台服务生命周期。"""

    def __init__(self) -> None:
        self.runtime_state = RuntimeServicesState()
        self._polling_task: asyncio.Task[None] | None = None
        self._semantic_matcher: SemanticAgentMatcher | None = None

    @staticmethod
    def _has_http_url(url: str) -> bool:
        normalized_url = url.strip()
        return normalized_url.startswith(("http://", "https://"))

    @staticmethod
    def _should_skip_gpu_matcher_in_testing(mode: str) -> bool:
        return (
            settings.APP_ENV == "testing"
            and mode == "gpu"
            and (not settings.EMBEDDING_MODEL_PATH.strip() or not settings.embedding_devices_list)
        )

    async def startup(self, app: FastAPI) -> None:
        """启动所有后台运行时服务。"""

        app.state.runtime_services = self.runtime_state
        self._start_semantic_matcher()
        await self._start_dsp_sync()
        self._start_forwarder_health_check()
        self._start_available_agents_polling()
        await self._start_alive_sync()

    async def shutdown(self) -> None:
        """停止所有后台运行时服务。"""

        await self._stop_alive_sync()
        await self._stop_semantic_matcher()
        await self._stop_dsp_sync()
        await self._stop_forwarder_health_check()
        await self._stop_available_agents_polling()

        try:
            await close_db()
            logger.info("数据库连接关闭成功")
        except SQLAlchemyError as exc:
            logger.exception("数据库连接关闭失败", error=str(exc))

    def _start_semantic_matcher(self) -> None:
        mode = (settings.DISCOVERY_MODE or "cpu").strip().lower()

        try:
            logger.info("开始初始化语义匹配器", mode=mode)

            if mode == "cpu":
                self._semantic_matcher = SemanticAgentMatcher(
                    mode=mode,
                    api_key=settings.EMBEDDING_API_KEY,
                    base_url=settings.EMBEDDING_BASE_URL,
                    model_name=settings.EMBEDDING_MODEL_NAME,
                    batch_size=settings.BGE_BATCH_SIZE,
                    max_wait_time=settings.BGE_MAX_WAIT_TIME,
                )
            else:
                if self._should_skip_gpu_matcher_in_testing(mode):
                    self._semantic_matcher = None
                    set_matcher(None)
                    self.runtime_state.semantic_matcher.running = False
                    self.runtime_state.semantic_matcher.last_error = None
                    logger.info(
                        "测试态缺少可用的 GPU embedding 配置，跳过语义匹配器启动",
                        mode=mode,
                        app_env=settings.APP_ENV,
                    )
                    return

                self._semantic_matcher = SemanticAgentMatcher(
                    mode=mode,
                    model_path=settings.EMBEDDING_MODEL_PATH,
                    devices=settings.embedding_devices_list,
                    reranker_url=settings.RERANKER_URL,
                    batch_size=settings.BGE_BATCH_SIZE,
                    max_wait_time=settings.BGE_MAX_WAIT_TIME,
                )

            logger.info("语义匹配器实例初始化完成，启动worker...")
            self._semantic_matcher.start_workers()
            set_matcher(self._semantic_matcher)
            self.runtime_state.semantic_matcher.running = True
            self.runtime_state.semantic_matcher.last_error = None

            logger.info("语义匹配器启动成功")
            if mode == "cpu":
                logger.info(
                    "语义匹配器配置",
                    mode=mode,
                    embedding_model=settings.EMBEDDING_MODEL_NAME,
                    embedding_base_url=settings.EMBEDDING_BASE_URL,
                    reranker_enabled=False,
                    batch_size=settings.BGE_BATCH_SIZE,
                    max_wait_time=settings.BGE_MAX_WAIT_TIME,
                )
            else:
                logger.info(
                    "语义匹配器配置",
                    mode=mode,
                    embedding_model_path=settings.EMBEDDING_MODEL_PATH,
                    embedding_devices=settings.EMBEDDING_DEVICES,
                    reranker_url=settings.RERANKER_URL or "未启用",
                    batch_size=settings.BGE_BATCH_SIZE,
                    max_wait_time=settings.BGE_MAX_WAIT_TIME,
                )
        except SEMANTIC_MATCHER_STARTUP_ERRORS as exc:
            self._semantic_matcher = None
            set_matcher(None)
            self.runtime_state.semantic_matcher.running = False
            self.runtime_state.semantic_matcher.last_error = str(exc)
            logger.exception("语义匹配器启动失败", error=str(exc))

    async def _stop_semantic_matcher(self) -> None:
        if self._semantic_matcher is None:
            self.runtime_state.semantic_matcher.running = False
            return

        try:
            await self._semantic_matcher.stop_workers()
            logger.info("语义匹配器停止成功")
            self.runtime_state.semantic_matcher.running = False
            self.runtime_state.semantic_matcher.last_error = None
        except SEMANTIC_MATCHER_SHUTDOWN_ERRORS as exc:
            self.runtime_state.semantic_matcher.last_error = str(exc)
            logger.exception("语义匹配器停止失败", error=str(exc))
        finally:
            self._semantic_matcher = None
            set_matcher(None)

    async def _start_dsp_sync(self) -> None:
        if not settings.DSP_AUTO_START:
            self.runtime_state.dsp_sync.running = False
            self.runtime_state.dsp_sync.last_error = None
            logger.info("DSP 自动启动已禁用")
            return

        if not self._has_http_url(settings.DSP_BASE_URL):
            self.runtime_state.dsp_sync.running = False
            self.runtime_state.dsp_sync.last_error = None
            logger.info("DSP 基础地址未配置，跳过后台同步启动")
            return

        try:
            await start_dsp_sync()
            logger.info("DSP 同步服务启动成功")
            logger.info("开始监控Registry数据变化...")
            self.runtime_state.dsp_sync.running = True
            self.runtime_state.dsp_sync.last_error = None
        except SyncOperationError as exc:
            self.runtime_state.dsp_sync.running = False
            self.runtime_state.dsp_sync.last_error = str(exc)
            logger.exception("DSP 同步服务启动失败", error=str(exc))

    async def _stop_dsp_sync(self) -> None:
        try:
            await stop_dsp_sync()
            logger.info("DSP 同步服务停止成功")
            self.runtime_state.dsp_sync.running = False
            self.runtime_state.dsp_sync.last_error = None
        except DSP_SYNC_SHUTDOWN_ERRORS as exc:
            self.runtime_state.dsp_sync.last_error = str(exc)
            logger.exception("DSP 同步服务停止失败", error=str(exc))

    def _start_forwarder_health_check(self) -> None:
        if not settings.FORWARDER_SERVER_ENABLED or not self._has_http_url(settings.FORWARDER_SERVER_URL):
            self.runtime_state.forwarder_health_check.running = False
            self.runtime_state.forwarder_health_check.last_error = None
            return

        try:
            start_health_check_task()
            logger.info("转发服务器健康检查任务启动成功")
            self.runtime_state.forwarder_health_check.running = True
            self.runtime_state.forwarder_health_check.last_error = None
        except RuntimeError as exc:
            self.runtime_state.forwarder_health_check.running = False
            self.runtime_state.forwarder_health_check.last_error = str(exc)
            logger.exception("转发服务器健康检查任务启动失败", error=str(exc))

    async def _stop_forwarder_health_check(self) -> None:
        try:
            await stop_health_check_task()
            logger.info("转发服务器健康检查任务停止成功")
            self.runtime_state.forwarder_health_check.running = False
            self.runtime_state.forwarder_health_check.last_error = None
        except FORWARDER_HEALTH_CHECK_SHUTDOWN_ERRORS as exc:
            self.runtime_state.forwarder_health_check.last_error = str(exc)
            logger.exception("转发服务器健康检查任务停止失败", error=str(exc))

    def _start_available_agents_polling(self) -> None:
        if not self._has_http_url(settings.POLLING_SERVER_URL):
            self.runtime_state.available_agents_polling.running = False
            self.runtime_state.available_agents_polling.last_error = None
            return

        if self._polling_task is not None and not self._polling_task.done():
            return

        self._polling_task = asyncio.create_task(self._poll_available_agents())
        self.runtime_state.available_agents_polling.running = True
        self.runtime_state.available_agents_polling.last_error = None
        logger.info("available-agents 轮询任务已启动（间隔5分钟）")

    async def _stop_available_agents_polling(self) -> None:
        if self._polling_task is None or self._polling_task.done():
            self.runtime_state.available_agents_polling.running = False
            return

        self._polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._polling_task

        self._polling_task = None
        self.runtime_state.available_agents_polling.running = False
        logger.info("available-agents 轮询任务已停止")

    async def _start_alive_sync(self) -> None:
        """启动 alive-sync 后台任务（满足所有守卫条件时）。"""
        if not settings.ALIVE_SYNC_ENABLED:
            self.runtime_state.alive_sync.running = False
            logger.info("alive-sync 未启用（ALIVE_SYNC_ENABLED=false），跳过")
            return
        if not settings.ALIVE_SYNC_AUTO_START:
            self.runtime_state.alive_sync.running = False
            logger.info("alive-sync 自动启动关闭（ALIVE_SYNC_AUTO_START=false），跳过")
            return
        if not self._has_http_url(settings.ALIVE_SYNC_PROVIDER_BASE_URL):
            self.runtime_state.alive_sync.running = False
            logger.info("alive-sync PROVIDER_BASE_URL 未配置，跳过")
            return
        if settings.APP_ENV == "testing":
            self.runtime_state.alive_sync.running = False
            logger.info("alive-sync 在测试态不自动拉起（APP_ENV=testing），跳过")
            return

        try:
            store = PostgresAliveSyncStore()
            set_alive_reader(store)
            await start_alive_sync(settings)
            self.runtime_state.alive_sync.running = True
            self.runtime_state.alive_sync.last_error = None
            logger.info("alive-sync 后台任务启动成功")
        except ALIVE_SYNC_SHUTDOWN_ERRORS as exc:
            clear_alive_reader()
            self.runtime_state.alive_sync.running = False
            self.runtime_state.alive_sync.last_error = str(exc)
            logger.exception("alive-sync 启动失败", error=str(exc))

    async def _stop_alive_sync(self) -> None:
        """停止 alive-sync 后台任务。"""
        try:
            await stop_alive_sync()
            logger.info("alive-sync 后台任务已停止")
        except ALIVE_SYNC_SHUTDOWN_ERRORS as exc:
            logger.warning("alive-sync 停止时出错（忽略）: %s", exc)
        finally:
            clear_alive_reader()
            self.runtime_state.alive_sync.running = False

    async def _poll_available_agents(self) -> None:
        while True:
            await self._run_polling_cycle()

            await asyncio.sleep(settings.POLLING_INTERVAL)

    async def _run_polling_cycle(self) -> None:
        try:
            logger.info("开始同步 available agents", source="polling_server")
            agents_urls = await self._load_active_agent_urls()

            logger.info("发送 active agents 到 polling server", active_agent_count=len(agents_urls))
            data = await self._fetch_polling_response(agents_urls)
            await self._apply_polling_result(agents_urls, data)

            logger.info(
                "同步 available agents 完成",
                available_count=data["available_count"],
                total_agents=data["total_agents"],
                duration_ms=data["duration_ms"],
            )
        except asyncio.CancelledError:
            raise
        except POLLING_CYCLE_ERRORS as exc:
            self.runtime_state.available_agents_polling.last_error = str(exc)
            logger.exception("同步 available agents 失败", error=str(exc))

    async def _load_active_agent_urls(self) -> dict[str, list[str]]:
        async with get_async_session_context() as session:
            result = await session.execute(select(Agent))
            agents = result.scalars().all()
            return {agent.aic: urls for agent in agents if (urls := self._extract_agent_urls(agent))}

    async def _fetch_polling_response(self, agents_urls: dict[str, list[str]]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{settings.POLLING_SERVER_URL}/check",
                json={"agents": agents_urls},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("polling response must be a JSON object")
            return cast("dict[str, Any]", payload)

    async def _apply_polling_result(self, agents_urls: dict[str, list[str]], data: dict[str, Any]) -> None:
        available_aics = data.get("available_aics", [])
        if not isinstance(available_aics, list):
            raise TypeError("available_aics must be a list")

        checked_at_raw = data.get("checked_at")
        checked_at = checked_at_raw if isinstance(checked_at_raw, str) else None
        available_set = {aic for aic in available_aics if isinstance(aic, str)}
        availability_map = {aic: (aic in available_set) for aic in agents_urls}

        await self._refresh_available_agents_runtime(availability_map, checked_at)

        self.runtime_state.available_agents_polling.running = True
        self.runtime_state.available_agents_polling.last_error = None
        self.runtime_state.available_agents_last_updated = self._parse_checked_at(checked_at).isoformat()
        self.runtime_state.total_active_agents = int(data.get("total_agents", 0))
        self.runtime_state.available_agents_count = int(data.get("available_count", 0))

    async def _refresh_available_agents_runtime(
        self,
        availability_map: dict[str, bool],
        checked_at: str | None,
    ) -> None:
        rows = [
            {
                "aic": aic,
                "is_available": is_available,
                "checked_at": self._parse_checked_at(checked_at),
            }
            for aic, is_available in availability_map.items()
        ]

        async with get_async_session_context() as session, session.begin():
            await session.execute(TRUNCATE_AVAILABLE_AGENTS_RUNTIME_SQL)
            if rows:
                await session.execute(INSERT_AVAILABLE_AGENTS_RUNTIME_SQL, rows)

    @staticmethod
    def _extract_agent_urls(agent: Agent) -> list[str]:
        acs = agent.acs or {}
        if acs.get("active") is not True:
            return []

        urls = [endpoint["url"] for endpoint in acs.get("endPoints", []) if endpoint.get("url")]
        if acs.get("webAppUrl"):
            urls.append(acs["webAppUrl"])

        return urls

    @staticmethod
    def _parse_checked_at(raw_value: str | None) -> datetime:
        if not raw_value:
            return datetime.now(UTC)
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except AttributeError, TypeError, ValueError:
            return datetime.now(UTC)


_runtime_coordinator = RuntimeCoordinator()


def get_runtime_services_snapshot(app: FastAPI) -> dict[str, object] | None:
    """获取挂载在应用上的运行时状态快照。"""

    runtime_services = getattr(app.state, "runtime_services", None)
    if runtime_services is None or not hasattr(runtime_services, "snapshot"):
        return None
    return cast("SupportsRuntimeSnapshot", runtime_services).snapshot()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan 入口。"""

    await _runtime_coordinator.startup(app)
    try:
        yield
    finally:
        await _runtime_coordinator.shutdown()
