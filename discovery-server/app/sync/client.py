"""
DSP（Data Synchronization Protocol）客户端实现。

此模块实现从注册中心服务器同步数据的客户端逻辑。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx
from asyncpg import PostgresError  # type: ignore[import-untyped]
from fastapi import status
from openai import OpenAIError
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import delete, select

from app.core.config import settings
from app.core.database import build_database_url_summary, get_async_session_context
from app.core.logging_config import get_logger
from app.discovery.semantic_matcher_holder import get_matcher

from .exception import SyncError, SyncOperationError
from .model import (
    Agent,
    DSPState,
    Envelope,
    OperationType,
    RegistryInfo,
    Skill,
    SnapshotResponseHeader,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

NDJSON_MEDIA_TYPE = "application/x-ndjson"
SYNC_LOOP_ERRORS = (
    SyncOperationError,
    SQLAlchemyError,
    httpx.HTTPError,
    OpenAIError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)
type HttpQueryParamValue = str | int | float | bool | None
type HttpQueryParams = dict[str, HttpQueryParamValue]


class DSPClient:
    """
    用于从注册中心服务器同步数据的 DSP 客户端。
    """

    def __init__(
        self,
        registry_base_url: str,
        sync_interval: int = 30,
        target_types: list[str] | None = None,
    ) -> None:
        """
        初始化 DSP 客户端。

        Args:
            registry_base_url: 注册中心服务器的基础 URL
            sync_interval: 轮询变更的间隔时间（秒）
            target_types: 要同步的对象类型列表（默认：["acs"]）
        """
        self.registry_base_url = registry_base_url.rstrip("/")
        self.sync_interval = sync_interval
        self.target_types = target_types or ["acs"]

        # 客户端状态 - 将在 start_background_sync 时初始化
        self.state: DSPState | None = None
        self.is_running = False
        self._sync_task: asyncio.Task[None] | None = None
        self._force_full_snapshot = False

        # HTTP 客户端
        self.http_client: httpx.AsyncClient | None = None
        # 语义匹配器实例
        self.semantic_matcher = get_matcher()
        self._manual_sync_task: asyncio.Task[None] | None = None
        self._manual_sync_error: str | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端。"""
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
        return self.http_client

    async def close(self) -> None:
        """关闭 HTTP 客户端并清理资源。"""
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None

    def _build_url(self, endpoint: str) -> str:
        """为 DSP API 端点构建完整 URL。"""
        return urljoin(f"{self.registry_base_url}/", endpoint)

    def _require_state(self) -> DSPState:
        """返回已初始化的客户端状态。"""

        if self.state is None:
            raise RuntimeError("DSP state has not been initialized")
        return self.state

    def _mark_snapshot_required(self, *, force_full: bool = False) -> None:
        """标记下一轮同步必须执行快照。"""

        state = self._require_state()
        state.needs_snapshot = True
        if force_full:
            self._force_full_snapshot = True

    async def _load_state_from_db(self) -> DSPState:
        """按当前运行模式加载状态，并在缺少派生索引时强制全量快照。"""

        state = await DSPState.load_from_db(require_indexed_skills=get_matcher() is not None)
        if state.needs_snapshot and state.last_seq is not None:
            # 一旦确认本地派生索引异常，当前数据库内容就不能再作为可信基线继续增量追赶。
            # 此时必须回到 Registry 的 source of truth 做 full snapshot replace，重新同步最新
            # 数据并重建索引，而不是依赖本地现存数据或 `from_seq=<last_seq>` 的增量 snapshot。
            self._force_full_snapshot = True
        return state

    async def _clear_local_snapshot_data(self) -> int:
        """在全量快照前清理本地同步数据，保证快照具备替换语义。"""

        state = self._require_state()

        async with get_async_session_context() as session, session.begin():
            count_result = await session.execute(select(func.count()).select_from(Agent))
            deleted_count = int(count_result.scalar_one())
            await session.execute(delete(Skill))
            await session.execute(delete(Agent))

        state.object_versions.clear()

        logger.info("全量 Snapshot 前清理本地同步数据", deleted_agent_count=deleted_count)
        return deleted_count

    @staticmethod
    def _parse_int_header(response: httpx.Response, header_name: str, default: str) -> int:
        """解析响应头中的整数值。"""

        raw_value = response.headers.get(header_name, default)
        try:
            return int(raw_value)
        except (TypeError, ValueError) as exc:
            raise SyncOperationError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error_name=SyncError.INVALID_RESPONSE,
                error_msg=f"响应头 {header_name} 非法: {raw_value}",
                input_params={"header_name": header_name, "header_value": raw_value},
            ) from exc

    def _build_snapshot_response_header(self, response: httpx.Response) -> SnapshotResponseHeader:
        """从快照响应中解析快照头信息。"""

        return SnapshotResponseHeader(
            snapshot_id=response.headers.get("X-Snapshot-Id", ""),
            snapshot_seq=self._parse_int_header(response, "X-Snapshot-Seq", "0"),
            chunk_index=self._parse_int_header(response, "X-Snapshot-Chunk-Index", "0"),
            chunk_total=self._parse_int_header(response, "X-Snapshot-Chunk-Total", "1"),
            object_count=self._parse_int_header(response, "X-Snapshot-Object-Count", "0"),
        )

    def _collect_response_envelopes(
        self,
        response: httpx.Response,
        *,
        context: dict[str, object] | None = None,
    ) -> list[Envelope]:
        """将 NDJSON 响应体转换为 Envelope 列表。"""

        return [self._parse_envelope_line(line, context=context) for line in response.text.strip().split("\n") if line]

    def _update_last_seq_from_response(self, response: httpx.Response) -> int | None:
        """从 changes 响应头更新 last_seq。"""

        state = self._require_state()
        next_seq = response.headers.get("X-Next-Seq")
        if next_seq:
            state.last_seq = self._parse_int_header(response, "X-Next-Seq", next_seq)
        return state.last_seq

    @staticmethod
    def _raise_for_snapshot_status(
        response: httpx.Response,
        *,
        types: list[str],
        from_seq: int | None,
        limit: int,
        snapshot_id: str | None = None,
        chunk_index: int | None = None,
        url: str | None = None,
        params: HttpQueryParams | None = None,
    ) -> None:
        """校验快照响应状态码。"""

        if response.status_code == 200:
            return

        if chunk_index is None:
            raise SyncOperationError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error_name=SyncError.SNAPSHOT_FAIL,
                error_msg=f"快照请求失败: {response.status_code} {response.text}",
                input_params={"types": types, "from_seq": from_seq, "limit": limit},
            )

        logger.error("Chunk request failed", url=url, params=params, response_text=response.text)
        raise SyncOperationError(
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_name=SyncError.SNAPSHOT_FAIL,
            error_msg=f"Chunk {chunk_index} request failed: {response.status_code} - {response.text}",
            input_params={
                "chunk_index": chunk_index,
                "snapshot_id": snapshot_id,
                "url": url,
                "params": params,
            },
        )

    def _process_changes_response(
        self,
        response: httpx.Response,
        *,
        seq: int,
        types: list[str],
    ) -> list[Envelope]:
        """处理 changes 响应并返回解析后的 Envelope 列表。"""

        if response.status_code == 200:
            next_seq = self._update_last_seq_from_response(response)
            envelopes = self._collect_response_envelopes(response)
            logger.debug("Processed changes", envelope_count=len(envelopes), next_seq=next_seq)
            return envelopes

        if response.status_code == 204:
            next_seq = self._update_last_seq_from_response(response)
            logger.debug("No changes available", next_seq=next_seq or seq)
            return []

        if response.status_code == 410:
            logger.warning("Client has fallen behind retention window, snapshot needed")
            self._mark_snapshot_required(force_full=True)
            raise SyncOperationError(
                status_code=status.HTTP_409_CONFLICT,
                error_name=SyncError.RETENTION_WINDOW_EXCEEDED,
                error_msg="Client fallen behind retention window",
                input_params={"seq": seq, "types": types},
            )

        raise SyncOperationError(
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_name=SyncError.CHANGES_FAIL,
            error_msg=f"Changes request failed: {response.status_code} {response.text}",
            input_params={"seq": seq, "types": types},
        )

    @staticmethod
    def _parse_envelope_line(line: str, *, context: dict[str, object] | None = None) -> Envelope:
        """解析 NDJSON 单行并转换为 Envelope。"""

        try:
            envelope_data = json.loads(line)
            return Envelope(**envelope_data)
        except (TypeError, ValueError) as exc:
            input_params: dict[str, object] = {"line": line}
            if context:
                input_params.update(context)
            raise SyncOperationError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error_name=SyncError.INVALID_RESPONSE,
                error_msg=f"解析响应数据失败: {exc}",
                input_params=input_params,
            ) from exc

    async def get_registry_info(self) -> RegistryInfo | None:
        """获取注册中心服务器信息。"""
        try:
            client = self._get_http_client()
            url = self._build_url("info")

            logger.info("获取注册中心信息", url=url)
            response = await client.get(url, headers={"Accept": "application/json"})

            if response.status_code == 200:
                data = response.json()
                return RegistryInfo(**data)
            logger.warning("获取注册中心信息失败", status_code=response.status_code)
            return None

        except httpx.ConnectError as e:
            logger.exception("连接注册中心失败", error=str(e), registry_url=self.registry_base_url)
            raise SyncOperationError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"连接注册中心失败: {e}",
                input_params={"registry_url": self.registry_base_url},
            ) from e
        except httpx.TimeoutException as e:
            logger.exception("连接注册中心超时", error=str(e), registry_url=self.registry_base_url)
            raise SyncOperationError(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"连接注册中心超时: {e}",
                input_params={"registry_url": self.registry_base_url},
            ) from e
        except (TypeError, ValueError) as e:
            logger.exception("获取注册中心信息时出错", error=str(e), registry_url=self.registry_base_url)
            raise SyncOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=SyncError.REGISTRY_UNAVAILABLE,
                error_msg=f"获取注册中心信息时出错: {e}",
                input_params={"registry_url": self.registry_base_url},
            ) from e

    async def create_snapshot(
        self,
        types: list[str] | None = None,
        from_seq: int | None = None,
        limit: int = 10000,
    ) -> AsyncGenerator[Envelope]:
        """
        创建并获取快照数据。

        Args:
            types: 要同步的对象类型（默认为 self.target_types）
            from_seq: 增量快照的起始序列
            limit: 每个块的最大对象数量

        Yields:
            来自快照的 Envelope 对象
        """
        types = types or self.target_types

        try:
            client = self._get_http_client()
            state = self._require_state()

            # 构建快照请求 URL
            url = self._build_url("snapshots")
            params: HttpQueryParams = {"types": ",".join(types), "limit": limit}
            if from_seq:
                params["from_seq"] = from_seq

            logger.info("创建快照", types=types, from_seq=from_seq, limit=limit)

            # 请求第一个块
            response = await client.get(url, params=params, headers={"Accept": NDJSON_MEDIA_TYPE})
            self._raise_for_snapshot_status(response, types=types, from_seq=from_seq, limit=limit)

            # 从响应头解析快照元数据
            snapshot_info = self._build_snapshot_response_header(response)

            logger.info(
                "Snapshot created",
                snapshot_id=snapshot_info.snapshot_id,
                snapshot_seq=snapshot_info.snapshot_seq,
                chunk_total=snapshot_info.chunk_total,
                object_count=snapshot_info.object_count,
            )

            # Yield objects from first chunk
            for envelope in self._collect_response_envelopes(response):
                yield envelope

                # 如果有剩余的数据块则继续获取
            for chunk_index in range(1, snapshot_info.chunk_total):
                chunk_params: HttpQueryParams = {
                    "id": snapshot_info.snapshot_id,
                    "chunk": chunk_index,
                    "limit": limit,
                }

                logger.debug("Fetching snapshot chunk", chunk_index=chunk_index, chunk_total=snapshot_info.chunk_total)

                chunk_response = await client.get(url, params=chunk_params, headers={"Accept": NDJSON_MEDIA_TYPE})
                self._raise_for_snapshot_status(
                    chunk_response,
                    types=types,
                    from_seq=from_seq,
                    limit=limit,
                    snapshot_id=snapshot_info.snapshot_id,
                    chunk_index=chunk_index,
                    url=url,
                    params=chunk_params,
                )

                # Yield objects from chunk
                for envelope in self._collect_response_envelopes(
                    chunk_response,
                    context={"chunk_index": chunk_index},
                ):
                    yield envelope

            # 使用快照序列更新客户端状态
            state.last_seq = snapshot_info.snapshot_seq
            state.needs_snapshot = False

            logger.info("Snapshot sync completed", snapshot_seq=snapshot_info.snapshot_seq)

            # 在服务器上清理快照（可选）
            try:
                await client.delete(self._build_url(f"snapshots/{snapshot_info.snapshot_id}"))
            except httpx.HTTPError as e:
                logger.debug("Failed to cleanup snapshot", error=str(e), snapshot_id=snapshot_info.snapshot_id)

        except SyncOperationError:
            raise
        except httpx.ConnectError as e:
            raise SyncOperationError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"连接注册中心失败: {e}",
                input_params={"registry_url": self.registry_base_url},
            ) from e
        except httpx.TimeoutException as e:
            raise SyncOperationError(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"快照请求超时: {e}",
                input_params={"types": types, "from_seq": from_seq},
            ) from e
        except httpx.HTTPError as e:
            raise SyncOperationError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error_name=SyncError.SNAPSHOT_FAIL,
                error_msg=f"快照请求失败: {e}",
                input_params={"types": types, "from_seq": from_seq},
            ) from e

    async def get_changes(
        self,
        seq: int,
        types: list[str] | None = None,
        limit: int = 1000,
        wait: int | None = None,
    ) -> AsyncGenerator[Envelope]:
        """
        从注册中心获取增量变更。

        Args:
            seq: 起始序列号
            types: 要同步的对象类型（默认为 self.target_types）
            limit: 最大变更条目数
            wait: 长轮询等待时间（秒）

        Yields:
            来自变更流的 Envelope 对象
        """
        types = types or self.target_types

        try:
            client = self._get_http_client()

            url = self._build_url("changes")
            params: HttpQueryParams = {"types": ",".join(types), "seq": seq, "limit": limit}
            if wait:
                params["wait"] = f"{wait}s"

            logger.debug("Fetching changes", seq=seq)

            response = await client.get(url, params=params, headers={"Accept": NDJSON_MEDIA_TYPE})
            for envelope in self._process_changes_response(response, seq=seq, types=types):
                yield envelope

        except SyncOperationError:
            raise
        except httpx.ConnectError as e:
            raise SyncOperationError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"连接注册中心失败: {e}",
                input_params={"registry_url": self.registry_base_url},
            ) from e
        except httpx.TimeoutException as e:
            raise SyncOperationError(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                error_name=SyncError.CONNECTION_FAIL,
                error_msg=f"变更请求超时: {e}",
                input_params={"seq": seq, "types": types},
            ) from e
        except httpx.HTTPError as e:
            raise SyncOperationError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                error_name=SyncError.CHANGES_FAIL,
                error_msg=f"变更请求失败: {e}",
                input_params={"seq": seq, "types": types},
            ) from e

    def should_apply_envelope(self, envelope: Envelope) -> bool:
        """
        判断是否应应用该 envelope（幂等性检查）。

        Args:
            envelope: 待检查的 Envelope 对象

        Returns:
            如果应应用返回 True，否则返回 False
        """
        state = self._require_state()
        # 获取当前对象的版本
        current_version = state.object_versions.get(envelope.type, {}).get(envelope.id, 0)

        # 如果版本较新则应用
        return envelope.version > current_version

    async def _log_agent_visibility(self, *, stage: str, aic: str) -> None:
        """记录目标 agent 在当前数据库中的可见性，用于定位同步写入路径。"""

        async with get_async_session_context() as session:
            current_database = (await session.execute(select(func.current_database()))).scalar_one()
            agent_count = (
                await session.execute(select(func.count()).select_from(Agent).where(Agent.aic == aic))
            ).scalar_one()

        logger.debug(
            "Agent visibility probe",
            stage=stage,
            aic=aic,
            agent_count=int(agent_count),
            current_database=current_database,
            configured_database=build_database_url_summary(settings.DATABASE_URL),
        )

    async def _apply_to_database(self, envelope: Envelope) -> None:
        """
        将 envelope 的数据应用到数据库的内部函数。

        Args:
            envelope: 要应用的 Envelope 对象

        Raises:
            SyncException: 数据库操作失败时抛出
        """
        try:
            # 获取数据库会话
            async with get_async_session_context() as session:
                async with session.begin():
                    # 查找现有的 Agent 记录
                    stmt = select(Agent).where(Agent.aic == envelope.id)
                    result = await session.execute(stmt)
                    existing_agent = result.scalar_one_or_none()

                    if envelope.op == OperationType.DELETE:
                        # 处理删除操作
                        logger.debug(
                            "Applying DELETE",
                            envelope_type=envelope.type,
                            envelope_id=envelope.id,
                            envelope_version=envelope.version,
                        )

                        if existing_agent:
                            await session.delete(existing_agent)
                            logger.debug("Deleted agent from database", agent_id=envelope.id)
                        else:
                            logger.debug("Agent not found for deletion", agent_id=envelope.id)

                    else:
                        # 处理 upsert 操作（默认）
                        logger.debug(
                            "Applying UPSERT",
                            envelope_type=envelope.type,
                            envelope_id=envelope.id,
                            envelope_version=envelope.version,
                        )

                        if existing_agent:
                            # 更新现有记录
                            existing_agent.version = envelope.version
                            existing_agent.seq = envelope.seq
                            existing_agent.acs = envelope.payload
                            session.add(existing_agent)
                        else:
                            # 创建新记录
                            agent = Agent(
                                aic=envelope.id,
                                version=envelope.version,
                                seq=envelope.seq,
                                acs=envelope.payload,
                            )
                            session.add(agent)

                operation = envelope.op or "upsert"
                logger.debug(
                    "Applied envelope to database",
                    envelope_type=envelope.type,
                    envelope_id=envelope.id,
                    envelope_version=envelope.version,
                    operation=operation,
                )
                await self._log_agent_visibility(stage="after_apply", aic=envelope.id)

        except SQLAlchemyError as e:
            logger.exception(
                "Failed to apply envelope to database",
                envelope_type=envelope.type,
                envelope_id=envelope.id,
                envelope_version=envelope.version,
                error=str(e),
            )
            raise SyncOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=SyncError.DATABASE_ERROR,
                error_msg=f"数据库操作失败: {e}",
                input_params={
                    "envelope_type": envelope.type,
                    "envelope_id": envelope.id,
                    "envelope_version": envelope.version,
                },
            ) from e

    async def update_search_index(self, envelope: Envelope) -> None:
        """
        更新搜索索引。

        Args:
            envelope: 要处理的 Envelope 对象
        """
        try:
            # 仅处理acs类型的数据
            if envelope.type != "acs":
                return

            logger.info(
                "update_search_index 被调用",
                envelope_id=envelope.id,
                semantic_matcher_is_none=self.semantic_matcher is None,
            )
            # 检查语义匹配器是否可用
            if self.semantic_matcher is None:
                logger.warning("语义匹配器未初始化，跳过 embedding 更新", envelope_id=envelope.id)
                return
            # 处理创建/更新操作
            logger.info("开始处理语义索引", envelope_id=envelope.id)
            await self._handle_agent_upsert_semantic(envelope)
            logger.info("语义索引处理完成", envelope_id=envelope.id)
        except (OpenAIError, RuntimeError, ValueError, TypeError, SQLAlchemyError) as e:
            logger.error(
                "处理语义索引时出错",
                envelope_type=envelope.type,
                envelope_id=envelope.id,
                error=str(e),
            )

    async def _handle_agent_upsert_semantic(self, envelope: Envelope) -> None:
        """处理智能体创建/更新的语义索引"""
        semantic_matcher = self.semantic_matcher
        if semantic_matcher is None:
            return

        # 解析智能体数据
        agent_data = envelope.payload
        if not agent_data:
            logger.debug("智能体数据为空，跳过语义索引更新", agent_id=envelope.id)
            return

        # 确保agent_data包含AIC
        if "aic" not in agent_data:
            agent_data["aic"] = envelope.id

        # 添加版本和序列号信息用于缓存管理
        agent_data["version"] = envelope.version
        agent_data["seq"] = envelope.seq
        agent_data["lastModifiedTime"] = envelope.seq  # 使用seq作为修改时间标识

        # 更新语义索引
        await semantic_matcher._update_agent_index(agent_data)

        logger.debug("已更新智能体语义索引", agent_id=envelope.id)

    @staticmethod
    def _normalize_semantic_index_concurrency(value: int) -> int:
        return max(1, value)

    def sync_task_in_progress(self) -> bool:
        """返回手动触发的同步任务是否仍在执行。"""

        return self._manual_sync_task is not None and not self._manual_sync_task.done()

    def manual_sync_error(self) -> str | None:
        """返回最近一次手动同步任务的失败原因。"""

        return self._manual_sync_error

    def trigger_sync_once(self) -> bool:
        """异步触发一次手动同步；返回本次是否创建了新任务。"""

        if self.sync_task_in_progress():
            logger.info("Manual DSP sync is already running")
            return False

        self._manual_sync_error = None
        self._manual_sync_task = asyncio.create_task(self._run_manual_sync_once())
        self._manual_sync_task.add_done_callback(self._handle_manual_sync_done)
        logger.info("Manual DSP sync task started")
        return True

    async def _run_manual_sync_once(self) -> None:
        await self.sync_once()

    def _handle_manual_sync_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self._manual_sync_error = "Manual DSP sync task was cancelled"
            logger.warning("Manual DSP sync task was cancelled")
            return

        try:
            task.result()
        except SyncOperationError as exc:
            self._manual_sync_error = str(exc)
            logger.exception("Manual DSP sync task failed", error=str(exc))
        except SYNC_LOOP_ERRORS as exc:
            self._manual_sync_error = str(exc)
            logger.exception("Manual DSP sync task failed", error=str(exc))
        else:
            self._manual_sync_error = None
            logger.info("Manual DSP sync task completed")

    async def _update_search_indexes_concurrently(self, envelopes: list[Envelope]) -> None:
        """并发刷新 snapshot 中已入库 envelope 的语义索引。"""

        if not envelopes:
            return

        concurrency = self._normalize_semantic_index_concurrency(settings.DSP_SEMANTIC_INDEX_CONCURRENCY)
        semaphore = asyncio.Semaphore(concurrency)

        async def update_one(envelope: Envelope) -> None:
            async with semaphore:
                await self.update_search_index(envelope)
                await self._log_agent_visibility(stage="after_search_index", aic=envelope.id)

        logger.info(
            "Starting snapshot semantic index refresh",
            envelope_count=len(envelopes),
            concurrency=concurrency,
        )
        await asyncio.gather(*(update_one(envelope) for envelope in envelopes))
        logger.info("Snapshot semantic index refresh completed", envelope_count=len(envelopes))

    async def apply(self, envelope: Envelope, *, update_search_index: bool = True) -> bool:
        """
        将 envelope 的数据应用到本地状态和数据库。

        Args:
            envelope: 要应用的 Envelope 对象
            update_search_index: 是否在本次调用内刷新语义索引

        Returns:
            是否实际应用了该 envelope。
        """
        state = self._require_state()
        # 幂等性检查：跳过已经处理过的旧版本
        if not self.should_apply_envelope(envelope):
            current_version = state.object_versions.get(envelope.type, {}).get(envelope.id, 0)
            logger.debug(
                f"Skipping envelope {envelope.type}:{envelope.id} v{envelope.version} (current: v{current_version})"
            )
            return False

        # 类型过滤：仅处理支持的数据类型
        if envelope.type != "acs":
            logger.debug("Skipping non-acs type", envelope_type=envelope.type)
            return False

        # 确保对象版本追踪结构存在
        if envelope.type not in state.object_versions:
            state.object_versions[envelope.type] = {}

        # 执行数据库操作
        await self._apply_to_database(envelope)

        # 预留附加处理扩展点；当前在数据库写入成功后仅刷新搜索索引。
        if update_search_index:
            await self.update_search_index(envelope)
            await self._log_agent_visibility(stage="after_search_index", aic=envelope.id)

        # 更新内存中的版本追踪状态（仅在上述各个操作成功后执行）
        if envelope.op == OperationType.DELETE:
            # 对于删除操作，从版本跟踪中移除对象
            if envelope.id in state.object_versions[envelope.type]:
                del state.object_versions[envelope.type][envelope.id]
                logger.debug("Removed from version tracking", envelope_type=envelope.type, envelope_id=envelope.id)
        else:
            # 对于upsert操作，更新版本跟踪
            state.object_versions[envelope.type][envelope.id] = envelope.version

        return True

    async def sync_once(self) -> None:
        """
        执行一次同步循环。

        根据协议规范实现 DSP 的核心同步逻辑。
        """
        start_time = datetime.now(UTC)

        if self.state is None:
            self.state = await self._load_state_from_db()

        try:
            if self.state.needs_snapshot:
                logger.info("需要Snapshot同步 (初始同步或数据过期)")

                full_snapshot_required = self._force_full_snapshot or not self.state.last_seq
                from_seq = None if full_snapshot_required else self.state.last_seq

                if full_snapshot_required:
                    await self._clear_local_snapshot_data()

                applied_envelopes: list[Envelope] = []
                async for envelope in self.create_snapshot(
                    types=self.target_types,
                    from_seq=from_seq,
                    limit=settings.DSP_SNAPSHOT_CHUNK_SIZE,
                ):
                    if await self.apply(envelope, update_search_index=False):
                        applied_envelopes.append(envelope)

                await self._update_search_indexes_concurrently(applied_envelopes)

                self._force_full_snapshot = False

                logger.info("Snapshot sync completed", last_seq=self.state.last_seq)
                await self._log_snapshot_sync_result()
            else:
                # Incremental sync via changes API
                await self._sync_changes_continuously()

            # Update last sync time
            self.state.last_sync_time = start_time

        except SyncOperationError:
            raise
        except SQLAlchemyError as e:
            logger.exception("Sync error", error=str(e))
            raise SyncOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=SyncError.SYNC_FAIL,
                error_msg=f"同步失败: {e}",
                input_params={
                    "sync_interval": self.sync_interval,
                    "target_types": self.target_types,
                },
            ) from e

    async def _sync_changes_continuously(self) -> None:
        """
        连续同步变更数据，直到服务器返回 204 No Content 为止。
        """
        state = self._require_state()
        seq = state.last_seq or 0
        total_change_count = 0

        while True:
            try:
                change_count = 0
                has_changes = False

                async for envelope in self.get_changes(
                    seq=seq,
                    types=self.target_types,
                    limit=settings.DSP_CHANGES_CHUNK_SIZE,
                    wait=min(self.sync_interval, 20),  # Use long polling but cap at 20s
                ):
                    await self.apply(envelope)
                    change_count += 1
                    has_changes = True

                total_change_count += change_count

                if change_count > 0:
                    logger.debug("Processed changes in batch", change_count=change_count, seq=state.last_seq)

                # 如果没有变更数据，说明收到了 204，退出循环
                if not has_changes:
                    logger.debug("No more changes available", total_processed=total_change_count)
                    break

                # 更新序列号为下一次请求
                seq = state.last_seq or 0

            except SyncOperationError as e:
                if e.error_name == SyncError.RETENTION_WINDOW_EXCEEDED:
                    logger.info("数据保留窗口超期，切换到Snapshot同步")
                    self._mark_snapshot_required(force_full=True)
                    await self.sync_once()  # Retry with snapshot
                    return
                raise

        await self._log_changes_sync_result(total_change_count)

    async def _log_snapshot_sync_result(self) -> None:
        """记录 snapshot 同步完成后的汇总信息。"""

        try:
            async with get_async_session_context() as session:
                result = await session.execute(select(Agent))
                agents = result.scalars().all()
                logger.info("Snapshot 同步完成", agent_count=len(agents))
        except (OSError, PostgresError, SQLAlchemyError) as exc:
            logger.warning("Snapshot 同步完成，但无法统计 Agent 数量", error=str(exc))

    async def _log_changes_sync_result(self, total_change_count: int) -> None:
        """记录 changes 同步完成后的汇总信息。"""

        if total_change_count <= 0:
            logger.debug("Changes同步检查完成，无新数据")
            return

        try:
            async with get_async_session_context() as session:
                result = await session.execute(select(Agent))
                agents = result.scalars().all()
                logger.info("Changes 连续同步完成", total_change_count=total_change_count, agent_count=len(agents))
        except (OSError, PostgresError, SQLAlchemyError) as exc:
            logger.warning(
                "Changes 同步完成，但无法统计 Agent 数量",
                total_change_count=total_change_count,
                error=str(exc),
            )

    async def start_background_sync(self) -> None:
        """启动后台同步任务。"""
        if self.is_running:
            logger.warning("Background sync is already running")
            return

        # 从数据库加载同步状态
        self.state = await self._load_state_from_db()
        logger.info("Loaded sync state", last_seq=self.state.last_seq, needs_snapshot=self.state.needs_snapshot)

        self.is_running = True
        self._sync_task = asyncio.create_task(self._background_sync_loop())
        logger.info("Started DSP background sync", sync_interval=self.sync_interval)

    async def stop_background_sync(self) -> None:
        """停止后台同步任务。"""
        if not self.is_running:
            return

        self.is_running = False
        if self._sync_task:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None

        logger.info("Stopped DSP background sync")

    async def _background_sync_loop(self) -> None:
        """后台任务循环，用于定期执行同步。"""
        logger.info("Starting DSP sync loop", sync_interval=self.sync_interval)

        while self.is_running:
            try:
                await self.sync_once()

                # Wait for next sync interval
                await asyncio.sleep(self.sync_interval)

            except asyncio.CancelledError:
                logger.info("DSP sync loop cancelled")
                raise
            except SYNC_LOOP_ERRORS as exc:
                logger.exception("Error in sync loop", error=str(exc))
                # Wait a bit before retrying to avoid tight error loops
                await asyncio.sleep(min(self.sync_interval, 10))


# Global DSP client instance
_dsp_client: DSPClient | None = None


def _has_http_url(url: str) -> bool:
    """判断 URL 是否为非空的绝对 http(s) 地址。"""

    return url.strip().startswith(("http://", "https://"))


def get_dsp_client() -> DSPClient:
    """获取或创建全局的 DSP 客户端实例。"""
    global _dsp_client
    if not _has_http_url(settings.DSP_BASE_URL):
        raise SyncOperationError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_name=SyncError.CLIENT_CONFIG_ERROR,
            error_msg="DSP 功能未启用或配置无效：DSP_BASE_URL 需要为绝对 http(s) URL",
            input_params={
                "dsp_base_url": settings.DSP_BASE_URL,
                "sync_interval": settings.DSP_CHANGES_PULL_INTERVAL,
            },
        )

    if _dsp_client is None:
        try:
            _dsp_client = DSPClient(
                registry_base_url=settings.DSP_BASE_URL,
                sync_interval=settings.DSP_CHANGES_PULL_INTERVAL,
                target_types=["acs"],  # Focus on ACS objects for agent discovery
            )
        except (AttributeError, TypeError) as e:
            raise SyncOperationError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error_name=SyncError.CLIENT_CONFIG_ERROR,
                error_msg=f"创建 DSP 客户端失败: {e}",
                input_params={
                    "dsp_base_url": settings.DSP_BASE_URL,
                    "sync_interval": settings.DSP_CHANGES_PULL_INTERVAL,
                },
            ) from e
    return _dsp_client


async def start_dsp_sync() -> None:
    """启动 DSP 同步服务。"""
    client = get_dsp_client()
    await client.start_background_sync()


async def stop_dsp_sync() -> None:
    """停止 DSP 同步服务。"""
    global _dsp_client
    if _dsp_client:
        await _dsp_client.stop_background_sync()
        await _dsp_client.close()
