"""Application factory and dual-listener runtime."""

from __future__ import annotations

import multiprocessing
import signal
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

import structlog
import uvicorn
from cachetools import TTLCache
from fastapi import FastAPI, Request, Response

from app.api.auth import router as auth_router
from app.api.group_problem import GroupApiError, group_api_exception_handler
from app.api.groups import router as groups_router
from app.core.config import Settings, get_settings
from app.core.dev_tls import ensure_runtime_tls_assets
from app.core.logging_config import setup_logging
from app.core.middleware import RequestIdMiddleware
from app.core.peer_cert import PeerCertH11Protocol, PeerCertificateMiddleware
from app.core.tracing import setup_tracing
from app.services.authz import AuthorizationService
from app.services.group_acl import GroupAclService, RedisGroupAclStore
from app.services.rabbitmq_mgmt import RabbitMqManagementClient

logger = structlog.get_logger()

type ListenerName = Literal["auth-api", "group-api"]

AUTH_LISTENER: ListenerName = "auth-api"
GROUP_LISTENER: ListenerName = "group-api"


@dataclass
class ServiceContainer:
    """Shared service objects attached to the FastAPI app state."""

    authz_service: AuthorizationService
    group_acl_service: GroupAclService

    @classmethod
    def from_settings(cls, settings: Settings) -> ServiceContainer:
        logger.info(
            "service_container_build",
            rabbitmq_mgmt_url=settings.rabbitmq_mgmt_url,
            local_cache_ttl=settings.local_cache_ttl_seconds,
        )
        tls_ca_cert = str(settings.redis_tls_ca_cert) if settings.redis_tls_ca_cert else None
        store = RedisGroupAclStore(
            settings.redis_url_value,
            tls_ca_cert=tls_ca_cert,
            tls_check_hostname=settings.redis_tls_check_hostname,
        )
        management_client = RabbitMqManagementClient(
            base_url=settings.rabbitmq_mgmt_url,
            username=settings.rabbitmq_mgmt_user,
            password=settings.rabbitmq_mgmt_password,
        )
        group_acl_service = GroupAclService(
            store=store,
            management_client=management_client,
            local_cache=TTLCache(
                maxsize=4096,
                ttl=settings.local_cache_ttl_seconds,
            ),
            key_ttl_seconds=settings.group_acl_key_ttl_seconds,
        )
        return cls(
            authz_service=AuthorizationService(group_acl_service),
            group_acl_service=group_acl_service,
        )

    async def aclose(self) -> None:
        await self.group_acl_service.aclose()


def _configure_logging(level: str, log_format: str) -> None:
    setup_logging(level, log_format)
    logger.info("logging_configured", log_level=level.upper(), log_format=log_format)


def _build_server_ssl_context(settings: Settings) -> ssl.SSLContext:
    ensure_runtime_tls_assets(settings)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(
        certfile=str(settings.tls_cert_file),
        keyfile=str(settings.tls_key_file),
    )
    ssl_context.load_verify_locations(cafile=str(settings.tls_ca_cert_file))
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3
    return ssl_context


def _get_openapi_version() -> str:
    try:
        return version("mq-auth-server")
    except PackageNotFoundError:
        return "2.2.0"


def _build_app_metadata(listener_name: ListenerName) -> tuple[str, str]:
    if listener_name == AUTH_LISTENER:
        return (
            "MQ Auth Auth API",
            "RabbitMQ HTTP auth backend listener with mTLS identity validation",
        )
    return (
        "MQ Auth Group API",
        "Group ACL management listener with Problem Details error responses",
    )


def _build_openapi_tags(listener_name: ListenerName) -> list[dict[str, str]]:
    shared_tags = [{"name": "ops", "description": "Liveness and readiness endpoints."}]
    if listener_name == AUTH_LISTENER:
        return [
            {
                "name": "rabbitmq-auth",
                "description": "RabbitMQ HTTP auth backend endpoints returning plain text decisions.",
            },
            *shared_tags,
        ]
    return [
        {
            "name": "group-acl",
            "description": "Leader-facing group ACL management endpoints with Problem Details errors.",
        },
        *shared_tags,
    ]


def _register_shared_routes(app: FastAPI) -> None:
    @app.get(
        "/health",
        tags=["ops"],
        summary="健康检查",
        responses={200: {"description": "Listener process is alive."}},
    )
    async def health(request: Request) -> dict[str, str | int]:
        server = request.scope.get("server")
        port = int(server[1]) if isinstance(server, tuple) and len(server) == 2 else 0
        return {"status": "ok", "port": port}

    @app.get(
        "/ready",
        tags=["ops"],
        summary="就绪检查",
        responses={
            200: {"description": "Redis-backed ACL store is reachable."},
            503: {"description": "Redis-backed ACL store is unavailable."},
        },
    )
    async def ready(request: Request) -> Response:
        store = request.app.state.group_acl_service.store
        if await store.ping():
            return Response(
                content='{"status":"ready"}',
                media_type="application/json",
                status_code=200,
            )
        return Response(
            content='{"status":"not_ready"}',
            media_type="application/json",
            status_code=503,
        )


def _configure_listener_routes(app: FastAPI, listener_name: ListenerName) -> None:
    if listener_name == AUTH_LISTENER:
        app.include_router(auth_router)
        return

    app.add_exception_handler(GroupApiError, group_api_exception_handler)
    app.include_router(groups_router)


def _configure_observability(app: FastAPI, listener_name: ListenerName) -> None:
    setup_tracing(app, listener_name=listener_name)

    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    except ImportError:
        logger.warning(
            "prometheus_instrumentator_missing",
            reason="prometheus-fastapi-instrumentator 未安装",
            listener=listener_name,
        )


def create_app(
    *,
    listener_name: ListenerName,
    settings: Settings | None = None,
    services: ServiceContainer | None = None,
) -> FastAPI:
    """Create the FastAPI application for a single listener."""

    resolved_settings = settings or get_settings()
    provided_services = services
    title, description = _build_app_metadata(listener_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("app_lifespan_start", title=app.title)
        active_services = provided_services or ServiceContainer.from_settings(resolved_settings)
        app.state.settings = resolved_settings
        app.state.authz_service = active_services.authz_service
        app.state.group_acl_service = active_services.group_acl_service
        try:
            yield
        finally:
            logger.info("app_lifespan_stop", title=app.title)
            if provided_services is None:
                await active_services.aclose()
                logger.info("service_container_closed", title=app.title)

    app = FastAPI(
        title=title,
        description=description,
        version=_get_openapi_version(),
        openapi_tags=_build_openapi_tags(listener_name),
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.listener_name = listener_name
    if provided_services is not None:
        app.state.authz_service = provided_services.authz_service
        app.state.group_acl_service = provided_services.group_acl_service
    # 中间件注册（后注册先执行：RequestId → PeerCert）
    app.add_middleware(PeerCertificateMiddleware)
    app.add_middleware(RequestIdMiddleware)
    _configure_listener_routes(app, listener_name)
    _register_shared_routes(app)
    _configure_observability(app, listener_name)

    return app


def _serve_listener(listener_name: ListenerName, port: int) -> None:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)
    app = create_app(listener_name=listener_name, settings=settings)
    config = uvicorn.Config(
        app=app,
        host=settings.host,
        port=port,
        http=PeerCertH11Protocol,
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=False,
        lifespan="on",
        timeout_keep_alive=75,
        timeout_graceful_shutdown=30,
        limit_concurrency=1000,
    )
    config.load()
    config.ssl = _build_server_ssl_context(settings)
    logger.info(
        "listener_starting",
        listener=listener_name,
        host=settings.host,
        port=port,
        mtls_required=True,
    )
    server = uvicorn.Server(config)
    server.run()


def _spawn_processes(settings: Settings) -> dict[str, multiprocessing.Process]:
    listeners: dict[ListenerName, int] = {
        GROUP_LISTENER: settings.group_api_port,
        AUTH_LISTENER: settings.auth_api_port,
    }
    processes: dict[str, multiprocessing.Process] = {}
    for name, port in listeners.items():
        logger.info("listener_process_spawn", listener=name, port=port)
        process = multiprocessing.Process(
            target=_serve_listener,
            args=(name, port),
            name=f"mq-auth-svc-{name}",
            daemon=False,
        )
        process.start()
        processes[name] = process
    return processes


def _terminate_processes(processes: dict[str, multiprocessing.Process]) -> None:
    for process in processes.values():
        if process.is_alive():
            logger.info("listener_process_terminate", pid=process.pid)
            process.terminate()
    for process in processes.values():
        process.join(timeout=10)


def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)
    logger.info(
        "service_start",
        host=settings.host,
        auth_port=settings.auth_api_port,
        group_port=settings.group_api_port,
    )
    processes = _spawn_processes(settings)

    def shutdown_all(signum: int, frame: object | None) -> None:
        del frame
        logger.info("service_signal_received", signum=signum)
        _terminate_processes(processes)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown_all)
    signal.signal(signal.SIGTERM, shutdown_all)

    try:
        while True:
            for name, process in processes.items():
                if not process.is_alive():
                    exit_code = process.exitcode
                    logger.error(
                        "listener_process_exited",
                        listener=name,
                        exit_code=exit_code,
                    )
                    _terminate_processes(processes)
                    raise SystemExit(exit_code or 1)
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("service_keyboard_interrupt")
        _terminate_processes(processes)
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
