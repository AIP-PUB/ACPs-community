"""Agent CA 认证服务 - 应用入口点。

这是基于 FastAPI 开发的 Agent CA 认证系统主入口文件。
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.acme.api import router as acme_router
from app.acme.error_handler import ACMEErrorHandler
from app.certificates.api import router as certificates_router
from app.certificates.api_ext import router as ext_router
from app.certificates.api_keys import router as keys_router
from app.common.ocsp_service import OCSPService
from app.core.atr_ip_filter import ATRManagementIPFilterMiddleware
from app.core.base_exception import register_exception_handlers
from app.core.ca_manager import get_ca_manager
from app.core.config import settings
from app.core.db_session import async_session_factory, close_async_engine, close_sync_engine
from app.core.logging_config import setup_logging
from app.crl.api import router as crl_router
from app.ocsp.api import router as ocsp_router

setup_logging(level=settings.log_level, log_format=settings.log_format)

HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_hex_segment(value: str, *, expected_length: int) -> bool:
    """校验 traceparent 片段是否为指定长度的十六进制字符串"""

    return len(value) == expected_length and all(character in HEX_DIGITS for character in value)


def _extract_trace_context(traceparent: str | None) -> tuple[str, str]:
    """从 W3C traceparent 头中提取 trace_id 和 span_id"""
    if not traceparent:
        return "", ""

    parts = traceparent.strip().split("-")
    if len(parts) != 4:
        return "", ""

    version, trace_id, span_id, trace_flags = parts
    if version.lower() == "ff":
        return "", ""
    if not _is_hex_segment(version, expected_length=2):
        return "", ""
    if not _is_hex_segment(trace_id, expected_length=32) or trace_id == "0" * 32:
        return "", ""
    if not _is_hex_segment(span_id, expected_length=16) or span_id == "0" * 16:
        return "", ""
    if not _is_hex_segment(trace_flags, expected_length=2):
        return "", ""

    return trace_id.lower(), span_id.lower()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """管理应用生命周期。

    Args:
        app: FastAPI 应用实例。

    Returns:
        异步上下文管理器。
    """
    # 强制启动时初始化 CAManager；文件校验失败则抛出异常，中止启动
    get_ca_manager()
    async with async_session_factory() as session:
        service = OCSPService(session)
        await service.bootstrap_default_responder()
        await session.commit()
    yield
    await close_async_engine()
    close_sync_engine()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Agent CA 认证系统后端 API",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """将 request_id 与 trace 上下文绑定到 structlog，并回写响应头"""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    trace_id, span_id = _extract_trace_context(request.headers.get("traceparent"))

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id, span_id=span_id)
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    request.state.span_id = span_id

    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()

    response.headers["X-Request-ID"] = request_id
    return response


app.add_middleware(ACMEErrorHandler)
app.add_middleware(ATRManagementIPFilterMiddleware)
register_exception_handlers(app)

app.include_router(acme_router, prefix="/acps-atr-v2/acme", tags=["ACME"])
app.include_router(
    certificates_router,
    prefix="/admin/certificates",
    tags=["不在ACPs体系中的证书管理，基本的功能"],
)
app.include_router(ext_router, prefix="/acps-atr-v2/ca", tags=["Extension API"])
app.include_router(keys_router, prefix="/acps-atr-v2/ca", tags=["Public Key Query"])
app.include_router(crl_router, prefix="/acps-atr-v2/crl", tags=["CRL"])
app.include_router(ocsp_router, prefix="/acps-atr-v2/ocsp", tags=["OCSP"])


@app.get("/health")
async def health_check() -> dict[str, str]:
    """返回应用健康状态。

    Returns:
        健康检查响应。
    """
    return {
        "status": "healthy",
        "service": "Agent CA API",
        "version": settings.app_version,
        "environment": settings.app_env,
    }


@app.get("/")
async def root() -> dict[str, str]:
    """返回根路径欢迎信息。

    Returns:
        根路径响应。
    """
    return {
        "message": "欢迎使用 Agent CA 认证服务 API",
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.uvicorn_host,
        port=settings.uvicorn_port,
        reload=settings.uvicorn_reload,
        log_level=settings.uvicorn_log_level,
    )
