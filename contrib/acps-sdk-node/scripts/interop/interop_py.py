"""Python 端：在驱动进程的事件循环里直接运行 Python SDK（服务端用 uvicorn + 真实 mTLS）。

PySide 与 interop_node.NodeSide 提供同一组方法。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import FastAPI

from acps_sdk.aip import ACSObject, GroupLeaderMqClient, GroupPartnerMqClient
from acps_sdk.aip.aip_group_runtime import build_inbox_queue_name
from acps_sdk.aip.aip_base_model import (
    Product,
    TaskCommand,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_notification_client import AipNotificationClient, NotificationReceiver
from acps_sdk.aip.aip_notification_server import NotificationService, add_aip_notification_router
from acps_sdk.aip.aip_peer_cert import AipPeerCertH11Protocol, AipPeerCertificateMiddleware
from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_rpc_server import (
    CommandHandlers,
    DefaultHandlers,
    TaskManager,
    add_aip_rpc_router,
)
from acps_sdk.aip.aip_stream_client import AipStreamClient
from acps_sdk.aip.aip_stream_model import ProductChunkEvent, TaskStatusUpdateEvent
from acps_sdk.aip.aip_stream_server import StreamHandlers, StreamHub, add_aip_stream_router

from interop_common import (
    INPUT_PROMPT,
    LEADER_AIC,
    NEED_INPUT_MARK,
    PARTNER_AIC,
    Pki,
    TlsFiles,
    expected_products,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [to_json(v) for v in value]
    return value


def ok(value: Any) -> dict[str, Any]:
    return {"ok": True, "result": to_json(value)}


def describe_error(exc: BaseException) -> dict[str, Any]:
    described: dict[str, Any] = {"name": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        described["status"] = exc.response.status_code
        with contextlib.suppress(Exception):
            described["body"] = exc.response.text
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        described["cause"] = describe_error(cause)
        if "status" not in described and "status" in described["cause"]:
            described["status"] = described["cause"]["status"]
    return described


def err(exc: BaseException) -> dict[str, Any]:
    return {"ok": False, "error": describe_error(exc)}


async def _bounded(coro: Any, timeout: float, label: str) -> dict[str, Any]:
    """与 Node agent 的请求超时对应：Python 端调用卡住时返回错误而不是拖住整个测试。"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": {"name": "HarnessTimeout", "message": f"{label} timed out after {timeout}s"}}


def client_ssl(identity: TlsFiles | None, pki: Pki) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(pki.ca))
    if identity is not None:
        context.load_cert_chain(certfile=str(identity.cert), keyfile=str(identity.key))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def server_ssl(identity: TlsFiles, *, strict: bool) -> ssl.SSLContext:
    """strict=False 时客户端证书可选（CERT_OPTIONAL），用来验证缺少对端身份时的 -32008。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(identity.cert), keyfile=str(identity.key))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(identity.ca))
    context.verify_mode = ssl.CERT_REQUIRED if strict else ssl.CERT_OPTIONAL
    return context


def _command_text(command: TaskCommand) -> str:
    return "\n".join(
        item.text for item in (command.dataItems or []) if isinstance(item, TextDataItem) and item.text
    )


def _products(task_id: str, text: str) -> list[Product]:
    return [Product.model_validate(p) for p in expected_products(task_id, text)]


class EventLog:
    """按顺序记录事件，支持「从某个位置之后等待满足条件的事件」。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._signal = asyncio.Event()

    def emit(self, event: str, data: Any) -> None:
        self.events.append({"event": event, "data": data})
        self._signal.set()

    async def wait(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float, *, since: int = 0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = since
        while True:
            for event in self.events[seen:]:
                if predicate(event):
                    return event
            seen = len(self.events)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"python side: event not seen within {timeout}s")
            self._signal.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._signal.wait(), timeout=remaining)


class PySide:
    """Python SDK 一侧（Partner 服务端 + Leader 客户端 + 群组两端）。"""

    lang = "python"

    def __init__(self, pki: Pki, broker: dict[str, Any] | None, *, debug: bool) -> None:
        self.pki = pki
        self.broker = broker
        self.debug = debug
        self._servers: list[tuple[uvicorn.Server, asyncio.Task[None], socket.socket]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._partner_urls: dict[str, str] | None = None
        self._notifications: NotificationService | None = None
        self._received: list[dict[str, Any]] = []
        self.log = EventLog()
        self._group_leader: GroupLeaderMqClient | None = None
        self._group_partner: GroupPartnerMqClient | None = None
        self._partner_report: dict[str, list[Any]] = {"commands": [], "mgmt": [], "disconnects": []}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._group_partner is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._group_partner.close(), timeout=20)
        if self._group_leader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._group_leader.close(), timeout=20)
        for server, _task, _sock in self._servers:
            server.should_exit = True
        for server, task, sock in self._servers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=10)
            sock.close()
        self._servers.clear()
        if self._notifications is not None:
            await self._notifications.close()
        for task in list(self._background):
            task.cancel()

    async def _run_app(self, app: FastAPI, context: ssl.SSLContext) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            http=AipPeerCertH11Protocol,
            ws="none",
            log_level="warning" if self.debug else "critical",
            proxy_headers=False,
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
        config.load()
        config.ssl = context
        server = uvicorn.Server(config)
        # 在驱动的事件循环里运行，不接管进程信号
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
        task = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(500):
            if server.started or task.done():
                break
            await asyncio.sleep(0.02)
        if not server.started:
            raise RuntimeError("uvicorn TLS server did not start")
        self._servers.append((server, task, sock))
        return f"https://127.0.0.1:{port}"

    # ---- Partner -----------------------------------------------------------

    async def serve_partner(self) -> dict[str, str]:
        if self._partner_urls is not None:
            return self._partner_urls

        hub = StreamHub()

        async def on_start(command: TaskCommand, task: TaskResult | None) -> TaskResult:
            if task is not None:
                TaskManager.add_command_to_history(task.taskId, command)
                return task
            text = _command_text(command)
            if NEED_INPUT_MARK in text:
                created = TaskManager.create_task(
                    command, TaskState.AwaitingInput, [TextDataItem(text=INPUT_PROMPT)]
                )
            else:
                created = TaskManager.create_task(command, TaskState.AwaitingCompletion)
                TaskManager.set_products(created.taskId, _products(created.taskId, text))
            created.senderId = PARTNER_AIC
            return created

        async def on_continue(command: TaskCommand, task: TaskResult) -> TaskResult:
            TaskManager.add_command_to_history(task.taskId, command)
            text = _command_text(command)
            if task.status.state != TaskState.AwaitingInput or not text.strip():
                return task
            TaskManager.set_products(task.taskId, _products(task.taskId, text))
            return TaskManager.update_task_status(task.taskId, TaskState.AwaitingCompletion)

        async def on_complete(command: TaskCommand, task: TaskResult) -> TaskResult:
            result = await DefaultHandlers.complete(command, task)
            if result.status.state == TaskState.Completed and hub.get_channel(task.taskId):
                await hub.publish_task_result(task.taskId, result.model_copy(deep=True))
            return result

        async def run_stream_task(command: TaskCommand) -> None:
            task_id = command.taskId or ""
            text = _command_text(command)
            task = TaskManager.create_task(command, TaskState.Accepted)
            task.senderId = PARTNER_AIC
            base = {
                "senderRole": "partner",
                "senderId": PARTNER_AIC,
                "taskId": task_id,
                "sessionId": command.sessionId,
            }
            await hub.publish_task_result(task_id, task.model_copy(deep=True))
            await hub.publish_status_update(
                task_id,
                TaskStatusUpdateEvent(
                    id=f"evt-{task_id}-2",
                    sentAt=_now(),
                    status=TaskStatus(state=TaskState.Working, stateChangedAt=_now()),
                    **base,
                ),
            )
            await asyncio.sleep(0.4)
            await hub.publish_product_chunk(
                task_id,
                ProductChunkEvent(
                    id=f"evt-{task_id}-3",
                    sentAt=_now(),
                    product=Product(
                        id=f"product-{task_id}",
                        name="stream",
                        dataItems=[TextDataItem(text="已处理：")],
                    ),
                    append=False,
                    lastChunk=False,
                    **base,
                ),
            )
            await hub.publish_product_chunk(
                task_id,
                ProductChunkEvent(
                    id=f"evt-{task_id}-4",
                    sentAt=_now(),
                    product=Product(id=f"product-{task_id}", dataItems=[TextDataItem(text=text)]),
                    append=True,
                    lastChunk=True,
                    **base,
                ),
            )
            TaskManager.set_products(task_id, _products(task_id, text))
            awaiting = TaskManager.update_task_status(task_id, TaskState.AwaitingCompletion)
            await hub.publish_task_result(task_id, awaiting.model_copy(deep=True))

        async def on_stream_start(command: TaskCommand) -> None:
            # handle_stream_request 以 create_task 触发且不保留引用，这里自己持有后台任务
            background = asyncio.create_task(run_stream_task(command))
            self._background.add(background)
            background.add_done_callback(self._background.discard)

        self._notifications = NotificationService(
            local_aic=PARTNER_AIC,
            callback_ssl_context=client_ssl(self.pki.partner, self.pki),
            identity_binding_enabled=True,
            max_retries=0,
        )

        app = FastAPI()
        app.add_middleware(AipPeerCertificateMiddleware)
        add_aip_rpc_router(
            app,
            "/rpc",
            CommandHandlers(on_start=on_start, on_continue=on_continue, on_complete=on_complete),
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        add_aip_stream_router(
            app,
            "/stream",
            hub,
            StreamHandlers(on_stream_start=on_stream_start),
            local_aic=PARTNER_AIC,
            identity_binding_enabled=True,
        )
        add_aip_notification_router(app, self._notifications)

        strict = await self._run_app(app, server_ssl(self.pki.partner, strict=True))
        permissive = await self._run_app(app, server_ssl(self.pki.partner, strict=False))
        self._partner_urls = {"baseUrl": strict, "permissiveBaseUrl": permissive}
        return self._partner_urls

    async def partner_notify(self, task_id: str, session_id: str, state: str) -> dict[str, Any]:
        assert self._notifications is not None
        result = TaskResult(
            id=f"result-{uuid.uuid4()}",
            sentAt=_now(),
            senderRole="partner",
            senderId=PARTNER_AIC,
            sessionId=session_id,
            taskId=task_id,
            status=TaskStatus(state=TaskState(state), stateChangedAt=_now()),
        )
        try:
            await self._notifications.dispatch(result)
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        return ok(result)

    # ---- Leader ------------------------------------------------------------

    async def rpc(
        self,
        url: str,
        method: str,
        task_id: str,
        session_id: str,
        *,
        text: str | None = None,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
    ) -> dict[str, Any]:
        try:
            client = AipRpcClient(
                partner_url=url,
                leader_id=leader_id,
                ssl_context=client_ssl(identity, self.pki),
                expected_partner_aic=expected,
            )
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        try:
            if method == "start":
                result = await client.start_task(session_id=session_id, user_input=text or "", task_id=task_id)
            elif method == "get":
                result = await client.get_task(task_id=task_id, session_id=session_id)
            elif method == "continue":
                result = await client.continue_task(task_id=task_id, session_id=session_id, user_input=text or "")
            elif method == "complete":
                result = await client.complete_task(task_id=task_id, session_id=session_id)
            elif method == "cancel":
                result = await client.cancel_task(task_id=task_id, session_id=session_id)
            else:
                raise ValueError(method)
            return ok(result)
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        finally:
            await client.close()

    async def stream_collect(
        self,
        stream_url: str,
        rpc_url: str,
        mode: str,
        task_id: str,
        session_id: str,
        *,
        text: str | None = None,
        last_event_seq: int | None = None,
        stop_after: int | None = None,
        complete_on_awaiting: bool = False,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
    ) -> dict[str, Any]:
        client = AipStreamClient(
            stream_url,
            rpc_url,
            leader_id,
            ssl_context=client_ssl(identity, self.pki),
            expected_partner_aic=expected,
        )
        if mode == "start":
            generator = client.start_stream(session_id=session_id, task_id=task_id, text_content=text)
        else:
            generator = client.re_stream(task_id=task_id, session_id=session_id, last_event_seq=last_event_seq)
        events: list[Any] = []
        complete_result: Any = None
        try:
            async for event in generator:
                events.append(to_json(event))
                if stop_after and len(events) >= stop_after:
                    break
                data = event.result.eventData if event.result else None
                if (
                    complete_on_awaiting
                    and isinstance(data, TaskResult)
                    and data.status.state == TaskState.AwaitingCompletion
                ):
                    complete_result = to_json(await client.complete(task_id=task_id, session_id=session_id))
            return ok({"events": events, "lastEventSeq": client.last_event_seq, "completeResult": complete_result})
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        finally:
            await generator.aclose()  # type: ignore[attr-defined]
            await client.close()

    async def receiver_start(self, token: str) -> str:
        async def handler(task_result: TaskResult) -> None:
            self._received.append(to_json(task_result))

        app = FastAPI()
        app.add_middleware(AipPeerCertificateMiddleware)
        receiver = NotificationReceiver(token=token, handler=handler, identity_binding_enabled=True)
        receiver.mount(app, "/callbacks/{task_id}")
        return await self._run_app(app, server_ssl(self.pki.leader, strict=True))

    async def received_callbacks(self) -> list[dict[str, Any]]:
        return list(self._received)

    async def notif(
        self,
        partner_url: str,
        method: str,
        task_id: str,
        *,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
        callback_url: str | None = None,
        token: str | None = None,
        config_id: str | None = None,
        session_id: str | None = None,
        notify_on_states: list[str] | None = None,
    ) -> dict[str, Any]:
        client = AipNotificationClient(
            partner_url,
            leader_id,
            ssl_context=client_ssl(identity, self.pki),
            expected_partner_aic=expected,
        )
        try:
            if method == "set":
                return ok(await client.set_config(task_id, callback_url or "", token or "", config_id))
            if method == "get":
                return ok(await client.get_configs(task_id, config_id))
            if method == "start":
                return ok(
                    await client.start_notification(
                        task_id, config_id or "", session_id or "", notify_on_states
                    )
                )
            if method == "delete":
                return ok(await client.delete_config(task_id, config_id))
            raise ValueError(method)
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        finally:
            await client.close()

    async def raw_post(
        self,
        url: str,
        body: Any,
        *,
        identity: TlsFiles | None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        try:
            async with httpx.AsyncClient(verify=client_ssl(identity, self.pki), timeout=15.0) as client:
                response = await client.post(
                    url,
                    content=payload.encode("utf-8"),
                    headers={"Content-Type": "application/json", **(headers or {})},
                )
        except Exception as exc:  # noqa: BLE001
            return err(exc)
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = response.text
        return ok({"status": response.status_code, "contentType": response.headers.get("content-type"), "body": parsed})

    # ---- 群组 --------------------------------------------------------------

    def mark(self) -> int:
        return len(self.log.events)

    async def group_leader_open(self, group_id: str) -> dict[str, Any]:
        assert self.broker is not None
        client = GroupLeaderMqClient(
            LEADER_AIC,
            rabbitmq_host=self.broker["host"],
            rabbitmq_port=self.broker["port"],
            rabbitmq_vhost=self.broker["vhost"],
            rabbitmq_user=LEADER_AIC,
            rabbitmq_password=self.broker["password"],
            identity_binding_enabled=True,
        )
        await client.connect()
        resolved = await client.create_group(group_id=group_id)

        async def on_message(message: Any) -> None:
            self.log.emit("group.leader.message", to_json(message))

        client.set_message_handler(on_message)
        await client.start_consuming()
        self._group_leader = client
        return {"groupId": resolved, "exchange": client._exchange_name}

    async def group_leader(self, method: str, timeout: float = 30.0, **kw: Any) -> dict[str, Any]:
        return await _bounded(self._group_leader_call(method, **kw), timeout, f"group leader {method}")

    async def _group_leader_call(self, method: str, **kw: Any) -> dict[str, Any]:
        client = self._group_leader
        assert client is not None
        mentions = kw.get("mentions")
        session_id = kw.get("sessionId")
        try:
            if method == "inviteViaInbox":
                partner_aic = kw["partnerAic"]
                result: Any = await client.invite_via_inbox(
                    ACSObject(aic=partner_aic), partner_inbox=build_inbox_queue_name(partner_aic)
                )
            elif method == "startTask":
                result = await client.start_task(
                    session_id, kw.get("text") or "", task_id=kw.get("taskId"), mentions=mentions
                )
            elif method == "continueTask":
                result = await client.continue_task(
                    kw["taskId"], session_id, kw.get("text") or "", mentions=mentions
                )
            elif method == "completeTask":
                result = await client.complete_task(kw["taskId"], session_id, mentions=mentions)
            elif method == "cancelTask":
                result = await client.cancel_task(kw["taskId"], session_id, mentions=mentions)
            elif method == "getPartnerStatus":
                result = await client.get_partner_status(kw.get("partnerAic"), session_id=session_id)
            elif method == "mutePartner":
                result = await client.mute_partner(kw["partnerAic"], session_id=session_id)
            elif method == "unmutePartner":
                result = await client.unmute_partner(kw["partnerAic"], session_id=session_id)
            elif method == "requestPartnerLeave":
                result = await client.request_partner_leave(kw["partnerAic"], session_id=session_id)
            elif method == "dissolveGroup":
                await client.dissolve_group()
                result = {"state": client.state.value}
            elif method == "close":
                await client.close()
                self._group_leader = None
                result = None
            else:
                raise ValueError(method)
            return ok(result)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    def leader_messages(self) -> list[dict[str, Any]]:
        return [e["data"] for e in self.log.events if e["event"] == "group.leader.message"]

    async def wait_leader_message(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10.0, *, since: int = 0
    ) -> dict[str, Any]:
        event = await self.log.wait(
            lambda e: e["event"] == "group.leader.message" and predicate(e["data"]), timeout, since=since
        )
        return event["data"]

    async def group_partner_open(self) -> dict[str, Any]:
        assert self.broker is not None
        client = GroupPartnerMqClient(
            PARTNER_AIC,
            rabbitmq_host=self.broker["host"],
            rabbitmq_port=self.broker["port"],
            rabbitmq_vhost=self.broker["vhost"],
            rabbitmq_user=PARTNER_AIC,
            rabbitmq_password=self.broker["password"],
            identity_binding_enabled=True,
        )
        report: dict[str, list[Any]] = {"commands": [], "mgmt": [], "disconnects": []}
        self._partner_report = report

        async def on_command(command: TaskCommand, is_mentioned: bool) -> None:
            text = _command_text(command)
            report["commands"].append(
                {
                    "id": command.id,
                    "command": command.command.value,
                    "taskId": command.taskId,
                    "sessionId": command.sessionId,
                    "senderId": command.senderId,
                    "groupId": command.groupId,
                    "mentions": command.mentions,
                    "isMentioned": is_mentioned,
                    "text": text,
                }
            )
            if not is_mentioned:
                return
            task_id = command.taskId or ""
            session_id = command.sessionId or ""
            kind = command.command.value
            if kind == "start":
                await client.accept_task(task_id, session_id)
                await client.start_working(task_id, session_id)
                if NEED_INPUT_MARK in text:
                    await client.request_input(task_id, session_id, INPUT_PROMPT)
                else:
                    await client.submit_for_completion(task_id, session_id, _products(task_id, text))
            elif kind == "continue":
                await client.submit_for_completion(task_id, session_id, _products(task_id, text))
            elif kind == "complete":
                await client.complete_task(task_id, session_id)
            elif kind == "cancel":
                await client.cancel_task(task_id, session_id)

        async def on_mgmt(command: Any) -> None:
            entry = {
                "id": command.id,
                "command": command.command.value,
                "senderId": command.senderId,
                "sessionId": command.sessionId,
                "mentions": command.mentions,
            }
            report["mgmt"].append(entry)
            self.log.emit("group.partner.mgmt", entry)

        def on_disconnect(_client: GroupPartnerMqClient, group_id: str | None) -> None:
            report["disconnects"].append(group_id)
            self.log.emit("group.partner.disconnected", {"groupId": group_id})

        async def on_invitation(invitation: Any) -> None:
            self.log.emit("group.partner.invitation", to_json(invitation))
            joined = await client.join_group_from_invitation(invitation)
            self.log.emit(
                "group.partner.joined",
                {
                    "ok": joined,
                    "groupId": client.group_id,
                    "queueName": client.queue_name,
                    "state": client.state.value,
                },
            )

        client.set_command_handler(on_command)
        client.set_mgmt_command_handler(on_mgmt)
        client.set_disconnect_handler(on_disconnect)
        await client.start_inbox_consuming(on_invitation)
        self._group_partner = client
        return {"inbox": build_inbox_queue_name(PARTNER_AIC)}

    async def group_partner(self, method: str, timeout: float = 30.0, **kw: Any) -> dict[str, Any]:
        return await _bounded(self._group_partner_call(method, **kw), timeout, f"group partner {method}")

    async def _group_partner_call(self, method: str, **kw: Any) -> dict[str, Any]:
        client = self._group_partner
        try:
            if method == "report":
                return ok(
                    {
                        **self._partner_report,
                        "state": client.state.value if client else None,
                        "groupId": client.group_id if client else None,
                        "isMuted": client.is_muted if client else None,
                    }
                )
            if method == "leave":
                assert client is not None
                await client.leave_group(session_id=kw.get("sessionId"))
                return ok({"state": client.state.value})
            if method == "close":
                if client is not None:
                    await client.close()
                self._group_partner = None
                return ok(None)
            raise ValueError(method)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    async def wait_partner_event(
        self,
        kind: str,
        predicate: Callable[[dict[str, Any]], bool] = lambda _d: True,
        timeout: float = 10.0,
        *,
        since: int = 0,
    ) -> dict[str, Any]:
        event = await self.log.wait(
            lambda e: e["event"] == f"group.partner.{kind}" and predicate(e["data"]), timeout, since=since
        )
        return event["data"]
