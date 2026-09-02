"""
Partner E2E Tests - Fixtures and Utilities

端到端测试的公共 fixtures，通过 HTTP 直接调用 Partner 服务。
所有测试都是完全的黑盒测试，不使用 TestClient，而是真正的 HTTP 请求。

服务生命周期管理：
- 若指定了 TEST_E2E_BASE_URLS（逗号分隔的基础 URL），使用已部署实例
- 否则自动启动临时实例，完成测试后自动关闭

注意：每个 Partner 运行在独立端口上，测试通过 agent_name 路由到对应端口。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Generator, Iterator
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from acps_sdk.aip.aip_identity import extract_common_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_SERVER_HOST = "127.0.0.1"
TEST_SERVER_STARTUP_TIMEOUT_SECONDS = 30.0

BEIJING_TZ = timezone(timedelta(hours=8))
POLL_INTERVAL = 0.5
MAX_POLL_TIME = 60
MAX_POLL_TIME_LONG = 120

# 扫描 online 目录发现所有 agent 及其端口配置
_PARTNERS_ONLINE_DIR = PROJECT_ROOT / "partners" / "online"
_LEADER_TLS_DIR = PROJECT_ROOT.parent / "demo-leader" / "leader" / "atr"


def _cert_common_name(cert_file: Path) -> str | None:
    cert = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
    return extract_common_name(cert)


def _discover_test_identity() -> tuple[Path, Path, Path, str] | None:
    """获取测试用 mTLS 身份 (cert, key, ca, sender_aic)。"""
    leader_cert_file = _LEADER_TLS_DIR / "client.pem"
    leader_key_file = _LEADER_TLS_DIR / "client.key"
    leader_ca_file = _LEADER_TLS_DIR / "trust-bundle.pem"
    if leader_cert_file.is_file() and leader_key_file.is_file() and leader_ca_file.is_file():
        common_name = _cert_common_name(leader_cert_file)
        if common_name:
            return leader_cert_file, leader_key_file, leader_ca_file, common_name

    if _PARTNERS_ONLINE_DIR.is_dir():
        for entry in sorted(_PARTNERS_ONLINE_DIR.iterdir(), key=lambda e: e.name):
            mq_cert_file = entry / "client.pem"
            mq_key_file = entry / "client.key"
            ca_file = entry / "trust-bundle.pem"
            if mq_cert_file.is_file() and mq_key_file.is_file() and ca_file.is_file():
                common_name = _cert_common_name(mq_cert_file)
                if common_name:
                    return mq_cert_file, mq_key_file, ca_file, common_name

            cert_file = entry / "server.pem"
            key_file = entry / "server.key"
            if cert_file.is_file() and key_file.is_file() and ca_file.is_file():
                common_name = _cert_common_name(cert_file)
                if common_name:
                    return cert_file, key_file, ca_file, common_name
    return None


_TEST_IDENTITY = _discover_test_identity()
TEST_CLIENT_CERT_FILE = _TEST_IDENTITY[0] if _TEST_IDENTITY else None
TEST_CLIENT_KEY_FILE = _TEST_IDENTITY[1] if _TEST_IDENTITY else None
TEST_CA_CERT_FILE = _TEST_IDENTITY[2] if _TEST_IDENTITY else None
TEST_SENDER_AIC = _TEST_IDENTITY[3] if _TEST_IDENTITY else "test-leader-e2e"
FORGED_SENDER_AIC = "1.2.156.3088.1.1.D55UOU.NEBZUA.1.0QLD"


def _build_test_client_ssl_context() -> ssl.SSLContext:
    """构建 E2E 测试专用 SSLContext，避免使用已弃用的 cert= 参数。"""
    if TEST_CA_CERT_FILE is not None:
        context = ssl.create_default_context(cafile=str(TEST_CA_CERT_FILE))
    else:
        context = ssl.create_default_context()
    context.check_hostname = False
    if TEST_CLIENT_CERT_FILE is not None and TEST_CLIENT_KEY_FILE is not None:
        context.load_cert_chain(
            certfile=str(TEST_CLIENT_CERT_FILE),
            keyfile=str(TEST_CLIENT_KEY_FILE),
        )
    return context


_TEST_CLIENT_SSL_CONTEXT = _build_test_client_ssl_context()


def _apply_test_rabbitmq_overrides(env: dict[str, str]) -> None:
    """仅向 E2E 子进程传递隔离 RabbitMQ 的连接覆盖项。"""
    for field_name in ("HOST", "PORT", "USER", "PASSWORD", "VHOST"):
        value = os.getenv(f"TEST_RABBITMQ_{field_name}")
        if value:
            env[f"RABBITMQ_{field_name}"] = value


def _discover_agent_urls(online_dir: Path | None = None) -> dict[str, str]:
    """扫描 online 目录，返回所有 agent 的 URL 映射 {name: url}。"""
    agent_urls: dict[str, str] = {}
    active_online_dir = online_dir or _PARTNERS_ONLINE_DIR
    if not active_online_dir.is_dir():
        return agent_urls
    for entry in sorted(active_online_dir.iterdir(), key=lambda e: e.name):
        config_path = entry / "config.toml"
        if config_path.is_file():
            try:
                with config_path.open("rb") as f:
                    cfg = tomllib.load(f)
                port = cfg.get("server", {}).get("port")
                if port:
                    # 根据配置判断是否启用 TLS
                    tls_enabled = cfg.get("server", {}).get("mtls", {}).get("tls_enabled", False)
                    protocol = "https" if tls_enabled else "http"
                    agent_urls[entry.name] = f"{protocol}://{TEST_SERVER_HOST}:{port}"
            except OSError, tomllib.TOMLDecodeError, TypeError:
                continue
    return agent_urls


AGENT_URLS = _discover_agent_urls()


def _reserve_free_port() -> tuple[int, socket.socket]:
    """预留一个临时 TCP 端口，降低并发测试下的抢占概率。"""
    reserved_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved_socket.bind((TEST_SERVER_HOST, 0))
    reserved_socket.listen(1)
    port = reserved_socket.getsockname()[1]
    return port, reserved_socket


def _rewrite_server_port(config_path: Path, port: int) -> None:
    """把复制后的 Partner config.toml 改写为临时端口。"""
    content = config_path.read_text(encoding="utf-8")
    updated_content, replaced_count = re.subn(
        r"(?m)^port\s*=\s*\d+\s*$",
        f"port = {port}",
        content,
        count=1,
    )
    if replaced_count != 1:
        raise RuntimeError(f"无法在 {config_path} 中定位 server.port 配置")
    config_path.write_text(updated_content, encoding="utf-8")


def _prepare_temp_online_dir() -> tuple[Path, dict[str, str], list[socket.socket]]:
    """复制 Partner online 配置到临时目录，并为每个 agent 分配临时端口。"""
    runtime_root = Path(tempfile.mkdtemp(prefix="demo-partner-e2e-online-"))
    temp_online_dir = runtime_root / "online"
    shutil.copytree(_PARTNERS_ONLINE_DIR, temp_online_dir)

    reserved_sockets: list[socket.socket] = []
    for entry in sorted(temp_online_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue

        config_path = entry / "config.toml"
        if not config_path.is_file():
            continue

        port, reserved_socket = _reserve_free_port()
        reserved_sockets.append(reserved_socket)
        _rewrite_server_port(config_path, port)

    return runtime_root, _discover_agent_urls(temp_online_dir), reserved_sockets


def _build_startup_error(message: str, log_path: Path) -> RuntimeError:
    """拼接临时测试实例启动失败时的错误信息。"""
    log_output = log_path.read_text(encoding="utf-8", errors="replace").strip()
    if log_output:
        return RuntimeError(f"{message}\n\n临时实例日志（末尾 100 行）：\n" + "\n".join(log_output.split("\n")[-100:]))
    return RuntimeError(message)


def _wait_for_agents_ready(agent_urls: dict[str, str], process: subprocess.Popen[str], log_path: Path) -> None:
    """等待临时测试实例完成启动，所有 agent /health 检查通过。"""
    deadline = time.monotonic() + TEST_SERVER_STARTUP_TIMEOUT_SECONDS

    with httpx.Client(timeout=1.0, verify=_TEST_CLIENT_SSL_CONTEXT) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _build_startup_error("E2E 临时测试实例启动失败。", log_path)

            # 检查所有 agent 的 /health
            all_ready = True
            for url in agent_urls.values():
                try:
                    response = client.get(f"{url}/health")
                    if response.status_code != 200:
                        all_ready = False
                        break
                except (
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    OSError,
                ):
                    all_ready = False
                    break

            if all_ready:
                return

            time.sleep(1)

    if process.poll() is not None:
        raise _build_startup_error("E2E 临时测试实例在健康检查前退出。", log_path)

    raise _build_startup_error(
        f"等待 E2E 临时测试实例就绪超时（{TEST_SERVER_STARTUP_TIMEOUT_SECONDS}s）。",
        log_path,
    )


def _managed_e2e_agent_urls() -> Iterator[dict[str, str]]:
    """在未显式注入 TEST_E2E_BASE_URLS 时，自管理一个临时测试实例。

    返回 agent_name → base_url 的映射。
    """
    file_descriptor, log_path_str = tempfile.mkstemp(prefix="demo-partner-e2e-", suffix=".log")
    os.close(file_descriptor)
    log_path = Path(log_path_str)

    runtime_root, runtime_agent_urls, reserved_sockets = _prepare_temp_online_dir()
    env = os.environ.copy()
    _apply_test_rabbitmq_overrides(env)
    env["PARTNERS_ONLINE_DIR"] = str(runtime_root / "online")

    with log_path.open("w+", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "partners.main"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            for reserved_socket in reserved_sockets:
                reserved_socket.close()
            reserved_sockets.clear()

            _wait_for_agents_ready(runtime_agent_urls, process, log_path)
            yield runtime_agent_urls
        finally:
            for reserved_socket in reserved_sockets:
                reserved_socket.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    with suppress(FileNotFoundError):
        log_path.unlink()
    shutil.rmtree(runtime_root, ignore_errors=True)


# =============================================================================
# Helper Functions
# =============================================================================


def create_task_command(
    text: str,
    command: str,
    task_id: str,
    session_id: str,
    command_id: str | None = None,
    sender_id: str | None = None,
) -> dict[str, Any]:
    """创建 AIP v2 TaskCommand"""
    now = datetime.now(BEIJING_TZ)
    return {
        "type": "task-command",
        "id": command_id or f"cmd-{now.timestamp()}",
        "sentAt": now.isoformat(),
        "senderRole": "leader",
        "senderId": sender_id or TEST_SENDER_AIC,
        "command": command,
        "dataItems": [{"type": "text", "text": text}] if text else [],
        "taskId": task_id,
        "sessionId": session_id,
    }


def create_rpc_request(command: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    """创建完整的 RPC 请求 (AIP v2 格式)"""
    return {
        "jsonrpc": "2.0",
        "id": request_id or f"rpc-{uuid.uuid4().hex[:12]}",
        "method": "rpc",
        "params": {"command": command},
    }


def _get_agent_url(agent_name: str, agent_urls: dict[str, str]) -> str:
    """根据 agent_name 返回对应的 base URL。"""
    if agent_name in agent_urls:
        return agent_urls[agent_name]
    # 降级：使用第一个可用的 agent URL
    return next(iter(agent_urls.values())) if agent_urls else f"https://{TEST_SERVER_HOST}:9021"


def send_rpc(
    client: httpx.Client,
    agent_name: str,
    text: str,
    command: str,
    task_id: str,
    session_id: str,
    agent_urls: dict[str, str],
    sender_id: str | None = None,
) -> dict[str, Any]:
    """发送 RPC 请求到对应 agent 的独立端口。"""
    task_command = create_task_command(text, command, task_id, session_id, sender_id=sender_id)
    request = create_rpc_request(task_command)
    base_url = _get_agent_url(agent_name, agent_urls)

    response = client.post(
        f"{base_url}/rpc",
        json=request,
        timeout=30.0,
    )

    return {
        "status_code": response.status_code,
        "data": response.json() if response.status_code == 200 else None,
        "raw_response": response,
    }


def poll_task_state(
    client: httpx.Client,
    agent_name: str,
    task_id: str,
    session_id: str,
    target_states: list[str],
    agent_urls: dict[str, str],
    max_time: float = MAX_POLL_TIME,
    poll_interval: float = POLL_INTERVAL,
) -> dict[str, Any]:
    """
    轮询任务状态直到达到目标状态或超时。

    Returns:
        {
            "converged": bool,
            "final_state": str,
            "final_result": dict,
            "poll_count": int,
            "elapsed_time": float,
            "state_history": List[str],
        }
    """
    start_time = time.time()
    poll_count = 0
    state_history = []
    last_state = None

    while time.time() - start_time < max_time:
        poll_count += 1
        result = send_rpc(client, agent_name, "", "get", task_id, session_id, agent_urls)

        if result["status_code"] != 200 or not result["data"]:
            time.sleep(poll_interval)
            continue

        task_data = result["data"].get("result", {})
        current_state = task_data.get("status", {}).get("state")

        if current_state and current_state != last_state:
            state_history.append(current_state)
            last_state = current_state

        if current_state in target_states:
            return {
                "converged": True,
                "final_state": current_state,
                "final_result": result["data"],
                "poll_count": poll_count,
                "elapsed_time": time.time() - start_time,
                "state_history": state_history,
            }

        time.sleep(poll_interval)

    # 超时
    final_result = send_rpc(client, agent_name, "", "get", task_id, session_id, agent_urls)
    return {
        "converged": False,
        "final_state": last_state,
        "final_result": final_result.get("data"),
        "poll_count": poll_count,
        "elapsed_time": time.time() - start_time,
        "state_history": state_history,
    }


# =============================================================================
# Pytest Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def agent_urls() -> Generator[dict[str, str]]:
    """返回 agent_name → base_url 的映射；自动启动临时实例或使用已部署实例。"""
    # 检查是否指定了已部署实例
    base_urls_env = os.getenv("TEST_E2E_BASE_URLS", "").strip()
    if base_urls_env:
        # TEST_E2E_BASE_URLS 格式：beijing_food=https://127.0.0.1:9021,china_hotel=https://127.0.0.1:9024
        agent_urls_dict = {}
        for pair in base_urls_env.split(","):
            if "=" in pair:
                name, url = pair.split("=", 1)
                agent_urls_dict[name.strip()] = url.strip().rstrip("/")
        if agent_urls_dict:
            yield agent_urls_dict
            return

    # 自动启动临时实例
    yield from _managed_e2e_agent_urls()


@pytest.fixture(scope="module")
def http_client() -> Generator[httpx.Client]:
    """提供 HTTP 客户端（模块级别）"""
    with httpx.Client(timeout=30.0, verify=_TEST_CLIENT_SSL_CONTEXT) as client:
        yield client


@pytest.fixture(scope="module")
def available_agents(agent_urls: dict[str, str]) -> list[str]:
    """返回可用的 Agent 列表"""
    return list(agent_urls.keys())


@pytest.fixture
def unique_ids() -> dict[str, str]:
    """生成唯一的 task_id 和 session_id"""
    timestamp = datetime.now(BEIJING_TZ).timestamp()
    return {
        "task_id": f"e2e-task-{timestamp}",
        "session_id": f"e2e-session-{timestamp}",
    }


@pytest.fixture
def rpc_helper(http_client: httpx.Client, agent_urls: dict[str, str]) -> Any:
    """提供 RPC 调用辅助函数"""

    class RpcHelper:
        def __init__(self, client: httpx.Client, urls: dict[str, str]) -> None:
            self.client = client
            self.agent_urls = urls

        def start(self, agent_name: str, text: str, task_id: str, session_id: str) -> dict[str, Any]:
            """发送 START 命令"""
            return send_rpc(
                self.client,
                agent_name,
                text,
                "start",
                task_id,
                session_id,
                self.agent_urls,
            )

        def get(self, agent_name: str, task_id: str, session_id: str) -> dict[str, Any]:
            """发送 GET 命令"""
            return send_rpc(self.client, agent_name, "", "get", task_id, session_id, self.agent_urls)

        def continue_task(self, agent_name: str, text: str, task_id: str, session_id: str) -> dict[str, Any]:
            """发送 CONTINUE 命令"""
            return send_rpc(
                self.client,
                agent_name,
                text,
                "continue",
                task_id,
                session_id,
                self.agent_urls,
            )

        def complete(self, agent_name: str, task_id: str, session_id: str) -> dict[str, Any]:
            """发送 COMPLETE 命令"""
            return send_rpc(
                self.client,
                agent_name,
                "确认完成",
                "complete",
                task_id,
                session_id,
                self.agent_urls,
            )

        def cancel(self, agent_name: str, task_id: str, session_id: str) -> dict[str, Any]:
            """发送 CANCEL 命令"""
            return send_rpc(
                self.client,
                agent_name,
                "",
                "cancel",
                task_id,
                session_id,
                self.agent_urls,
            )

        def poll_until(
            self,
            agent_name: str,
            task_id: str,
            session_id: str,
            target_states: list[str],
            max_time: float = MAX_POLL_TIME,
        ) -> dict[str, Any]:
            """轮询直到达到目标状态"""
            return poll_task_state(
                self.client,
                agent_name,
                task_id,
                session_id,
                target_states,
                self.agent_urls,
                max_time,
            )

    return RpcHelper(http_client, agent_urls)
