"""
AIP v2 通知方式服务端实现

本模块分两层：
  N1 层（纯逻辑，无 HTTP 依赖）：
    - NotificationConfigStore   —— 通知配置存储
    - NotificationSubscription  —— 订阅记录
    - NotificationRegistry      —— 订阅注册表
    - NotificationDispatcher    —— 回调分发器（含重试）

  N2 层（HTTP / FastAPI，在此文件后半段实现）：
    - NotificationHandlers      —— 应用层回调
    - NotificationService       —— 聚合 store/registry/dispatcher
    - add_aip_notification_router —— 注册 FastAPI 路由
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

import httpx

from .aip_base_model import TaskResult, TaskState, TaskCommand
from .aip_identity import (
    AipIdentityError,
    InvalidPeerCertificateError,
    PeerAicMissingError,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    identity_error_to_jsonrpc,
    normalize_aic,
)
from .aip_notification_model import NotificationConfig
from acps_sdk.aic import validate_aic_format
from .aip_peer_cert import get_request_peer_aic

logger = logging.getLogger("acps_sdk.aip.aip_notification_server")
_UNSCOPED_OWNER = "__notification_unscoped__"


# ===========================================================================
# N1：纯逻辑层
# ===========================================================================

# ---------------------------------------------------------------------------
# NotificationConfigStore
# ---------------------------------------------------------------------------


class NotificationConfigStore:
    """通知配置存储（内存实现）。

    注意：本实现为单事件循环使用，不加锁。
    所有调用须在同一 asyncio 事件循环内串行执行。
    若需多线程共享，需在外部加锁。
    """

    def __init__(self) -> None:
        # owner -> task_id -> {config_id -> NotificationConfig}
        self._store: dict[str, dict[str, dict[str, NotificationConfig]]] = {}

    def _owner_key(self, owner_aic: str | None) -> str:
        return owner_aic or _UNSCOPED_OWNER

    def set(
        self,
        config: NotificationConfig,
        *,
        owner_aic: str | None = None,
    ) -> NotificationConfig:
        """新建或更新配置。无 id 时自动生成。"""
        task_id = config.taskId
        owner_key = self._owner_key(owner_aic)
        if owner_key not in self._store:
            self._store[owner_key] = {}
        if task_id not in self._store[owner_key]:
            self._store[owner_key][task_id] = {}

        if not config.id:
            # 生成唯一 id 并返回新配置对象
            new_id = f"notification-{uuid.uuid4()}"
            config = config.model_copy(update={"id": new_id})

        self._store[owner_key][task_id][config.id] = config  # type: ignore[index]
        return config

    def delete_for_owner(
        self,
        owner_aic: str | None,
        task_id: str,
        *,
        config_id: Optional[str] = None,
    ) -> bool:
        """删除配置。config_id=None 删除该 task 全部配置。返回是否有实际删除。"""
        owner_key = self._owner_key(owner_aic)
        owner_store = self._store.get(owner_key, {})
        task_configs = owner_store.get(task_id)
        if not task_configs:
            return False

        if config_id is not None:
            if config_id in task_configs:
                del task_configs[config_id]
                if not task_configs:
                    del owner_store[task_id]
                if not owner_store:
                    self._store.pop(owner_key, None)
                return True
            return False
        else:
            # 删除全部
            del owner_store[task_id]
            if not owner_store:
                self._store.pop(owner_key, None)
            return True

    def get_for_owner(
        self,
        owner_aic: str | None,
        task_id: str,
        *,
        config_id: Optional[str] = None,
    ) -> list[NotificationConfig]:
        """查询配置。config_id 给定则返回该条；否则返回 task 全部配置。"""
        owner_key = self._owner_key(owner_aic)
        task_configs = self._store.get(owner_key, {}).get(task_id, {})
        if config_id is not None:
            cfg = task_configs.get(config_id)
            return [cfg] if cfg is not None else []
        return list(task_configs.values())

    def require_for_owner(
        self,
        owner_aic: str | None,
        task_id: str,
        *,
        config_id: str,
    ) -> NotificationConfig:
        configs = self.get_for_owner(owner_aic, task_id, config_id=config_id)
        if not configs:
            raise KeyError(f"NotificationConfig {config_id} not found")
        return configs[0]

    def delete(self, task_id: str, *, config_id: Optional[str] = None) -> bool:
        return self.delete_for_owner(None, task_id, config_id=config_id)

    def get(self, task_id: str, *, config_id: Optional[str] = None) -> list[NotificationConfig]:
        return self.get_for_owner(None, task_id, config_id=config_id)

    def list_for_task(self, task_id: str) -> list[NotificationConfig]:
        return self.get(task_id)


# ---------------------------------------------------------------------------
# NotificationSubscription & NotificationRegistry
# ---------------------------------------------------------------------------


@dataclass
class NotificationSubscription:
    """一条订阅记录。"""

    task_id: str
    config_id: str
    owner_aic: str | None = None
    notify_on_states: Optional[List[TaskState]] = None


class NotificationRegistry:
    """通知订阅注册表。

    记录哪些任务订阅了通知以及希望收到哪些状态的通知。
    """

    def __init__(self) -> None:
        # task_id -> list[NotificationSubscription]
        self._subs: dict[str, list[NotificationSubscription]] = {}

    def add(self, subscription: NotificationSubscription) -> None:
        """注册一条订阅。"""
        if subscription.task_id not in self._subs:
            self._subs[subscription.task_id] = []
        self._subs[subscription.task_id].append(subscription)

    def remove_task(self, task_id: str) -> None:
        """移除一个任务的所有订阅。"""
        self._subs.pop(task_id, None)

    def matches(self, task_id: str, state: TaskState) -> list[NotificationSubscription]:
        """返回命中指定 task_id 与 state 的所有订阅。

        notify_on_states 为 None 或空列表时命中所有状态。
        """
        subs = self._subs.get(task_id, [])
        result = []
        for sub in subs:
            if not sub.notify_on_states:
                result.append(sub)
            elif state in sub.notify_on_states:
                result.append(sub)
        return result


# ---------------------------------------------------------------------------
# NotificationDispatcher
# ---------------------------------------------------------------------------


class NotificationDispatcher:
    """通知回调分发器。

    对命中订阅，并发向各回调 URL 发送 POST 请求（请求体为 TaskResult JSON）。
    非 2xx 或网络异常时按指数退避重试；全部失败后记日志，不抛出。
    """

    def __init__(
        self,
        config_store: NotificationConfigStore,
        registry: NotificationRegistry,
        *,
        local_aic: str | None = None,
        callback_ssl_context: ssl.SSLContext | None = None,
        transport: Optional[httpx.AsyncTransport] = None,
        identity_binding_enabled: bool = True,
        max_retries: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        self._store = config_store
        self._registry = registry
        self._local_aic = local_aic
        self._identity_binding_enabled = identity_binding_enabled
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        # 注入 transport 以便测试替换真实 HTTP；None 时使用真实客户端
        if self._identity_binding_enabled and self._local_aic is None:
            raise ValueError("local_aic is required when identity_binding_enabled=True")
        if self._identity_binding_enabled and transport is None and callback_ssl_context is None:
            raise ValueError(
                "callback_ssl_context is required when identity_binding_enabled=True and transport is not injected"
            )
        if transport is not None:
            self._client = httpx.AsyncClient(transport=transport)
        elif callback_ssl_context is not None:
            self._client = httpx.AsyncClient(verify=callback_ssl_context)
        else:
            self._client = httpx.AsyncClient()

    async def dispatch(self, task_result: TaskResult) -> None:
        """分发通知：找到匹配订阅，并发调用各回调端点。"""
        if self._identity_binding_enabled:
            assert_sender_matches_expected(task_result, self._local_aic)
        task_id = task_result.taskId
        state = task_result.status.state
        subs = self._registry.matches(task_id, state)
        if not subs:
            return

        tasks = []
        for sub in subs:
            cfg = self._store.require_for_owner(
                sub.owner_aic,
                task_id,
                config_id=sub.config_id,
            )
            tasks.append(self._post_once(cfg, task_result))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _post_once(
        self, config: NotificationConfig, task_result: TaskResult
    ) -> None:
        """向单个回调 URL 发送通知，带指数退避重试。"""
        from .aip_notification_model import NOTIFICATION_TOKEN_HEADER

        headers = {
            NOTIFICATION_TOKEN_HEADER: config.token,
            "Content-Type": "application/json",
        }
        body = task_result.model_dump_json(exclude_none=True)

        for attempt in range(1 + self._max_retries):
            try:
                response = await self._client.post(
                    config.url,
                    content=body.encode(),
                    headers=headers,
                )
                if response.is_success:
                    return
                logger.warning(
                    "Notification callback non-2xx: url=%s status=%d attempt=%d",
                    config.url,
                    response.status_code,
                    attempt,
                )
            except Exception as exc:
                logger.warning(
                    "Notification callback exception: url=%s attempt=%d error=%s",
                    config.url,
                    attempt,
                    str(exc),
                )

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_s * (2**attempt))

        logger.warning(
            "Notification callback gave up after all retries: url=%s task_id=%s",
            config.url,
            task_result.taskId,
        )

    async def close(self) -> None:
        """关闭内部 HTTP 客户端。"""
        await self._client.aclose()


# ===========================================================================
# N2：HTTP / FastAPI 层
# ===========================================================================

import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .aip_notification_model import (
    NotificationDeleteRequest,
    NotificationDeleteResponse,
    NotificationDeleteResult,
    NotificationGetRequest,
    NotificationGetResponse,
    NotificationRequest,
    NotificationResponse,
    NotificationStartRequest,
)


@dataclass
class NotificationHandlers:
    """通知服务的应用层回调接口。

    Partner 侧注册后，可在 /notification/start 收到 start 命令时触发任务执行。
    在「双阶段」设计（Leader 先 RPC start，再 notification/start 订阅）中，
    on_notification_start 通常为 None；只有「单阶段」设计才注册此回调。
    """

    on_notification_start: Optional[Callable[[TaskCommand], Awaitable[None]]] = None


class NotificationService:
    """聚合 store / registry / dispatcher 的服务对象。

    初始化后挂载到 FastAPI app；通过 dispatch 方法触发已注册订阅的回调。
    可选地接受 NotificationHandlers，用于在 /notification/start 时触发任务启动。
    """

    def __init__(
        self,
        *,
        handlers: Optional[NotificationHandlers] = None,
        local_aic: str | None = None,
        callback_ssl_context: ssl.SSLContext | None = None,
        transport: Optional[httpx.AsyncTransport] = None,
        identity_binding_enabled: bool = True,
        max_retries: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        self.store = NotificationConfigStore()
        self.registry = NotificationRegistry()
        self.local_aic = local_aic
        self.identity_binding_enabled = identity_binding_enabled
        self.dispatcher = NotificationDispatcher(
            config_store=self.store,
            registry=self.registry,
            local_aic=local_aic,
            callback_ssl_context=callback_ssl_context,
            transport=transport,
            identity_binding_enabled=identity_binding_enabled,
            max_retries=max_retries,
            backoff_s=backoff_s,
        )
        self.handlers = handlers

    async def dispatch(self, task_result: TaskResult) -> None:
        """触发与 task_result 匹配的所有订阅回调。"""
        await self.dispatcher.dispatch(task_result)

    async def notify(self, task_result: TaskResult) -> None:
        """dispatch 的别名，与设计文档命名对齐。"""
        await self.dispatcher.dispatch(task_result)

    async def handle_set(
        self,
        req: "NotificationRequest",
        *,
        owner_aic: str | None = None,
    ) -> "NotificationResponse":
        """处理 notification/set 请求的业务逻辑（不含 HTTP 解析）。"""
        cfg = self.store.set(req.params, owner_aic=owner_aic)
        return NotificationResponse(id=req.id, result=cfg)

    async def handle_delete(
        self,
        req: "NotificationDeleteRequest",
        *,
        owner_aic: str | None = None,
    ) -> "NotificationDeleteResponse":
        """处理 notification/delete，不存在时 result=None。"""
        success = self.store.delete_for_owner(
            owner_aic,
            req.params.taskId,
            config_id=req.params.notificationConfigId,
        )
        result = NotificationDeleteResult() if success else None
        return NotificationDeleteResponse(id=req.id, result=result)

    async def handle_get(
        self,
        req: "NotificationGetRequest",
        *,
        owner_aic: str | None = None,
    ) -> "NotificationGetResponse":
        """处理 notification/get 请求的业务逻辑（不含 HTTP 解析）。"""
        configs = self.store.get_for_owner(
            owner_aic,
            req.params.taskId,
            config_id=req.params.notificationConfigId,
        )
        return NotificationGetResponse(id=req.id, result=configs)

    async def handle_start(
        self,
        req: "NotificationStartRequest",
        *,
        owner_aic: str | None = None,
    ) -> dict:
        """处理 notification/start 的业务逻辑，返回 JSON-RPC 响应 dict。

        不包含 HTTP 层错误抛出；缺少必要字段时 raise ValueError。
        """
        command = req.params.message
        if self.identity_binding_enabled:
            assert_sender_matches_peer(command, owner_aic)
        task_id = command.taskId
        if not task_id:
            raise ValueError("taskId is required")

        command_params = command.commandParams or {}
        config_id = command_params.get("notificationConfigId")
        if not config_id:
            raise ValueError("commandParams.notificationConfigId is required")

        cfg = self.store.require_for_owner(owner_aic, task_id, config_id=config_id)

        notify_on_states = None
        raw_states = command_params.get("notifyOnStates")
        if raw_states:
            from .aip_base_model import TaskState as TS
            try:
                notify_on_states = [TS(s) for s in raw_states]
            except ValueError:
                pass

        sub = NotificationSubscription(
            task_id=task_id,
            config_id=cfg.id,
            owner_aic=owner_aic,
            notify_on_states=notify_on_states,
        )
        self.registry.add(sub)

        if self.handlers and self.handlers.on_notification_start:
            asyncio.create_task(self.handlers.on_notification_start(command))

        return {"jsonrpc": "2.0", "id": req.id, "result": True}

    async def close(self) -> None:
        """释放内部 HTTP 客户端资源。"""
        await self.dispatcher.close()


def resolve_notification_owner(
    peer_aic: str | None,
    identity_binding_enabled: bool,
) -> str | None:
    if not identity_binding_enabled:
        return None
    normalized = normalize_aic(peer_aic)
    if normalized is None:
        raise PeerAicMissingError("peer AIC is required for notification identity binding")
    valid, error = validate_aic_format(normalized)
    if not valid:
        raise InvalidPeerCertificateError(f"peer AIC is invalid: {error}")
    return normalized


def add_aip_notification_router(
    app: FastAPI,
    service: NotificationService,
    *,
    base_path: str = "",
) -> None:
    """向 FastAPI 应用注册 AIP Notification 的四个 JSON-RPC 端点。

    端点使用闭包捕获 service，无需依赖注入框架。
    路由均为 POST，路径为 {base_path}/notification/{method}。
    base_path 默认为空，与旧行为保持兼容。
    """
    prefix = base_path.rstrip("/")

    def _resolve_owner(request: Request) -> str | None:
        try:
            return resolve_notification_owner(
                get_request_peer_aic(request),
                service.identity_binding_enabled,
            )
        except AipIdentityError as exc:
            error = identity_error_to_jsonrpc(exc)
            status_code = 401 if error.code == -32008 else 403
            raise HTTPException(
                status_code=status_code,
                detail=error.model_dump(exclude_none=True),
            ) from exc

    # ------------------------------------------------------------------
    # notification/set
    # ------------------------------------------------------------------
    @app.post(f"{prefix}/notification/set")
    async def notification_set(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            req = NotificationRequest.model_validate(body)
        except (ValidationError, ValueError, Exception) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": -32700, "message": "Parse error", "data": str(exc)},
            )

        resp = await service.handle_set(req, owner_aic=_resolve_owner(request))
        return JSONResponse(content=json.loads(resp.model_dump_json(exclude_none=True)))

    # ------------------------------------------------------------------
    # notification/delete
    # ------------------------------------------------------------------
    @app.post(f"{prefix}/notification/delete")
    async def notification_delete(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            req = NotificationDeleteRequest.model_validate(body)
        except (ValidationError, ValueError, Exception) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": -32700, "message": "Parse error", "data": str(exc)},
            )

        resp = await service.handle_delete(req, owner_aic=_resolve_owner(request))
        if resp.result is None:
            raise HTTPException(
                status_code=404,
                detail={"code": -32001, "message": "Notification config not found"},
            )
        return JSONResponse(content=json.loads(resp.model_dump_json(exclude_none=True)))

    # ------------------------------------------------------------------
    # notification/get
    # ------------------------------------------------------------------
    @app.post(f"{prefix}/notification/get")
    async def notification_get(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            req = NotificationGetRequest.model_validate(body)
        except (ValidationError, ValueError, Exception) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": -32700, "message": "Parse error", "data": str(exc)},
            )

        resp = await service.handle_get(req, owner_aic=_resolve_owner(request))
        return JSONResponse(content=json.loads(resp.model_dump_json(exclude_none=True)))

    # ------------------------------------------------------------------
    # notification/start
    # ------------------------------------------------------------------
    @app.post(f"{prefix}/notification/start")
    async def notification_start(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            req = NotificationStartRequest.model_validate(body)
        except (ValidationError, ValueError, Exception) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": -32700, "message": "Parse error", "data": str(exc)},
            )

        try:
            resp_body = await service.handle_start(req, owner_aic=_resolve_owner(request))
        except AipIdentityError as exc:
            error = identity_error_to_jsonrpc(exc)
            status_code = 401 if error.code == -32008 else 403
            raise HTTPException(
                status_code=status_code,
                detail=error.model_dump(exclude_none=True),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": -32602, "message": str(exc)},
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": -32001, "message": str(exc)},
            )

        return JSONResponse(content=resp_body)
