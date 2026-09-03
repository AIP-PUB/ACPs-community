"""
Partner Agent 基础工具函数

提供 SSL 上下文构建、Agent 发现与端口校验等基础能力，
供 main.py 和测试使用。
"""

import multiprocessing
import os
import ssl
import tomllib
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger

ONLINE_DIR = Path(__file__).parent / "online"
CONFIG_FILENAME = "config.toml"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def get_online_dir -> Path:
 """返回 Partner online 配置目录，支持测试态通过环境变量覆写。"""
    raw_path = os.getenv("PARTNERS_ONLINE_DIR", "").strip
    if raw_path:
        return Path(raw_path)

    cwd_online_dir = Path.cwd / "partners" / "online"
    if cwd_online_dir.is_dir:
        return cwd_online_dir

    return ONLINE_DIR


def resolve_identity_binding_enabled(config: dict[str, Any]) -> bool:
 """解析 demo 全局身份绑定开关。

    测试和本地开发可通过环境变量临时覆写，生产默认从 config.toml 读取，
    缺省值为开启。
 """
    override = os.getenv("AIP_IDENTITY_BINDING_ENABLED", "").strip.lower
    if override:
        if override in _TRUE_VALUES:
            return True
        if override in _FALSE_VALUES:
            return False
        logger.warning(
            "Invalid AIP_IDENTITY_BINDING_ENABLED override, falling back to config",
            value=override,
        )

    app_cfg = config.get("app", {})
    return bool(app_cfg.get("identity_binding_enabled", True))


# ---------------------------------------------------------------------------
# mTLS / SSL
# ---------------------------------------------------------------------------


def build_ssl_context(agent_path: str, server_cfg: dict[str, Any]) -> ssl.SSLContext | None:
 """
    根据 config.toml 中 [server] 和 [server.mtls] 的配置构建 SSL 上下文。

    返回 None 表示使用纯 HTTP。
 """
    mtls_cfg = server_cfg.get("mtls", {})
    tls_enabled = mtls_cfg.get("tls_enabled", False)

    if not tls_enabled:
        return None

    def resolve(p: str) -> Path:
        path = Path(p)
        if path.is_absolute:
            return path
        return Path(agent_path) / p

    cert_file = resolve(mtls_cfg["cert_file"])
    key_file = resolve(mtls_cfg["key_file"])
    ca_file = resolve(mtls_cfg["ca_file"])

    for f, desc in [
        (cert_file, "cert_file"),
        (key_file, "key_file"),
        (ca_file, "ca_file"),
    ]:
        if not f.is_file:
            raise FileNotFoundError(f"mTLS {desc} not found: {f}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    verify_client = mtls_cfg.get("verify_client", False)
    if verify_client:
        ctx.load_verify_locations(cafile=str(ca_file))
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_NONE

    return ctx


def build_client_ssl_context(agent_path: str, server_cfg: dict[str, Any]) -> ssl.SSLContext | None:
 """
    根据 config.toml 中 [server.mtls] 的配置构建客户端 SSL 上下文。

    用于 Partner 作为 HTTP 客户端访问外部 mTLS 服务（如 notification callback）。
    此场景应保留 agent 自身的身份证书，不应被 RabbitMQ 专用证书覆盖。

    返回 None 表示不启用 mTLS 客户端连接。
 """
    mtls_cfg = server_cfg.get("mtls", {})
    tls_enabled = mtls_cfg.get("tls_enabled", False)
    if not tls_enabled:
        return None

    def resolve(p: str) -> Path:
        path = Path(p)
        if path.is_absolute:
            return path
        return Path(agent_path) / p

    cert_file = resolve(mtls_cfg["cert_file"])
    key_file = resolve(mtls_cfg["key_file"])
    ca_file = resolve(mtls_cfg["ca_file"])
    client_cert_file = Path(agent_path) / "client.pem"
    client_key_file = Path(agent_path) / "client.key"

    if client_cert_file.is_file and client_key_file.is_file:
        cert_file = client_cert_file
        key_file = client_key_file

    for f, desc in [
        (cert_file, "cert_file"),
        (key_file, "key_file"),
        (ca_file, "ca_file"),
    ]:
        if not f.is_file:
            raise FileNotFoundError(f"mTLS {desc} not found: {f}")

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def build_rabbitmq_ssl_context(agent_path: str, server_cfg: dict[str, Any]) -> ssl.SSLContext | None:
 """
    构建 RabbitMQ 连接使用的客户端 SSL 上下文。

    若目录内存在 client.pem/client.key，则优先使用它们，便于为 MQ 单独配置
    与 HTTP callback 不同的客户端证书；否则回退到 server.mtls 的证书配置。
 """
    mtls_cfg = server_cfg.get("mtls", {})
    tls_enabled = mtls_cfg.get("tls_enabled", False)
    if not tls_enabled:
        return None

    def resolve(p: str) -> Path:
        path = Path(p)
        if path.is_absolute:
            return path
        return Path(agent_path) / p

    cert_file = resolve(mtls_cfg["cert_file"])
    key_file = resolve(mtls_cfg["key_file"])
    ca_file = resolve(mtls_cfg["ca_file"])
    mq_cert_file = Path(agent_path) / "rabbitmq-client.pem"
    mq_key_file = Path(agent_path) / "rabbitmq-client.key"
    fallback_client_cert_file = Path(agent_path) / "client.pem"
    fallback_client_key_file = Path(agent_path) / "client.key"

    if mq_cert_file.is_file and mq_key_file.is_file:
        cert_file = mq_cert_file
        key_file = mq_key_file
    elif fallback_client_cert_file.is_file and fallback_client_key_file.is_file:
        cert_file = fallback_client_cert_file
        key_file = fallback_client_key_file

    for f, desc in [
        (cert_file, "cert_file"),
        (key_file, "key_file"),
        (ca_file, "ca_file"),
    ]:
        if not f.is_file:
            raise FileNotFoundError(f"mTLS {desc} not found: {f}")

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def build_uvicorn_ssl_kwargs(agent_path: str, server_cfg: dict[str, Any]) -> dict[str, Any]:
 """
    根据 config.toml 中 [server.mtls] 的配置，返回 uvicorn.run 所需的 SSL 关键字参数。

    返回空字典表示使用纯 HTTP。
 """
    mtls_cfg = server_cfg.get("mtls", {})
    tls_enabled = mtls_cfg.get("tls_enabled", False)

    if not tls_enabled:
        return {}

    def resolve(p: str) -> Path:
        path = Path(p)
        if path.is_absolute:
            return path
        return Path(agent_path) / p

    cert_file = resolve(mtls_cfg["cert_file"])
    key_file = resolve(mtls_cfg["key_file"])
    ca_file = resolve(mtls_cfg["ca_file"])

    for f, desc in [
        (cert_file, "cert_file"),
        (key_file, "key_file"),
        (ca_file, "ca_file"),
    ]:
        if not f.is_file:
            raise FileNotFoundError(f"mTLS {desc} not found: {f}")

    kwargs: dict[str, Any] = {
        "ssl_certfile": str(cert_file),
        "ssl_keyfile": str(key_file),
        "ssl_ca_certs": str(ca_file),
    }

    verify_client = mtls_cfg.get("verify_client", False)
    if verify_client:
        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    else:
        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE

    return kwargs


# ---------------------------------------------------------------------------
# Agent 发现与端口校验
# ---------------------------------------------------------------------------


def discover_agents(filter_names: list[str] | None = None) -> dict[str, str]:
 """扫描 online 目录，返回 {agent_name: agent_path} 字典。"""
    agents: dict[str, str] = {}
    online_dir = get_online_dir
    if not online_dir.exists:
        online_dir.mkdir(parents=True, exist_ok=True)
        return agents

    for entry in sorted(online_dir.iterdir, key=lambda e: e.name):
        name = entry.name
        agent_path = str(entry)
        if not entry.is_dir:
            continue
        if not (entry / "acs.json").exists:
            logger.warning("Skipping agent: acs.json missing", agent=name)
            continue
        if not (entry / CONFIG_FILENAME).exists:
            logger.warning("Skipping agent: config missing", agent=name, config=CONFIG_FILENAME)
            continue
        if filter_names and name not in filter_names:
            continue
        agents[name] = agent_path

    return agents


def read_agent_port(agent_path: str) -> int:
 """从 agent 的 config.toml 读取端口号。"""
    config_path = Path(agent_path) / CONFIG_FILENAME
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    return int(cfg.get("server", {}).get("port", 9021))


def validate_ports(agents: dict[str, str]) -> dict[int, str]:
 """验证所有 agent 端口无冲突，返回 {port: agent_name} 映射。"""
    port_map: dict[int, str] = {}
    for name, path in agents.items:
        port = read_agent_port(path)
        if port in port_map:
            raise ValueError(f"Port conflict: {name} and {port_map[port]} both use port {port}")
        port_map[port] = name
    return port_map


# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------


def terminate_processes(processes: dict[str, multiprocessing.Process]) -> None:
 """优雅终止所有子进程。"""
    for name, p in processes.items:
        if p.is_alive:
            logger.info("Terminating process", agent=name, pid=p.pid)
            p.terminate
    for name, p in processes.items:
        p.join(timeout=5)
        if p.is_alive:
            logger.warning("Force killing process", agent=name, pid=p.pid)
            p.kill


def check_process_health(processes: dict[str, multiprocessing.Process], shutdown_fn: Any) -> None:
 """检查所有子进程状态，若任一退出则触发关闭。"""
    import sys

    for name, p in processes.items:
        if not p.is_alive:
            logger.warning("Process exited", agent=name, exit_code=p.exitcode)
            shutdown_fn
            sys.exit(1)
