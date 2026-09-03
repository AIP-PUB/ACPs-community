"""
Partner Agent 多进程启动入口

每个 partner agent 独立运行在自己的端口上，使用各自 config.toml 中的配置。
支持可选的 mTLS（服务端 HTTPS + 客户端证书验证）。

用法：
    # 启动所有 online 目录下的 partner（每个独立端口）
    python -m partners.main

    # 仅启动指定 partner
    python -m partners.main beijing_food
"""

import multiprocessing
import signal
import sys
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any

import structlog
from acps_sdk.aip.access_status import error_info_from_rpc_response, status_from_rpc_response
from acps_sdk.aip.aip_group_identity import assert_direct_group_request_identity
from acps_sdk.aip.aip_group_model import RabbitMQRequest, RabbitMQResponse, RabbitMQResponseError
from acps_sdk.aip.aip_identity import AipIdentityError, identity_error_to_jsonrpc
from acps_sdk.aip.aip_notification_server import NotificationService, add_aip_notification_router
from acps_sdk.aip.aip_peer_cert import (
    AipPeerCertH11Protocol,
    AipPeerCertificateMiddleware,
    get_request_peer_aic,
)
from acps_sdk.aip.aip_rpc_model import RpcRequest, RpcResponse
from acps_sdk.aip.aip_rpc_server import handle_rpc_request
from acps_sdk.amp.models import AccessBody, AccessParticipant, AccessRequest, AccessResponse, ErrorInfo
from acps_sdk.amp.trace_context import TRACEPARENT_HEADER, new_span_id, new_trace_id, parse_traceparent
from fastapi import FastAPI, HTTPException, Request

try:
    from partners.generic_runner import GenericRunner
    from partners.group_handler import GroupHandler
    from partners.utils import (
        CONFIG_FILENAME,
        build_client_ssl_context,
        build_rabbitmq_ssl_context,
        build_ssl_context,
        check_process_health,
        discover_agents,
        read_agent_port,
        resolve_identity_binding_enabled,
        terminate_processes,
        validate_ports,
    )
except ImportError:
    from .generic_runner import GenericRunner
    from .group_handler import GroupHandler
    from .utils import (
        CONFIG_FILENAME,
        build_client_ssl_context,
        build_rabbitmq_ssl_context,
        build_ssl_context,
        check_process_health,
        discover_agents,
        read_agent_port,
        resolve_identity_binding_enabled,
        terminate_processes,
        validate_ports,
    )

logger = structlog.get_logger()
# ---------------------------------------------------------------------------
# 单 Agent 的 FastAPI 应用工厂
# ---------------------------------------------------------------------------


def create_agent_app(agent_name: str, agent_path: str) -> FastAPI:
    """为单个 partner agent 创建 FastAPI 应用实例。"""
    try:
        from partners.notification_handler import NotificationHandler
        from partners.stream_handler import StreamHandler
    except ImportError:
        from .notification_handler import NotificationHandler
        from .stream_handler import StreamHandler

    # 读取 acs.json 能力位（静态，决定是否注册路由）
    import json as _json

    from acps_sdk.aip.aip_stream_server import StreamHub

    acs_path = Path(agent_path) / "acs.json"
    config_path = Path(agent_path) / CONFIG_FILENAME
    try:
        with acs_path.open() as acs_file:
            acs_data = _json.load(acs_file)
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
        capabilities = acs_data.get("capabilities", {})
        streaming_enabled = bool(capabilities.get("streaming", False))
        notification_enabled = bool(capabilities.get("notification", False))
    except Exception:
        config = {}
        streaming_enabled = False
        notification_enabled = False
        acs_data = {}

    partner_aic = acs_data.get("aic") or f"agent.{agent_name}"
    identity_binding_enabled = resolve_identity_binding_enabled(config)
    server_cfg = config.get("server", {})
    callback_ssl_context = build_client_ssl_context(agent_path, server_cfg)

    # 预先创建 hub / service（路由注册需要引用它们）
    hub = StreamHub() if streaming_enabled else None
    notif_service = (
        NotificationService(
            local_aic=partner_aic,
            callback_ssl_context=callback_ssl_context,
            identity_binding_enabled=identity_binding_enabled,
        )
        if notification_enabled
        else None
    )

    # 可变容器：lifespan 中赋值，路由闭包中读取
    _stream_handler: list[StreamHandler | None] = [None]
    _notif_handler: list[NotificationHandler | None] = [None]

    runner: GenericRunner | None = None
    group_handler: GroupHandler | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        nonlocal runner, group_handler
        rabbitmq_cfg = config.get("rabbitmq", {})
        mq_ssl_context = build_rabbitmq_ssl_context(agent_path, server_cfg)
        runner = GenericRunner(agent_name, agent_path)
        group_handler = GroupHandler(
            agent_name,
            runner,
            rabbitmq_config=rabbitmq_cfg,
            ssl_context=mq_ssl_context,
            identity_binding_enabled=identity_binding_enabled,
        )
        await group_handler.start()
        runner.start_heartbeat()
        runner.start_metrics()

        # 两阶段初始化：在 lifespan 中将 handler 绑定到 runner
        if streaming_enabled and hub is not None:
            sh = StreamHandler(runner=runner)
            sh.hub = hub  # 复用预先注册路由时的 hub
            _stream_handler[0] = sh
            logger.info("Streaming capability enabled", agent=agent_name)

        if notification_enabled and notif_service is not None:
            nh = NotificationHandler(runner=runner, service=notif_service)
            _notif_handler[0] = nh
            logger.info("Notification capability enabled", agent=agent_name)

        _short_aic = runner._aic[-8:] if len(runner._aic) > 8 else runner._aic
        try:
            runner._system_emitter.emit_sync(
                {
                    "message": f"Agent started: name={agent_name}, aic={_short_aic}",
                    "category": "lifecycle",
                    "component": "agent",
                    "module": "startup",
                    "tags": {"agent_name": agent_name},
                    "agent_name": agent_name,
                    "aic": runner._aic,
                    "port": read_agent_port(agent_path),
                },
                severity_number=9,
                severity_text="INFO",
            )
        except Exception:
            logger.warning("System emit S-P6 failed", exc_info=True)

        logger.info("Agent loaded", agent=agent_name)
        yield

        if runner is not None:
            try:
                runner._system_emitter.emit_sync(
                    {
                        "message": f"Agent shutdown: name={agent_name}",
                        "category": "lifecycle",
                        "component": "agent",
                        "module": "shutdown",
                        "tags": {"agent_name": agent_name},
                        "agent_name": agent_name,
                    },
                    severity_number=9,
                    severity_text="INFO",
                )
            except Exception:
                logger.warning("System emit S-P7 failed", exc_info=True)

        if group_handler:
            await group_handler.shutdown()
        if runner:
            await runner.shutdown()
        if _notif_handler[0] is not None:
            await _notif_handler[0].close()
            _notif_handler[0] = None
        if _stream_handler[0] is not None:
            _stream_handler[0] = None
        logger.info("Agent shutdown complete", agent=agent_name)

    app = FastAPI(lifespan=lifespan, title=f"Partner: {agent_name}")
    app.state.identity_binding_enabled = identity_binding_enabled
    app.state.partner_aic = partner_aic
    app.add_middleware(AipPeerCertificateMiddleware)

    # ---- 流式传输端点（两阶段初始化） ----
    if streaming_enabled and hub is not None:
        from acps_sdk.aip.aip_stream_server import handle_stream_request
        from fastapi import Request as _Request
        from fastapi.responses import StreamingResponse as _StreamingResponse

        async def _stream_endpoint(request: _Request) -> _StreamingResponse:
            """lifespan 前（handler 未就绪）返回 503；就绪后委托 handle_stream_request。"""
            if _stream_handler[0] is None:
                raise HTTPException(status_code=503, detail="Stream handler not ready")

            async def _on_stream_start(command: object) -> None:
                sh = _stream_handler[0]
                if sh is not None:
                    await sh.on_stream_start(command)

            return await handle_stream_request(
                request,
                hub,
                _on_stream_start,
                local_aic=partner_aic,
                identity_binding_enabled=identity_binding_enabled,
            )

        app.post("/stream")(_stream_endpoint)

    # ---- 通知端点 ----
    if notification_enabled and notif_service is not None:
        add_aip_notification_router(app, notif_service)

    # ---- RPC 端点 ----
    @app.post("/rpc", response_model=RpcResponse)
    async def rpc_endpoint(rpc_request: RpcRequest, http_request: Request) -> RpcResponse:
        if runner is None:
            raise HTTPException(status_code=503, detail="Runner not ready")

        parent = parse_traceparent(http_request.headers.get(TRACEPARENT_HEADER))
        trace_id = parent.trace_id if parent else new_trace_id()
        span_id = new_span_id()
        parent_span_id = parent.span_id if parent else ""
        command = rpc_request.params.command
        request_bytes = len(rpc_request.model_dump_json().encode("utf-8"))
        request_headers = {TRACEPARENT_HEADER: http_request.headers.get(TRACEPARENT_HEADER, "")}
        if http_request.headers.get("content-type"):
            request_headers["content-type"] = http_request.headers.get("content-type", "")

        t0 = monotonic()
        resp: RpcResponse | None = None
        status_code = 500
        error_info = None
        try:
            resp = await handle_rpc_request(
                http_request,
                runner.handlers,
                local_aic=partner_aic,
                identity_binding_enabled=identity_binding_enabled,
                dispatch_request=runner.dispatch,
            )
            status_code = status_from_rpc_response(resp)
            error_info = error_info_from_rpc_response(resp)
        except Exception as exc:
            status_code = 500
            error_info = ErrorInfo(code=500, message=str(exc))
            raise
        finally:
            duration_ms = int((monotonic() - t0) * 1000)
            response_bytes = len(resp.model_dump_json().encode("utf-8")) if resp is not None else 0
            method = str(command.command.value if hasattr(command.command, "value") else command.command)

            body = AccessBody(
                request=AccessRequest(
                    method=method,
                    url="/rpc",
                    route="/rpc",
                    headers=request_headers,
                    bodySizeBytes=request_bytes,
                ),
                response=AccessResponse(statusCode=status_code, bodySizeBytes=response_bytes),
                caller=AccessParticipant(aic=command.senderId, serviceName="demo-leader"),
                callee=AccessParticipant(aic=runner._aic, serviceName=runner._service_name),
                error=error_info,
                durationMs=duration_ms,
            )
            try:
                await runner._access_emitter.emit(
                    body,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    correlation_id=command.sessionId,
                )
            except Exception:
                logger.warning("Access emit failed", exc_info=True)
        return resp

    @app.post("/group/rpc", response_model=RabbitMQResponse)
    async def group_rpc_endpoint(
        rpc_request: RabbitMQRequest,
        http_request: Request,
    ) -> RabbitMQResponse:
        """群组模式 RPC 端点，用于处理群组邀请（joinGroup）请求"""
        if group_handler is None:
            raise HTTPException(status_code=503, detail="Group handler not ready")
        if identity_binding_enabled:
            try:
                assert_direct_group_request_identity(
                    rpc_request,
                    peer_aic=get_request_peer_aic(http_request),
                )
            except AipIdentityError as exc:
                error = identity_error_to_jsonrpc(exc)
                return RabbitMQResponse(
                    id=rpc_request.id,
                    error=RabbitMQResponseError(
                        code=error.code,
                        message=error.message,
                    ),
                )
        return await group_handler.handle_group_rpc(rpc_request)

    @app.get("/health")
    async def health_check() -> dict[str, Any]:
        return {
            "agent": agent_name,
            "status": "online",
            "tasks": {
                "active": len(runner.tasks) if runner is not None else 0,
            },
            "groups": {
                "active": len(group_handler.active_groups) if group_handler else 0,
            },
        }

    return app


# ---------------------------------------------------------------------------
# 单 Agent 进程入口
# ---------------------------------------------------------------------------


def run_agent_process(agent_name: str, agent_path: str) -> None:
    """在独立进程中启动单个 partner agent 的 uvicorn 服务。"""
    import uvicorn

    config_path = Path(agent_path) / CONFIG_FILENAME
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")  # nosec B104 - configurable development/container bind address
    port = server_cfg.get("port", 9021)

    app = create_agent_app(agent_name, agent_path)

    server_ssl_context = build_ssl_context(agent_path, server_cfg)

    protocol = "https" if server_ssl_context else "http"
    mtls_cfg = server_cfg.get("mtls", {})
    verify_info = ""
    if server_ssl_context and mtls_cfg.get("verify_client", False):
        verify_info = ", client-cert=required"
    logger.info(
        "Starting agent",
        agent=agent_name,
        protocol=protocol,
        host=host,
        port=port,
        verify_info=verify_info,
    )

    uvicorn_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        http=AipPeerCertH11Protocol,
        log_level=config.get("log", {}).get("level", "info").lower(),
        proxy_headers=False,
    )
    uvicorn_config.load()
    uvicorn_config.ssl = server_ssl_context
    uvicorn.Server(uvicorn_config).run()


# ---------------------------------------------------------------------------
# 主入口：发现所有 agent 并启动各自进程
# ---------------------------------------------------------------------------


def _spawn_processes(agents: dict[str, str]) -> dict[str, multiprocessing.Process]:
    """为每个 agent 启动独立子进程。"""
    processes: dict[str, multiprocessing.Process] = {}
    for name, path in agents.items():
        p = multiprocessing.Process(
            target=run_agent_process,
            args=(name, path),
            name=f"partner-{name}",
            daemon=True,
        )
        p.start()
        processes[name] = p
        logger.info("Process started", agent=name, pid=p.pid)
    return processes


def _wait_and_monitor(processes: dict[str, multiprocessing.Process]) -> None:
    """监控所有子进程，任一异常退出则终止全部。"""
    import time

    def shutdown_all(signum: int | None = None, frame: object = None) -> None:
        logger.info("Shutting down all partner processes...")
        terminate_processes(processes)

    signal.signal(signal.SIGTERM, shutdown_all)
    signal.signal(signal.SIGINT, shutdown_all)

    try:
        while True:
            check_process_health(processes, shutdown_all)
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_all()
        raise SystemExit(0) from None


def main() -> None:
    filter_names = sys.argv[1:] if len(sys.argv) > 1 else None
    agents = discover_agents(filter_names)

    if not agents:
        logger.error("No agents found in online directory")
        sys.exit(1)

    validate_ports(agents)

    logger.info("Discovered agents", count=len(agents), agents=list(agents.keys()))
    for name, path in agents.items():
        logger.info("Agent config", agent=name, port=read_agent_port(path))

    processes = _spawn_processes(agents)
    _wait_and_monitor(processes)


if __name__ == "__main__":
    main()
