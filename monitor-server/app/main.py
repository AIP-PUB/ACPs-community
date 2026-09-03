"""AMP Monitor Server — Query API 应用入口。

提供 Audit 日志的 HTTP Query API（FastAPI），以及 Audit Writer（Kafka Consumer）后台任务。
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.core.base_exception import register_exception_handlers
from app.core.config import settings
from app.core.db_session import close_async_engine, get_async_engine
from app.core.health_probe import check_clickhouse as _check_ch
from app.core.health_probe import check_database
from app.core.health_probe import check_opensearch as _check_os
from app.core.logging_config import setup_logging
from app.core.oidc import close_oidc, init_oidc
from app.core.redis_client import check_redis, close_redis
from app.metrics.tsdb import check_victoria_metrics as _check_vm

setup_logging(level=settings.log_level, log_format=settings.log_format)

logger = structlog.get_logger(__name__)

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_hex_segment(value: str, *, expected_length: int) -> bool:
    """校验 traceparent 片段是否为指定长度的十六进制字符串。"""
    return len(value) == expected_length and all(c in _HEX_DIGITS for c in value)


def _extract_trace_context(traceparent: str | None) -> tuple[str, str]:
    """从 W3C traceparent 头中提取 trace_id 和 span_id。

    Args:
        traceparent: W3C traceparent 头字符串。

    Returns:
        tuple[str, str]: (trace_id, span_id) 元组，解析失败时返回空字符串。
    """
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

    启动时：初始化 Audit Writer（Kafka Consumer）后台任务。
    测试模式（APP_ENV=testing）跳过 Kafka Consumer，只提供 Query API。
    关闭时：停止 Kafka Consumer，关闭数据库连接池。

    Args:
        app: FastAPI 应用实例。
    """
    from app.audit.writer import AuditWriter

    audit_writer = AuditWriter()
    writer_task: asyncio.Task[None] | None = None
    kafka_started = False

    from app.heartbeat.exception import HeartbeatConfigError
    from app.heartbeat.runtime import HeartbeatRuntime

    heartbeat_runtime = HeartbeatRuntime()

    from app.metrics.exception import MetricsConfigError
    from app.metrics.runtime import MetricsRuntime

    metrics_runtime = MetricsRuntime()

    from app.access.exception import AccessConfigError
    from app.access.runtime import AccessRuntime

    access_runtime = AccessRuntime()

    from app.message.exception import MessageConfigError
    from app.message.runtime import MessageRuntime

    message_runtime = MessageRuntime()

    from app.system.exception import SystemConfigError
    from app.system.runtime import SystemRuntime

    system_runtime = SystemRuntime()

    try:
        await init_oidc()
        if settings.app_env != "testing":
            try:
                await audit_writer.start()
                kafka_started = True
                writer_task = asyncio.create_task(audit_writer.run(), name="audit-writer")
                logger.info("AMP Monitor Server 已启动（含 Kafka Consumer）", port=settings.uvicorn_port)
            except Exception as exc:
                logger.warning(
                    "Kafka Consumer 启动失败，服务以降级模式运行（仅 Query API）",
                    error=str(exc),
                )

            try:
                await heartbeat_runtime.start()
            except HeartbeatConfigError:
                raise
            except Exception as exc:
                logger.warning("Heartbeat 后台任务启动失败，服务以降级模式运行", error=str(exc))

            try:
                await metrics_runtime.start()
            except MetricsConfigError:
                raise
            except Exception as exc:
                logger.warning("Metrics 后台任务启动失败，服务以降级模式运行", error=str(exc))

            try:
                await access_runtime.start()
            except AccessConfigError:
                raise  # 配置非法 → fail-fast，拒绝启动
            except Exception as exc:
                logger.warning("Access 后台任务启动失败，服务以降级模式运行", error=str(exc))

            try:
                await message_runtime.start()
            except MessageConfigError:
                raise  # 配置非法 → fail-fast，拒绝启动
            except Exception as exc:
                logger.warning("Message 后台任务启动失败，服务以降级模式运行", error=str(exc))

            try:
                await system_runtime.start()
            except SystemConfigError:
                raise  # 配置非法 → fail-fast，拒绝启动
            except Exception as exc:
                logger.warning("System 后台任务启动失败，服务以降级模式运行", error=str(exc))
        else:
            logger.info("测试模式：跳过 Kafka Consumer 启动，仅提供 Query API")
            # testing 模式下也需要完成 DDL bootstrap + 配置校验
            await message_runtime.start()
            try:
                await system_runtime.start()
            except SystemConfigError:
                raise  # 配置非法 → fail-fast
            except Exception as exc:
                logger.warning(
                    "System bootstrap 失败（测试模式，非致命；OpenSearch 未启动则跳过）",
                    error=str(exc),
                )

        yield
    finally:
        if kafka_started:
            await audit_writer.stop()
        if writer_task is not None and not writer_task.done():
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task
        await heartbeat_runtime.stop()
        await metrics_runtime.stop()
        await access_runtime.stop()
        await message_runtime.stop()
        await system_runtime.stop()
        await close_oidc()
        await close_async_engine()
        await close_redis()
        logger.info("AMP Monitor Server 已关闭")


app = FastAPI(
    title=settings.api_title,
    version=settings.project_version,
    description="AMP Monitor Server — Audit 日志查询接口",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """将 request_id 与 trace 上下文绑定到 structlog，并回写响应头。"""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    trace_id, span_id = _extract_trace_context(request.headers.get("traceparent"))

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id, span_id=span_id)
    request.state.request_id = request_id

    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()

    response.headers["X-Request-ID"] = request_id
    return response


if settings.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
        expose_headers=settings.cors_expose_headers,
        max_age=settings.cors_max_age,
    )

register_exception_handlers(app)

from app.access.api import router as _access_router  # noqa: E402
from app.audit.api import router as audit_router  # noqa: E402
from app.heartbeat.api import router as heartbeat_router  # noqa: E402
from app.message.api import router as _message_router  # noqa: E402
from app.metrics.api import router as _metrics_router_full  # noqa: E402
from app.system.api import router as _system_router  # noqa: E402

app.include_router(audit_router, prefix=settings.api_v1_str)
app.include_router(heartbeat_router, prefix=settings.api_v1_str)

# Metrics：snapshots/query + series/query 始终注册；
# rankings/query 受 metrics_analytics_enabled 控制；
# slo/evaluate + capacity/saturation 受 metrics_governance_enabled 控制（§6.19，同 Heartbeat silence/top）。
app.include_router(_metrics_router_full, prefix=settings.api_v1_str)

# Access：operations/query + events/query 始终注册（Core Profile）；
# analytics/apm 端点由 access_analytics_enabled / access_apm_enabled 控制（api.py 内条件化注册）。
app.include_router(_access_router, prefix=settings.api_v1_str)

# Message：events/query 始终注册（Core Profile）；
# lifecycle/deadletters 由 message_reliability_enabled 控制；
# destinations/throughput 由 message_destination_enabled 控制；
# destinations/query 由 message_destination_enabled AND message_state_collector_enabled 控制（api.py 内条件化注册）。
app.include_router(_message_router, prefix=settings.api_v1_str)

# System：events/query 始终注册（Core Profile）；system_query_enabled 可关（只写部署）。
app.include_router(_system_router, prefix=settings.api_v1_str)


@app.get("/health", summary="健康检查")
async def health_check() -> JSONResponse:
    """检查数据库、Redis、VictoriaMetrics、ClickHouse、OpenSearch 连通性并返回服务健康状态。

    OpenSearch 不计入 all_ok：System 模块不可用时其余五个模块仍可正常服务，
    OpenSearch 状态在 checks.opensearch 字段中单独报告（监控告警用）。
    """
    db_ok = await check_database(get_async_engine())
    redis_ok = await check_redis()
    vm_ok = await _check_vm()
    ch_ok = await _check_ch()
    os_ok = await _check_os()
    all_ok = db_ok and redis_ok and vm_ok and ch_ok
    payload = {
        "status": "ok" if all_ok else "degraded",
        "service": "AMP Monitor Server",
        "version": settings.project_version,
        "environment": settings.app_env,
        "checks": {
            "database": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
            "victoria_metrics": "ok" if vm_ok else "error",
            "clickhouse": "ok" if ch_ok else "error",
            "opensearch": "ok" if os_ok else "error",
        },
    }
    status_code = 200 if all_ok else 503
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/", summary="根路径")
async def root() -> dict[str, str]:
    """返回根路径欢迎信息。

    Returns:
        dict[str, str]: 根路径响应。
    """
    return {
        "message": "欢迎使用 AMP Monitor Server",
        "docs": "/docs",
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
