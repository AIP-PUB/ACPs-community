"""
Leader Agent Platform - Application Entry Point

本模块是 Leader Agent 的应用入口，负责：
- 初始化所有核心组件
- 配置 FastAPI 应用
- 启动/停止生命周期管理

使用方法：
    # 从 leader 目录运行：
    uvicorn main:app --host 0.0.0.0 --port 9031 --reload

    # 或者从项目根目录运行：
    uvicorn leader.main:app --host 0.0.0.0 --port 9031 --reload
"""

import logging
import os
import ssl
from contextlib import asynccontextmanager

from acps_sdk.aip.aip_peer_cert import AipPeerCertH11Protocol, AipPeerCertificateMiddleware
from assistant.amp_setup import LEADER_AIC
from assistant.api import init_routes, router
from assistant.auth import close_auth, init_auth, oidc_enabled
from assistant.config import settings
from assistant.core import (
    GroupConfig,
    GroupManager,
    GroupTaskExecutor,
    Orchestrator,
    RabbitMQConfig,
    SessionManager,
    create_group_manager,
    create_intent_analyzer,
    create_orchestrator,
    get_session_manager,
)
from assistant.core.orchestrator import _build_client_ssl_context
from assistant.heartbeat_setup import start_heartbeat, stop_heartbeat
from assistant.message_setup import LEADER_MESSAGE_EMITTER, get_current_trace
from assistant.metrics_setup import start_metrics, stop_metrics
from assistant.models.exceptions import LeaderAgentError
from assistant.services import ScenarioLoader
from assistant.system_setup import LEADER_SYSTEM_EMITTER
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 配置日志
log_level = settings.get("logging", {}).get("level", "INFO")
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# 配置包级别日志
for package, level in settings.get("logging", {}).get("packages", {}).items():
    logging.getLogger(package).setLevel(level)

logger = logging.getLogger(__name__)

# 全局组件引用
_session_manager: SessionManager | None = None
_scenario_loader: ScenarioLoader | None = None
_orchestrator: Orchestrator | None = None
_group_manager: GroupManager | None = None
_notification_executor = None  # NotificationExecutor | None（仅在 callback_base_url 配置时启用）


def _web_allowed_origins(settings_dict: dict) -> list[str]:
    """返回允许访问 Leader API 的浏览器 origin 列表。"""
    web_config = settings_dict.get("web", {})
    raw_origins = web_config.get("allowed_origins", [])

    if isinstance(raw_origins, str):
        candidates = [raw_origins]
    elif isinstance(raw_origins, list | tuple | set):
        candidates = list(raw_origins)
    else:
        candidates = []

    origins = [str(item).strip() for item in candidates if str(item).strip()]
    if origins:
        return origins

    return [
        "http://localhost:9030",
        "http://127.0.0.1:9030",
    ]


def _build_callback_server_ssl_context(settings_dict: dict) -> ssl.SSLContext | None:
    """构建 Leader callback mTLS 服务端 SSLContext。"""
    mtls_cfg = settings_dict.get("mtls", {})
    cert_file = mtls_cfg.get("server_cert_file", "")
    key_file = mtls_cfg.get("server_key_file", "")
    ca_file = mtls_cfg.get("server_ca_file", "")
    if not cert_file or not key_file or not ca_file:
        return None

    from leader.runtime_paths import resolve_leader_dir

    leader_dir = resolve_leader_dir()
    cert_path = leader_dir / cert_file
    key_path = leader_dir / key_file
    ca_path = leader_dir / ca_file
    for path in (cert_path, key_path, ca_path):
        if not path.is_file():
            return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    ctx.load_verify_locations(cafile=str(ca_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def init_components() -> None:
    """初始化所有核心组件。"""
    global _session_manager, _scenario_loader, _orchestrator, _group_manager

    logger.info("Initializing Leader Agent components...")

    await init_auth()

    # 1. 初始化 Session Manager
    _session_manager = get_session_manager()
    await _session_manager.start()
    logger.info("Session Manager initialized")

    # 2. 初始化 Scenario Loader
    _scenario_loader = ScenarioLoader()
    scenario_count = len(_scenario_loader.scenario_briefs)
    logger.info(f"Scenario Loader initialized: {scenario_count} expert scenarios discovered")

    # 3. 初始化 Intent Analyzer
    intent_analyzer = create_intent_analyzer(_scenario_loader)
    logger.info("Intent Analyzer (LLM-1) initialized")

    # 4. 初始化群组组件（如果启用）
    group_executor = None
    group_config = settings.get("group", {})
    identity_binding_enabled = bool(settings.get("app", {}).get("identity_binding_enabled", True))
    if group_config.get("enabled", False):
        logger.info("Group Mode enabled, initializing group components...")

        # 获取 RabbitMQ 配置
        rabbitmq_config = settings.get("rabbitmq", {})
        rabbitmq_mgmt_config = settings.get("rabbitmq", {}).get("management", {})

        # 获取 leader_aic
        leader_aic = settings.get("app", {}).get("leader_aic", "unknown-leader")

        # 构建 SSL 上下文用于群组 invite 调用
        ssl_context = _build_client_ssl_context(settings)

        # 创建 GroupManager
        _group_manager = create_group_manager(
            leader_aic=leader_aic,
            rabbitmq_config=RabbitMQConfig(
                host=rabbitmq_config.get("host", "localhost"),
                port=rabbitmq_config.get("port", 5671),
                user=rabbitmq_config.get("user"),
                password=rabbitmq_config.get("password"),
                vhost=rabbitmq_config.get("vhost", "acps"),
                auth_service_url=rabbitmq_config.get("auth_service_url"),
                management_host=rabbitmq_mgmt_config.get("host", "localhost"),
                management_port=rabbitmq_mgmt_config.get("port", 15672),
            ),
            group_config=GroupConfig(
                status_probe_interval=group_config.get("status_probe_interval", 30),
                max_wait_seconds=group_config.get("max_wait_seconds", 300),
                partner_join_timeout=group_config.get("partner_join_timeout", 60),
                max_retry_count=group_config.get("max_retry_count", 3),
            ),
            ssl_context=ssl_context,
            identity_binding_enabled=identity_binding_enabled,
            message_emitter=LEADER_MESSAGE_EMITTER,
            trace_context_provider=get_current_trace,
        )
        await _group_manager.start()
        logger.info("GroupManager initialized and started")

        # 设置 SessionManager 的 GroupManager 引用
        _session_manager.set_group_manager(_group_manager)

        # 创建 GroupTaskExecutor
        group_executor = GroupTaskExecutor(
            leader_aic=leader_aic,
            group_manager=_group_manager,
        )
        logger.info("GroupTaskExecutor initialized")
    else:
        logger.info("Group Mode disabled")

    # 5. 初始化 Orchestrator
    _orchestrator = create_orchestrator(
        session_manager=_session_manager,
        scenario_loader=_scenario_loader,
        intent_analyzer=intent_analyzer,
        group_executor=group_executor,
        group_manager=_group_manager,
    )
    # 启动 Orchestrator，初始化所有懒加载组件（包括 HistoryCompressor）
    await _orchestrator.start()
    logger.info("Orchestrator initialized and started")

    # 6. 初始化路由
    init_routes(_orchestrator, _session_manager)
    logger.info("API routes initialized")

    # 6b. 挂载通知回调路由（由 Orchestrator 在 start() 中创建 NotificationExecutor）
    global _notification_executor
    _notification_executor = _orchestrator.notification_executor
    if _notification_executor is not None:
        from assistant.api.notification_routes import register_notification_routes

        register_notification_routes(app, _notification_executor)
        logger.info("Notification callback routes registered at /aip/notifications")

    # 7. 启动周期心跳 + 指标发射（所有组件就绪后）
    start_heartbeat()
    start_metrics()

    logger.info("All Leader Agent components initialized successfully")


async def shutdown_components() -> None:
    """关闭所有核心组件。"""
    global _session_manager, _group_manager, _notification_executor

    logger.info("Shutting down Leader Agent components...")

    # 优先停心跳 + 指标
    await stop_heartbeat()
    await stop_metrics()

    if _notification_executor is not None:
        await _notification_executor.close()
        logger.info("NotificationExecutor closed")

    if _session_manager:
        await _session_manager.stop()
        logger.info("Session Manager stopped")

    if _group_manager:
        await _group_manager.stop()
        logger.info("GroupManager stopped")

    await close_auth()

    logger.info("All Leader Agent components shut down")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理。"""
    # 启动
    await init_components()

    _short_aic = LEADER_AIC[-8:]
    try:
        LEADER_SYSTEM_EMITTER.emit_sync(
            {
                "message": f"Leader service started: aic={_short_aic}",
                "category": "lifecycle",
                "component": "app",
                "module": "startup",
                "aic": LEADER_AIC,
            },
            severity_number=9,
            severity_text="INFO",
        )
    except Exception:
        logger.debug("Failed to emit leader startup lifecycle event", exc_info=True)

    yield

    try:
        LEADER_SYSTEM_EMITTER.emit_sync(
            {
                "message": f"Leader service shutdown: aic={_short_aic}",
                "category": "lifecycle",
                "component": "app",
                "module": "shutdown",
                "aic": LEADER_AIC,
            },
            severity_number=9,
            severity_text="INFO",
        )
    except Exception:
        logger.debug("Failed to emit leader shutdown lifecycle event", exc_info=True)

    # 关闭
    await shutdown_components()


# 创建 FastAPI 应用
app = FastAPI(
    title="Leader Agent Platform",
    description="智能协作平台的中枢调度服务",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(AipPeerCertificateMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_web_allowed_origins(settings),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 全局异常处理
@app.exception_handler(LeaderAgentError)
async def leader_agent_exception_handler(
    request: Request,
    exc: LeaderAgentError,
) -> JSONResponse:
    """处理 Leader Agent 业务异常。"""
    # 从错误码提取 HTTP 状态码
    try:
        status_code = int(str(exc.code)[:3])
    except ValueError, TypeError:
        status_code = 500

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


# 注册路由
app.include_router(router)


# 根路径
@app.get("/")
async def root():
    """根路径信息。"""
    return {
        "service": "Leader Agent Platform",
        "version": "1.0.0",
        "status": "running",
        "api_docs": "/docs",
        "oidcEnabled": oidc_enabled(),
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("LEADER_API_HOST", settings.get("uvicorn", {}).get("host", "0.0.0.0"))
    port = int(os.getenv("LEADER_API_PORT", settings.get("uvicorn", {}).get("port", 9031)))
    reload = os.getenv("UVICORN_RELOAD", str(settings.get("uvicorn", {}).get("reload", False))).lower() == "true"
    callback_base_url = settings.get("app", {}).get("callback_base_url") or None
    identity_binding_enabled = bool(settings.get("app", {}).get("identity_binding_enabled", True))
    callback_server_ssl = _build_callback_server_ssl_context(settings)

    if callback_base_url and identity_binding_enabled:
        if callback_server_ssl is None:
            raise RuntimeError(
                "callback_base_url requires [mtls].server_cert_file/server_key_file/server_ca_file "
                "when app.identity_binding_enabled=true"
            )

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            http=AipPeerCertH11Protocol,
            reload=reload,
        )
        config.load()
        config.ssl = callback_server_ssl
        uvicorn.Server(config).run()
    else:
        uvicorn.run(
            "leader.main:app",
            host=host,
            port=port,
            reload=reload,
        )
