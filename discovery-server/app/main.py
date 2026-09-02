import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.config import settings
from app.core.database import build_database_url_summary
from app.core.exception_handlers import register_exception_handlers
from app.core.health_probe import (
    PROMETHEUS_CONTENT_TYPE,
    build_health_status,
    build_metrics_payload,
    build_ready_status,
    build_root_status,
    check_database_ready,
)
from app.core.lifespan import lifespan
from app.core.logging_config import configure_logging
from app.core.request_context import request_context_middleware
from app.core.security_headers import security_headers_middleware
from app.discovery.discovery_api import router as discovery_router
from app.heartbeat_sync.api import router as alive_sync_router
from app.sync.api import router as dsp_router

logger = configure_logging(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESC,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path=settings.APP_ROOT_PATH,
    lifespan=lifespan,
)
app.middleware("http")(request_context_middleware)
app.middleware("http")(security_headers_middleware)

app.include_router(discovery_router, prefix="/acps-adp-v2", tags=["用户使用的发现API"])
app.include_router(dsp_router, prefix="/admin/dsp", tags=["数据同步 DSP 的管理维护测试用 API"])
app.include_router(alive_sync_router, prefix="/admin/alive-sync", tags=["AMP Alive 同步的管理维护测试用 API"])
register_exception_handlers(app)


@app.get("/")
async def root(request: Request) -> dict[str, object]:
    """root端点。"""
    return build_root_status(request.app)


@app.get("/health")
async def health() -> dict[str, str]:
    """health 端点。"""

    return build_health_status()


@app.get("/ready")
async def ready() -> JSONResponse:
    """ready 端点。"""

    status_code, payload = build_ready_status(await check_database_ready())
    media_type = PROBLEM_JSON_MEDIA_TYPE if status_code != 200 else None
    return JSONResponse(status_code=status_code, content=payload, media_type=media_type)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    """metrics 端点。"""

    payload = build_metrics_payload(request.app, database_ready=await check_database_ready())
    return PlainTextResponse(content=payload, media_type=PROMETHEUS_CONTENT_TYPE)


def run() -> None:
    logger.info("启动Discovery Server")
    logger.info("服务配置", url=f"http://{settings.UVICORN_HOST}:{settings.UVICORN_PORT}")
    logger.info("API 文档地址", url=f"http://{settings.UVICORN_HOST}:{settings.UVICORN_PORT}/docs")
    logger.info("Registry DSP URL", dsp_base_url=settings.DSP_BASE_URL)
    logger.info("数据库连接", database_url=build_database_url_summary(settings.DATABASE_URL))
    logger.info("自动重载", enabled=settings.UVICORN_RELOAD)
    logger.info("日志级别", level=settings.UVICORN_LOG_LEVEL)

    uvicorn.run(
        "app.main:app",
        host=settings.UVICORN_HOST,
        port=settings.UVICORN_PORT,
        reload=settings.UVICORN_RELOAD,
        log_level=settings.UVICORN_LOG_LEVEL,
    )


if __name__ == "__main__":
    run()
