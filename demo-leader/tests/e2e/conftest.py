"""demo-leader E2E 运行时管理。"""

from __future__ import annotations

import json
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
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from acps_sdk.aip.aip_identity import extract_common_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEADER_ROOT = PROJECT_ROOT / "leader"
DEMO_PARTNER_ROOT = PROJECT_ROOT.parent / "demo-partner"
PARTNER_ONLINE_DIR = DEMO_PARTNER_ROOT / "partners" / "online"
PARTNER_PYTHON = DEMO_PARTNER_ROOT / ".venv" / "bin" / "python"
PARTNER_TLS_DIR = DEMO_PARTNER_ROOT / "partners" / "online" / "beijing_food"
LEADER_CERT_FILE = LEADER_ROOT / "atr" / "client.pem"
LEADER_KEY_FILE = LEADER_ROOT / "atr" / "client.key"
LEADER_TRUST_BUNDLE = LEADER_ROOT / "atr" / "trust-bundle.pem"
LEADER_SERVER_CERT_FILE = PARTNER_TLS_DIR / "server.pem"
LEADER_SERVER_KEY_FILE = PARTNER_TLS_DIR / "server.key"
RABBITMQ_TLS_DIR = PROJECT_ROOT.parent / "acps-infra" / "dev-infra" / "certs" / "issued" / "rabbitmq"
RABBITMQ_CLIENT_CERT_FILE = RABBITMQ_TLS_DIR / "rabbitmq-client.pem"
RABBITMQ_CLIENT_KEY_FILE = RABBITMQ_TLS_DIR / "rabbitmq-client.key"
RABBITMQ_CA_FILE = RABBITMQ_TLS_DIR / "trust-bundle.pem"
RABBITMQ_MGMT_URL = os.getenv("RABBITMQ_MGMT_URL", "http://localhost:15672")
RABBITMQ_ADMIN_USER = "admin"
RABBITMQ_ADMIN_PASSWORD = "devpass"
TEST_CONNECT_HOST = "localhost"
TEST_BIND_HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 45.0

BEIJING_FOOD_ACS = "beijing_food.json"
BEIJING_RURAL_ACS = "beijing_rural.json"
BEIJING_URBAN_ACS = "beijing_urban.json"
CHINA_HOTEL_ACS = "china_hotel.json"
CHINA_TRANSPORT_ACS = "china_transport.json"

STATIC_TOUR_MAPPING = {
    "hotel": [CHINA_HOTEL_ACS],
    "food": [BEIJING_FOOD_ACS],
    "intercity_transport": [CHINA_TRANSPORT_ACS],
    "local_transport": [BEIJING_URBAN_ACS, BEIJING_RURAL_ACS],
    "attraction": [BEIJING_URBAN_ACS, BEIJING_RURAL_ACS],
}
ACS_FILE_BY_AGENT = {
    "beijing_food": BEIJING_FOOD_ACS,
    "beijing_rural": BEIJING_RURAL_ACS,
    "beijing_urban": BEIJING_URBAN_ACS,
    "china_hotel": CHINA_HOTEL_ACS,
    "china_transport": CHINA_TRANSPORT_ACS,
}


def _apply_test_rabbitmq_overrides(env: dict[str, str]) -> None:
    """仅向 E2E 启动的服务子进程传递隔离 RabbitMQ 配置。"""
    for field_name in ("HOST", "PORT", "USER", "PASSWORD", "VHOST"):
        value = os.getenv(f"TEST_RABBITMQ_{field_name}")
        if value:
            env[f"RABBITMQ_{field_name}"] = value


@dataclass(slots=True)
class LeaderE2ERuntime:
    """封装 demo-leader E2E 所需的临时运行时信息。"""

    leader_base_url: str
    client_ssl_context: ssl.SSLContext
    partner_health_url: str | None = None
    partner_urls: dict[str, str] = field(default_factory=dict)
    rabbitmq_mgmt_url: str | None = None


@dataclass(slots=True)
class _PartnerRuntimeConfig:
    """Partner 临时运行时配置。"""

    runtime_root: Path
    online_dir: Path
    agent_urls: dict[str, str]
    reserved_sockets: list[socket.socket]


def _cert_common_name(cert_file: Path) -> str:
    cert = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
    common_name = extract_common_name(cert)
    if common_name is None:
        raise RuntimeError(f"certificate CN missing: {cert_file}")
    return common_name


def _reserve_free_port() -> tuple[int, socket.socket]:
    """预留一个临时端口，减少并发测试抢占。"""
    reserved_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved_socket.bind((TEST_BIND_HOST, 0))
    reserved_socket.listen(1)
    port = reserved_socket.getsockname()[1]
    return port, reserved_socket


def _build_runtime_client_ssl_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """构建用于 Leader/Partner E2E 请求的 mTLS SSLContext。"""
    context = ssl.create_default_context(cafile=str(ca_bundle or LEADER_TRUST_BUNDLE))
    context.check_hostname = False
    context.load_cert_chain(certfile=str(LEADER_CERT_FILE), keyfile=str(LEADER_KEY_FILE))
    return context


def _resolve_partner_trust_bundle(online_dir: Path) -> Path:
    for entry in sorted(online_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        bundle_path = entry / "trust-bundle.pem"
        if bundle_path.is_file():
            return bundle_path
    raise RuntimeError(f"未在 {online_dir} 中找到 partner trust-bundle.pem")


def _rewrite_partner_server_port(config_path: Path, port: int) -> None:
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


def _discover_partner_urls(online_dir: Path) -> dict[str, str]:
    """根据临时 online 目录发现 Partner URL。"""
    agent_urls: dict[str, str] = {}
    for entry in sorted(online_dir.iterdir(), key=lambda item: item.name):
        config_path = entry / "config.toml"
        if not entry.is_dir() or not config_path.is_file():
            continue

        with config_path.open("rb") as file_obj:
            config = tomllib.load(file_obj)

        port = config.get("server", {}).get("port")
        if not port:
            continue

        tls_enabled = config.get("server", {}).get("mtls", {}).get("tls_enabled", False)
        protocol = "https" if tls_enabled else "http"
        agent_urls[entry.name] = f"{protocol}://{TEST_CONNECT_HOST}:{port}"

    return agent_urls


def _prepare_temp_partner_runtime() -> _PartnerRuntimeConfig:
    """复制 demo-partner online 目录并改写为临时端口。"""
    runtime_root = Path(tempfile.mkdtemp(prefix="demo-leader-e2e-partners-"))
    online_dir = runtime_root / "online"
    shutil.copytree(PARTNER_ONLINE_DIR, online_dir)
    _rewrite_partner_mq_client_assets(online_dir)

    reserved_sockets: list[socket.socket] = []
    for entry in sorted(online_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue

        config_path = entry / "config.toml"
        if not config_path.is_file():
            continue

        port, reserved_socket = _reserve_free_port()
        reserved_sockets.append(reserved_socket)
        _rewrite_partner_server_port(config_path, port)

    return _PartnerRuntimeConfig(
        runtime_root=runtime_root,
        online_dir=online_dir,
        agent_urls=_discover_partner_urls(online_dir),
        reserved_sockets=reserved_sockets,
    )


def _rewrite_partner_mq_client_assets(online_dir: Path) -> None:
    rabbitmq_bundle = RABBITMQ_CA_FILE.read_bytes()
    rabbitmq_client_cert = RABBITMQ_CLIENT_CERT_FILE.read_bytes()
    rabbitmq_client_key = RABBITMQ_CLIENT_KEY_FILE.read_bytes()

    for entry in sorted(online_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue

        bundle_path = entry / "trust-bundle.pem"
        if bundle_path.is_file():
            original_bundle = bundle_path.read_bytes()
            if rabbitmq_bundle not in original_bundle:
                bundle_path.write_bytes(original_bundle.rstrip() + b"\n" + rabbitmq_bundle)

        (entry / "rabbitmq-client.pem").write_bytes(rabbitmq_client_cert)
        (entry / "rabbitmq-client.key").write_bytes(rabbitmq_client_key)


def _create_rabbitmq_test_user(base_url: str, *, username: str, password: str) -> None:
    encoded_username = quote(username, safe="")
    encoded_vhost = quote("acps", safe="")
    auth = (RABBITMQ_ADMIN_USER, RABBITMQ_ADMIN_PASSWORD)
    with httpx.Client(timeout=10.0) as client:
        user_response = client.put(
            f"{base_url}/api/users/{encoded_username}",
            auth=auth,
            json={"password": password, "tags": ""},
        )
        user_response.raise_for_status()
        permission_response = client.put(
            f"{base_url}/api/permissions/{encoded_vhost}/{encoded_username}",
            auth=auth,
            json={"configure": ".*", "write": ".*", "read": ".*"},
        )
        permission_response.raise_for_status()


def _delete_rabbitmq_test_user(base_url: str, *, username: str) -> None:
    encoded_username = quote(username, safe="")
    with httpx.Client(timeout=10.0) as client:
        response = client.delete(
            f"{base_url}/api/users/{encoded_username}",
            auth=(RABBITMQ_ADMIN_USER, RABBITMQ_ADMIN_PASSWORD),
        )
    if response.status_code not in {204, 404}:
        response.raise_for_status()


def _rewrite_static_mapping(domain_path: Path) -> None:
    """把 tour 场景改写为纯静态映射，避免 E2E 依赖 discovery 固定端口。"""
    content = domain_path.read_text(encoding="utf-8")
    replacement_lines = ["[partners.static_mapping]"]
    for dimension_id, acs_files in STATIC_TOUR_MAPPING.items():
        replacement_lines.append(f"{dimension_id} = {json.dumps(acs_files, ensure_ascii=False)}")
    lines = content.splitlines()

    start_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[partners.static_mapping]"),
        -1,
    )
    end_index = next(
        (
            index
            for index, line in enumerate(lines[start_index + 1 :], start=start_index + 1)
            if line.strip() == "[consistency]"
        ),
        -1,
    )
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        raise RuntimeError(f"无法在 {domain_path} 中定位 [partners.static_mapping] 配置段")
    updated_lines = lines[:start_index] + replacement_lines + [""] + lines[end_index:]
    updated_content = "\n".join(updated_lines) + "\n"
    domain_path.write_text(updated_content, encoding="utf-8")


def _rewrite_partner_acs_endpoint(acs_path: Path, rpc_url: str) -> None:
    """改写场景内 ACS 文件的 JSONRPC 端点。"""
    with acs_path.open(encoding="utf-8") as file_obj:
        acs_data = json.load(file_obj)

    for endpoint in acs_data.get("endPoints", []):
        transport = str(endpoint.get("transport", "")).upper()
        url = str(endpoint.get("url", ""))
        if transport in {"HTTP", "JSONRPC"} and url.startswith(("http://", "https://")):
            endpoint["url"] = rpc_url

    acs_path.write_text(
        json.dumps(acs_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_temp_scenario_root(partner_urls: dict[str, str]) -> Path:
    """复制 Leader scenario 目录并把静态 ACS URL 改写到临时 Partner 端口。"""
    runtime_root = Path(tempfile.mkdtemp(prefix="demo-leader-e2e-scenario-"))
    scenario_root = runtime_root / "scenario"
    shutil.copytree(LEADER_ROOT / "scenario", scenario_root)

    tour_root = scenario_root / "expert" / "tour"
    _rewrite_static_mapping(tour_root / "domain.toml")

    for agent_name, acs_filename in ACS_FILE_BY_AGENT.items():
        rpc_url = f"{partner_urls[agent_name]}/rpc"
        _rewrite_partner_acs_endpoint(tour_root / acs_filename, rpc_url)

    return scenario_root


def _prepare_temp_oidc_scenario_root() -> Path:
    """为 OIDC 黑盒联调复制一份原始 scenario，避免依赖临时 Partner runtime。"""
    runtime_root = Path(tempfile.mkdtemp(prefix="demo-leader-oidc-e2e-scenario-"))
    scenario_root = runtime_root / "scenario"
    shutil.copytree(LEADER_ROOT / "scenario", scenario_root)
    return scenario_root


def _wait_for_partner_ready(
    agent_urls: dict[str, str],
    process: subprocess.Popen[str],
    log_path: Path,
    ssl_context: ssl.SSLContext,
) -> None:
    """等待所有临时 Partner 就绪。"""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

    with httpx.Client(timeout=1.0, verify=ssl_context) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))

            all_ready = True
            for url in agent_urls.values():
                try:
                    response = client.get(f"{url}/health")
                    if response.status_code != 200:
                        all_ready = False
                        break
                except httpx.HTTPError, OSError:
                    all_ready = False
                    break

            if all_ready:
                return

            time.sleep(1.0)

    raise RuntimeError(
        f"Partner 临时实例启动超时（{STARTUP_TIMEOUT_SECONDS}s）\n\n"
        + log_path.read_text(encoding="utf-8", errors="replace")
    )


def _wait_for_leader_ready(
    leader_base_url: str,
    process: subprocess.Popen[str],
    log_path: Path,
    ssl_context: ssl.SSLContext,
) -> None:
    """等待临时 Leader HTTPS 服务就绪。"""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

    with httpx.Client(timeout=2.0, verify=ssl_context) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))

            try:
                response = client.get(f"{leader_base_url}/api/v1/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass

            time.sleep(1.0)

    raise RuntimeError(
        f"Leader 临时实例启动超时（{STARTUP_TIMEOUT_SECONDS}s）\n\n"
        + log_path.read_text(encoding="utf-8", errors="replace")
    )


def _start_partner_process(
    partner_runtime: _PartnerRuntimeConfig,
    ssl_context: ssl.SSLContext,
) -> tuple[subprocess.Popen[str], Path]:
    """启动临时 Partner 进程。"""
    file_descriptor, log_path_str = tempfile.mkstemp(prefix="demo-leader-e2e-partners-", suffix=".log")
    os.close(file_descriptor)
    log_path = Path(log_path_str)

    env = os.environ.copy()
    _apply_test_rabbitmq_overrides(env)
    env["PARTNERS_ONLINE_DIR"] = str(partner_runtime.online_dir)

    with log_path.open("w+", encoding="utf-8") as log_file:
        process = subprocess.Popen(  # noqa: S603
            [str(PARTNER_PYTHON), "-m", "partners.main"],
            cwd=DEMO_PARTNER_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    for reserved_socket in partner_runtime.reserved_sockets:
        reserved_socket.close()
    partner_runtime.reserved_sockets.clear()

    _wait_for_partner_ready(partner_runtime.agent_urls, process, log_path, ssl_context)
    return process, log_path


def _start_leader_process(
    scenario_root: Path,
    ssl_context: ssl.SSLContext,
    *,
    tls_enabled: bool = True,
    oidc_enabled: bool = False,
) -> tuple[subprocess.Popen[str], Path, str]:
    """启动临时 Leader 进程。"""
    leader_port, reserved_socket = _reserve_free_port()
    reserved_socket.close()

    file_descriptor, log_path_str = tempfile.mkstemp(prefix="demo-leader-e2e-leader-", suffix=".log")
    os.close(file_descriptor)
    log_path = Path(log_path_str)

    env = os.environ.copy()
    _apply_test_rabbitmq_overrides(env)
    env["LEADER_SCENARIO_ROOT"] = str(scenario_root)
    env["DISCOVERY_SERVER_BASE_URL"] = ""
    env["LEADER_OIDC_ENABLED"] = "true" if oidc_enabled else "false"

    protocol = "https" if tls_enabled else "http"
    leader_base_url = f"{protocol}://{TEST_CONNECT_HOST}:{leader_port}"
    process_args = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        TEST_BIND_HOST,
        "--port",
        str(leader_port),
    ]
    if tls_enabled:
        process_args.extend(
            [
                "--ssl-certfile",
                str(LEADER_SERVER_CERT_FILE),
                "--ssl-keyfile",
                str(LEADER_SERVER_KEY_FILE),
            ]
        )

    with log_path.open("w+", encoding="utf-8") as log_file:
        process = subprocess.Popen(  # noqa: S603
            process_args,
            cwd=LEADER_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    _wait_for_leader_ready(leader_base_url, process, log_path, ssl_context)
    return process, log_path, leader_base_url


@pytest.fixture(scope="session")
def e2e_runtime() -> Generator[LeaderE2ERuntime]:
    """启动 demo-leader E2E 所需的临时 HTTPS runtime。"""
    partner_runtime = _prepare_temp_partner_runtime()
    ssl_context = _build_runtime_client_ssl_context(_resolve_partner_trust_bundle(partner_runtime.online_dir))
    partner_process: subprocess.Popen[str] | None = None
    partner_log_path: Path | None = None
    scenario_root: Path | None = None
    leader_process: subprocess.Popen[str] | None = None
    leader_log_path: Path | None = None
    rabbitmq_username = _cert_common_name(RABBITMQ_CLIENT_CERT_FILE)
    rabbitmq_password = f"shared-{uuid.uuid4().hex}"

    try:
        _create_rabbitmq_test_user(
            RABBITMQ_MGMT_URL,
            username=rabbitmq_username,
            password=rabbitmq_password,
        )

        partner_process, partner_log_path = _start_partner_process(partner_runtime, ssl_context)
        scenario_root = _prepare_temp_scenario_root(partner_runtime.agent_urls)
        leader_process, leader_log_path, leader_base_url = _start_leader_process(
            scenario_root,
            ssl_context,
            oidc_enabled=False,
        )

        yield LeaderE2ERuntime(
            leader_base_url=leader_base_url,
            client_ssl_context=ssl_context,
            partner_health_url=partner_runtime.agent_urls["beijing_food"],
            partner_urls=partner_runtime.agent_urls,
            rabbitmq_mgmt_url=RABBITMQ_MGMT_URL,
        )
    finally:
        if leader_process and leader_process.poll() is None:
            leader_process.terminate()
            try:
                leader_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                leader_process.kill()
                leader_process.wait(timeout=10)

        if partner_process and partner_process.poll() is None:
            partner_process.terminate()
            try:
                partner_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                partner_process.kill()
                partner_process.wait(timeout=10)

        for reserved_socket in partner_runtime.reserved_sockets:
            reserved_socket.close()

        preserve_logs = os.getenv("LEADER_E2E_PRESERVE_LOGS", "").lower() in {"1", "true", "yes"}
        if leader_log_path and preserve_logs:
            print(f"Preserved leader E2E log: {leader_log_path}")
        elif leader_log_path:
            leader_log_path.unlink(missing_ok=True)
        if partner_log_path and preserve_logs:
            print(f"Preserved partner E2E log: {partner_log_path}")
        elif partner_log_path:
            partner_log_path.unlink(missing_ok=True)
        shutil.rmtree(partner_runtime.runtime_root, ignore_errors=True)
        if scenario_root is not None:
            shutil.rmtree(scenario_root.parent, ignore_errors=True)
        _delete_rabbitmq_test_user(
            RABBITMQ_MGMT_URL,
            username=rabbitmq_username,
        )


@pytest.fixture(scope="session")
def oidc_e2e_runtime() -> Generator[LeaderE2ERuntime]:
    """为 OIDC 黑盒联调启动仅包含 Leader 的轻量 HTTPS runtime。"""
    ssl_context = _build_runtime_client_ssl_context()
    scenario_root = _prepare_temp_oidc_scenario_root()
    leader_process: subprocess.Popen[str] | None = None
    leader_log_path: Path | None = None

    try:
        leader_process, leader_log_path, leader_base_url = _start_leader_process(
            scenario_root,
            ssl_context,
            tls_enabled=False,
            oidc_enabled=True,
        )
        yield LeaderE2ERuntime(
            leader_base_url=leader_base_url,
            client_ssl_context=ssl_context,
        )
    finally:
        if leader_process and leader_process.poll() is None:
            leader_process.terminate()
            try:
                leader_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                leader_process.kill()
                leader_process.wait(timeout=10)

        if leader_log_path and os.getenv("LEADER_E2E_PRESERVE_LOGS", "").lower() in {"1", "true", "yes"}:
            print(f"Preserved leader OIDC E2E log: {leader_log_path}")
        elif leader_log_path:
            leader_log_path.unlink(missing_ok=True)
        shutil.rmtree(scenario_root.parent, ignore_errors=True)


@pytest.fixture(autouse=True)
def inject_e2e_runtime(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把临时 runtime URL 与默认 HTTPS Client 注入到每个 E2E 模块。"""
    module = request.module
    if module is None or not hasattr(module, "httpx"):
        return

    runtime_fixture_name = "oidc_e2e_runtime" if request.node.get_closest_marker("oidc") else "e2e_runtime"
    runtime: LeaderE2ERuntime = request.getfixturevalue(runtime_fixture_name)

    original_client = module.httpx.Client

    def runtime_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.setdefault("verify", runtime.client_ssl_context)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", runtime_client)
    monkeypatch.setattr(module, "BASE_URL", runtime.leader_base_url, raising=False)
    if runtime.partner_health_url is not None:
        monkeypatch.setattr(module, "PARTNER_URL", runtime.partner_health_url, raising=False)
    if runtime.rabbitmq_mgmt_url is not None:
        monkeypatch.setattr(module, "RABBITMQ_MGMT_URL", runtime.rabbitmq_mgmt_url, raising=False)
