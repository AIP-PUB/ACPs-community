"""
AIP v2 通知方式客户端（Leader 侧）

提供：
- AipNotificationClient  —— 向 Partner 通知服务发起 CRUD + start 操作
- NotificationReceiver   —— Leader 侧接收 Partner 推送回调的 FastAPI 端点
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .aip_base_model import TaskCommand, TaskCommandType, TaskResult
from .aip_identity import (
    AipIdentityError,
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_peer_aic_from_httpx_response,
    identity_error_to_jsonrpc,
)
from .aip_peer_cert import get_request_peer_aic
from .aip_notification_model import (
    NOTIFICATION_TOKEN_HEADER,
    NotificationConfig,
    NotificationDeleteRequest,
    NotificationGetRequest,
    NotificationIdParams,
    NotificationRequest,
    NotificationStartRequest,
    NotificationStartRequestParams,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# AipNotificationClient
# ===========================================================================


class AipNotificationClient:
    """Leader 侧通知服务客户端。

    向 Partner 的 /notification/* 端点发送 JSON-RPC 请求，管理通知配置和订阅。
    transport 参数仅用于测试注入。
    """

    def __init__(
        self,
        partner_url: str,
        leader_id: str,
        *,
        ssl_context: Optional[ssl.SSLContext] = None,
        transport: Optional[httpx.AsyncTransport] = None,
        expected_partner_aic: str | None = None,
        identity_binding_enabled: bool = True,
    ) -> None:
        self.partner_url = partner_url.rstrip("/")
        self.leader_id = leader_id
        self._expected_partner_aic = expected_partner_aic
        self._identity_binding_enabled = identity_binding_enabled

        if self._identity_binding_enabled and not self._expected_partner_aic:
            raise ValueError(
                "expected_partner_aic is required when identity_binding_enabled=True"
            )
        if not self._identity_binding_enabled:
            logger.warning(
                "AIP identity binding disabled for Notification client partner_url=%s",
                self.partner_url,
            )

        kwargs: dict = {}
        if transport is not None:
            kwargs["transport"] = transport
        elif ssl_context is not None:
            kwargs["verify"] = ssl_context

        self._client = httpx.AsyncClient(**kwargs)

    async def _post(self, path: str, body: dict) -> dict:
        """向 partner_url + path 发送 POST，返回解析后的 JSON 响应体。"""
        url = f"{self.partner_url}/{path.lstrip('/')}"
        response = await self._client.post(
            url,
            content=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        if self._identity_binding_enabled:
            remote_aic = extract_peer_aic_from_httpx_response(response)
            assert_aic_matches_expected(
                remote_aic,
                self._expected_partner_aic,
                actual_label="TLS server AIC",
                expected_label="expected_partner_aic",
            )
        response.raise_for_status()
        return response.json()

    async def set_config(
        self,
        task_id: str,
        callback_url: str,
        token: str,
        config_id: Optional[str] = None,
    ) -> NotificationConfig:
        """注册或更新通知配置。"""
        cfg = NotificationConfig(id=config_id, url=callback_url, token=token, taskId=task_id)
        req = NotificationRequest(id=str(uuid.uuid4()), params=cfg)
        resp = await self._post(
            "notification/set", json.loads(req.model_dump_json(exclude_none=True))
        )
        result = resp.get("result")
        if result is None:
            raise ValueError(f"notification/set returned no result: {resp}")
        return NotificationConfig.model_validate(result)

    # 向下兼容旧方法名
    async def set_notification(
        self,
        task_id: str,
        callback_url: str,
        token: str,
        config_id: Optional[str] = None,
    ) -> NotificationConfig:
        """set_config 的别名（向下兼容）。"""
        return await self.set_config(task_id, callback_url, token, config_id)

    async def delete_config(
        self,
        task_id: str,
        config_id: Optional[str] = None,
    ) -> bool:
        """删除通知配置，返回 True 表示成功。"""
        req = NotificationDeleteRequest(
            id=str(uuid.uuid4()),
            params=NotificationIdParams(taskId=task_id, notificationConfigId=config_id),
        )
        resp = await self._post(
            "notification/delete", json.loads(req.model_dump_json(exclude_none=True))
        )
        result = resp.get("result", {})
        return bool(result.get("success", False))

    # 向下兼容旧方法名
    async def delete_notification(
        self,
        task_id: str,
        config_id: Optional[str] = None,
    ) -> bool:
        """delete_config 的别名（向下兼容）。"""
        return await self.delete_config(task_id, config_id)

    async def get_configs(
        self,
        task_id: str,
        config_id: Optional[str] = None,
    ) -> list[NotificationConfig]:
        """查询通知配置列表。"""
        req = NotificationGetRequest(
            id=str(uuid.uuid4()),
            params=NotificationIdParams(taskId=task_id, notificationConfigId=config_id),
        )
        resp = await self._post(
            "notification/get", json.loads(req.model_dump_json(exclude_none=True))
        )
        result = resp.get("result", [])
        return [NotificationConfig.model_validate(r) for r in result]

    # 向下兼容旧方法名
    async def get_notifications(
        self,
        task_id: str,
        config_id: Optional[str] = None,
    ) -> list[NotificationConfig]:
        """get_configs 的别名（向下兼容）。"""
        return await self.get_configs(task_id, config_id)

    async def start_notification(
        self,
        task_id: str,
        config_id: str,
        session_id: str,
        notify_on_states: Optional[list] = None,
    ) -> bool:
        """启动通知订阅（注册该任务对应的配置到 Partner 的 registry）。"""
        command_params: dict = {"notificationConfigId": config_id}
        if notify_on_states:
            command_params["notifyOnStates"] = [
                s.value if hasattr(s, "value") else s for s in notify_on_states
            ]

        command = TaskCommand(
            id=f"cmd-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_id,
            sessionId=session_id,
            command=TaskCommandType.Start,
            taskId=task_id,
            commandParams=command_params,
        )
        if self._identity_binding_enabled:
            assert_sender_matches_expected(command, self.leader_id)
        req = NotificationStartRequest(
            id=str(uuid.uuid4()),
            params=NotificationStartRequestParams(message=command),
        )
        resp = await self._post(
            "notification/start", json.loads(req.model_dump_json(exclude_none=True))
        )
        return bool(resp.get("result", False))

    async def close(self) -> None:
        """释放内部 HTTP 客户端资源。"""
        await self._client.aclose()


# ===========================================================================
# NotificationReceiver
# ===========================================================================


class NotificationReceiver:
    """Leader 侧通知回调接收器。

    挂载一个 FastAPI 端点，对收到的 POST 请求进行：
    1. 请求体解析为 TaskResult
    2. Token 时序安全校验（支持固定 token 或自定义 token_validator）
    3. 异步调用注册的 handler（fire-and-forget）

    用法（固定 token）：
        receiver = NotificationReceiver(token="...", handler=my_handler)
        receiver.mount(app, "/callback/partner1")

    用法（自定义校验器，适用于多任务场景）：
        def my_validator(provided: str, task_result: TaskResult) -> bool:
            expected = get_token_for_task(task_result.taskId)
            return secrets.compare_digest(expected.encode(), provided.encode())

        receiver = NotificationReceiver(
            token="",                   # 提供 token_validator 时此字段不使用
            handler=my_handler,
            token_validator=my_validator,
        )
        receiver.mount(app, "/callback/{task_id}")
    """

    def __init__(
        self,
        token: str,
        handler: Callable[[TaskResult], Awaitable[None]],
        *,
        token_validator: Optional[Callable[[str, TaskResult], bool]] = None,
        identity_binding_enabled: bool = True,
    ) -> None:
        self._token = token
        self._handler = handler
        self._token_validator = token_validator
        self._identity_binding_enabled = identity_binding_enabled
        if not self._identity_binding_enabled:
            logger.warning("AIP identity binding disabled for Notification receiver")

    def _validate_token(
        self,
        provided: Optional[str],
        task_result: Optional[TaskResult] = None,
    ) -> bool:
        """Token 校验：时序安全（防时序攻击）。

        若注入了 token_validator，则将校验委托给它（传入 task_result 供多任务场景使用）；
        否则对 self._token 做固定 compare_digest。
        """
        if not provided:
            return False
        if self._token_validator is not None and task_result is not None:
            return self._token_validator(provided, task_result)
        return secrets.compare_digest(self._token.encode(), provided.encode())

    def mount(self, app: FastAPI, path: str) -> None:
        """向 FastAPI 应用注册回调端点。

        先解析请求体获取 TaskResult，再校验 token（使得 token_validator 可用 task_result）。
        """
        handler = self._handler
        validate = self._validate_token
        token_header = NOTIFICATION_TOKEN_HEADER

        @app.post(path)
        async def _receive_callback(request: Request) -> JSONResponse:
            # 先解析请求体，以便 token_validator 使用 task_result
            try:
                body = await request.body()
                task_result = TaskResult.model_validate_json(body)
            except (ValidationError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"Parse error: {exc}")

            if self._identity_binding_enabled:
                try:
                    assert_sender_matches_peer(task_result, get_request_peer_aic(request))
                except AipIdentityError as exc:
                    error = identity_error_to_jsonrpc(exc)
                    status_code = 401 if error.code == -32008 else 403
                    raise HTTPException(
                        status_code=status_code,
                        detail=error.model_dump(exclude_none=True),
                    ) from exc

            # Token 校验（时序安全，now with task_result available）
            provided_token = request.headers.get(token_header)
            if not validate(provided_token, task_result):
                raise HTTPException(status_code=401, detail="Invalid or missing notification token")

            # fire-and-forget：不阻塞响应
            asyncio.create_task(handler(task_result))
            return JSONResponse(content={"ok": True})


def add_aip_notification_receiver_router(
    app: FastAPI,
    endpoint: str,
    receiver: NotificationReceiver,
) -> None:
    """向 FastAPI 应用挂载通知回调接收端点（对 receiver.mount 的命名包装）。

    与 add_aip_stream_router / add_aip_notification_router 保持命名风格一致。

    endpoint 可含 FastAPI 路径参数（如 /aip/callbacks/{task_id}），
    此时搭配 token_validator 即可实现按任务 token 校验。
    """
    receiver.mount(app, endpoint)
