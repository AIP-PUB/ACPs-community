"""FastAPI 应用入口。

该模块定义了用于本地开发、Dev Container 和 Docker 部署的 ASGI 应用。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.account.api_account import router as account_router
from app.account.api_auth import router as auth_router
from app.agent.api import router_client as agent_router_client
from app.agent.api import router_public as agent_router_public
from app.agent.api import router_staff as agent_router_staff
from app.agent.api_atr import router_mtls as agent_router_atr_mtls
from app.agent.api_atr import router_public as agent_router_atr_public
from app.core.acps_exception import AcpsError
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE, build_problem_details, register_exception_handlers
from app.core.config import settings
from app.core.db_session import AsyncSessionLocal, async_engine, close_sync_engine
from app.core.logging_config import setup_logging
from app.core.oidc import close_oidc_validator, init_oidc_validator
from app.core.otel import init_otel
from app.core.peer_cert import PeerCertificateMiddleware
from app.core.security import limiter, register_security_middleware
from app.eab.api import router_atr as eab_router_atr
from app.eab.api import router_internal as eab_router_internal
from app.file.api import router as file_router
from app.sync.api import router as sync_router
from app.utils.ip_restrict import create_ip_restriction_middleware, parse_allowed_ips
from app.verification.api import router as verification_router

setup_logging(level=settings.log_level, log_format=settings.log_format)
ATR_ALLOWED_NETWORKS = parse_allowed_ips(settings.atr_allow_ip_list)
ATR_IP_RESTRICTION_MIDDLEWARE = create_ip_restriction_middleware(
    ATR_ALLOWED_NETWORKS,
    settings.atr_base_path,
)
METRICS_ENDPOINT_PATH = "/metrics"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """管理应用启动与关闭生命周期。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 启动完成后将控制权交还给 FastAPI。
    """
    init_otel(settings, app)
    await init_oidc_validator()
    yield
    await close_oidc_validator()
    await async_engine.dispose()
    close_sync_engine()


def acps_exception_handler(request: Request, exc: AcpsError) -> JSONResponse:
    """将 ACPs 异常转换为协议要求的响应格式。

    Args:
        request: 当前传入的 HTTP 请求。
        exc: 抛出的 ACPs 异常。

    Returns:
        JSONResponse: 协议约定的错误响应。
    """
    del request
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_response_payload(),
    )


def _dispatch_acps_exception(request: Request, exc: Exception) -> JSONResponse:
    """适配 Starlette 异常处理器签名并委派给 ACPs 处理器。"""
    if not isinstance(exc, AcpsError):
        raise TypeError(f"Unexpected exception type for ACPs handler: {type(exc)!r}")
    return acps_exception_handler(request, exc)


def _create_metrics_instrumentator() -> Instrumentator:
    """创建 Prometheus 指标暴露器。"""
    return Instrumentator(excluded_handlers=[METRICS_ENDPOINT_PATH])


def _register_cors_middleware(app: FastAPI) -> None:
    """按配置为 Web 场景启用 CORS。"""
    if not settings.cors_enabled:
        return

    # CORS 仅对浏览器生效，CLI 不受该策略影响。
    allow_credentials = settings.cors_allow_credentials
    if "*" in settings.cors_origins:
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_allow_origin_regex or None,
        allow_credentials=allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
        expose_headers=settings.cors_expose_headers,
        max_age=settings.cors_max_age,
    )


def _build_app(*, title: str, enable_atr_ip_restriction: bool, enable_cors: bool = False) -> FastAPI:
    """构建共享基础设施后的 FastAPI 应用实例。

    Args:
        title: 应用标题。
        enable_atr_ip_restriction: 是否为 ATR 路径启用 IP 限制。

    Returns:
        FastAPI: 已完成基础设施装配的应用实例。
    """
    app = FastAPI(
        title=title,
        description="Agent Registration and Discovery System API",
        version="0.1.0",
        root_path=settings.root_path,
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    register_security_middleware(
        app,
        ATR_IP_RESTRICTION_MIDDLEWARE if enable_atr_ip_restriction else None,
    )
    if enable_cors:
        _register_cors_middleware(app)
    app.add_exception_handler(AcpsError, _dispatch_acps_exception)
    _register_runtime_routes(app)
    return app


def _register_runtime_routes(app: FastAPI) -> None:
    """注册根路径、健康检查和指标端点。"""

    @app.get("/")
    async def root() -> dict[str, str]:
        """返回 API 根路径响应。

        Returns:
            dict[str, str]: 供 API 发现使用的入口元数据。
        """
        return {
            "message": "Welcome to the Agent Internet Backend API",
            "docs_url": "/docs",
            "redoc_url": "/redoc",
        }

    @app.get("/health")
    @limiter.limit(settings.rate_limit_health)
    async def health(request: Request) -> dict[str, str]:
        """返回轻量级存活检查响应，供容器健康检查使用。

        Returns:
            dict[str, str]: 最小化的存活检查载荷。
        """
        del request
        return {"status": "ok"}

    @app.get("/ready", response_model=None)
    async def ready() -> JSONResponse | dict[str, str]:
        """在确认数据库可连接后返回就绪检查响应。

        Returns:
            JSONResponse | dict[str, str]: 就绪状态载荷，或 503 Problem Details 响应。
        """
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content=build_problem_details(
                    status=503,
                    title="Dependency unavailable",
                    detail="Database connectivity check failed",
                    type_="urn:acps:error:dependencies:dependency-unavailable",
                    extensions={"error_name": "DEPENDENCY_UNAVAILABLE"},
                ),
                media_type=PROBLEM_JSON_MEDIA_TYPE,
            )
        return {"status": "ready"}

    _create_metrics_instrumentator().instrument(app).expose(
        app,
        endpoint=METRICS_ENDPOINT_PATH,
        include_in_schema=False,
    )


def _register_public_routes(app: FastAPI) -> None:
    """注册公开业务面与 ATR 公共端点。"""
    # `/entity` 只挂到 mtls 平面，public app 只保留 ATR 公开读取与管理前置端点。
    app.include_router(auth_router, prefix=settings.api_v1_str)
    app.include_router(account_router, prefix=settings.api_v1_str)
    app.include_router(verification_router, prefix=settings.api_v1_str)
    app.include_router(agent_router_public, prefix=settings.api_v1_str)
    app.include_router(agent_router_client, prefix=settings.api_v1_str)
    app.include_router(agent_router_staff, prefix=settings.api_v1_str)
    app.include_router(agent_router_atr_public, prefix=settings.atr_base_path)
    app.include_router(eab_router_atr, prefix=settings.atr_base_path)
    app.include_router(eab_router_internal)
    app.include_router(file_router, prefix=settings.api_v1_str)
    app.include_router(sync_router, prefix=settings.dsp_base_path, tags=["数据同步协议"])


def _register_mtls_routes(app: FastAPI) -> None:
    """注册 Provider 侧本体证书 mTLS 平面路由。"""
    app.include_router(agent_router_atr_mtls, prefix=settings.atr_base_path)


def create_public_app() -> FastAPI:
    """创建公共业务 HTTPS 应用。"""
    public_app = _build_app(
        title=settings.api_title,
        enable_cors=True,
        enable_atr_ip_restriction=False,
    )
    _register_public_routes(public_app)
    return public_app


def create_mtls_app() -> FastAPI:
    """创建 Provider 侧本体证书 mTLS 应用。"""
    mtls_plane_app = _build_app(
        title=f"{settings.api_title} MTLS Plane",
        enable_atr_ip_restriction=True,
    )
    mtls_plane_app.state.app_env = settings.app_env
    mtls_plane_app.add_middleware(PeerCertificateMiddleware)
    _register_mtls_routes(mtls_plane_app)
    return mtls_plane_app


app = create_public_app()
mtls_app = create_mtls_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.uvicorn_host,
        port=settings.uvicorn_port,
        reload=settings.uvicorn_reload,
        log_level=settings.uvicorn_log_level,
    )
