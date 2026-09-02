"""集成测试 conftest — 默认本地地址缺服务时由夹具自动托管。

前置条件：
    如需手工联调，可启动以下服务：
    - registry-server → http://localhost:9001
    - ca-server       → http://localhost:9003
    - discovery-server → http://localhost:9005
    - monitor-server  → http://localhost:9009
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._local_services import (
    DEFAULT_MONITOR_URL,
    DEFAULT_MQ_AUTH_URL,
    DEFAULT_MQ_GROUP_URL,
    LocalServiceRuntime,
    ensure_local_services,
    get_managed_registry_mtls_endpoint,
    get_managed_registry_mtls_probe_materials,
    get_mq_dev_cert_dir,
    stop_local_services,
    wait_for_service,
)
from tests._registry_mtls import RegistryMtlsSettings, detect_registry_mtls_settings

# ─── 服务端口常量 ─────────────────────────────────────────────────────────────

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:9001")
REGISTRY_MTLS_URL = os.getenv("REGISTRY_MTLS_URL", REGISTRY_URL.replace(":9001", ":9002"))
REGISTRY_MTLS_CA_FILE = os.getenv("REGISTRY_MTLS_CA_FILE")
CA_URL = os.getenv("CA_URL", "http://localhost:9003")
DISCO_URL = os.getenv("DISCO_URL", "http://localhost:9005")
MQ_GROUP_URL = os.getenv("MQ_GROUP_API_URL", DEFAULT_MQ_GROUP_URL)
MQ_AUTH_URL = os.getenv("MQ_AUTH_API_URL", DEFAULT_MQ_AUTH_URL)
MONITOR_URL = os.getenv("MONITOR_URL", os.getenv("MONITOR_BASE_URL", DEFAULT_MONITOR_URL))
CONFIG_FILE_NAME = "acps-cli.toml"
TOKEN_DIR_NAME = ".acps-cli"  # nosec B105 - integration test fixture path, not a secret
TEXT_PLAIN_MODE = "text/plain"
DEFAULT_REGISTRY_ADMIN_USERNAME = "admin"
DEFAULT_REGISTRY_ADMIN_SECRET = "".join(["admin", "123"])
DEFAULT_CA_ADMIN_API_TOKEN = "local-ca-admin-token"  # nosec B105 - matches ca-server .env.example local default

# ─── 测试用户名前缀（每次测试生成唯一用户，避免冲突） ───────────────────────────

_TEST_PREFIX = "integ"


def _unique_username() -> str:
    """生成唯一测试用户名。"""
    return f"{_TEST_PREFIX}_{uuid.uuid4().hex[:8]}"


# ─── 服务可达性检查 ──────────────────────────────────────────────────────────


def _service_skip_message(service_name: str, base_url: str) -> str:
    """生成本地服务不可达时的统一跳过提示。"""
    return (
        f"{service_name} not reachable at {base_url} — "
        "start the local services documented in README.md "
        "or run just dev check first"
    )


def _resolve_ca_admin_api_token() -> str:
    """解析 CA admin token；优先环境变量，回退到仓内测试默认值。"""

    return os.getenv("CA_SERVER_ADMIN_API_TOKEN", "").strip() or DEFAULT_CA_ADMIN_API_TOKEN


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义 marker。"""
    config.addinivalue_line("markers", "integration: requires running dev-infra services")


# ─── 服务可达性 fixture（session 级，跳过整个 session 而非逐个用例） ─────────────


@pytest.fixture(scope="session")
def local_service_runtime() -> Iterator[LocalServiceRuntime]:
    """记录当前 pytest 进程托管的本地 sibling 服务。"""

    runtime = LocalServiceRuntime(ca_admin_api_token=_resolve_ca_admin_api_token())
    try:
        yield runtime
    finally:
        stop_local_services(runtime)


@pytest.fixture(scope="session")
def registry_url(local_service_runtime: LocalServiceRuntime) -> str:
    """返回 registry-server 基础 URL，不可达时按默认本地拓扑自动准备。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["registry"],
            base_urls={"registry": REGISTRY_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return REGISTRY_URL


@pytest.fixture(scope="session")
def ca_url(local_service_runtime: LocalServiceRuntime) -> str:
    """返回 ca-server 基础 URL，不可达时按默认本地拓扑自动准备。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["ca"],
            base_urls={"ca": CA_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return CA_URL


@pytest.fixture(scope="session")
def disco_url(local_service_runtime: LocalServiceRuntime) -> str:
    """返回 discovery-server 基础 URL，不可达时按默认本地拓扑自动准备。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["discovery"],
            base_urls={"discovery": DISCO_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return DISCO_URL


@pytest.fixture(scope="session")
def mq_cert_dir(local_service_runtime: LocalServiceRuntime) -> Path:
    """返回 mq-auth-server dev certs 目录；若服务尚未启动则先自动拉起。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["mq"],
            base_urls={"mq": MQ_GROUP_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return get_mq_dev_cert_dir(local_service_runtime)


@pytest.fixture(scope="session")
def monitor_url(local_service_runtime: LocalServiceRuntime) -> str:
    """返回 monitor-server 基础 URL，不可达时按默认本地拓扑自动准备。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["monitor"],
            base_urls={"monitor": MONITOR_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return MONITOR_URL


@pytest.fixture(scope="session")
def mq_config_file(tmp_path_factory: pytest.TempPathFactory, mq_cert_dir: Path) -> Path:
    """生成集成测试专用 acps-cli.toml（[mq] 段），包含 mTLS cert 路径。"""
    tmp = tmp_path_factory.mktemp("mq-config")
    config = tmp / "acps-cli.toml"
    cert_dir = mq_cert_dir
    config.write_text(
        "\n".join(
            [
                "[mq]",
                f'group_api_url = "{MQ_GROUP_URL}"',
                f'auth_api_url = "{MQ_AUTH_URL}"',
                f'group_cert_file = "{cert_dir / "client.pem"}"',
                f'group_key_file = "{cert_dir / "client.key"}"',
                f'probe_cert_file = "{cert_dir / "client.pem"}"',
                f'probe_key_file = "{cert_dir / "client.key"}"',
                f'ca_cert_file = "{cert_dir / "acps-root-ca.pem"}"',
            ]
        ),
        encoding="utf-8",
    )
    return config


@pytest.fixture(scope="session")
def registry_mtls_settings(
    local_service_runtime: LocalServiceRuntime,
) -> RegistryMtlsSettings:
    """返回 register-entity 场景所需的 mTLS 前置诊断结果。"""
    ensure_local_services(
        local_service_runtime,
        required_services=["registry"],
        base_urls={"registry": REGISTRY_URL},
    )
    managed_endpoint = get_managed_registry_mtls_endpoint(local_service_runtime)
    if (
        managed_endpoint is not None
        and "REGISTRY_MTLS_URL" not in os.environ
        and "REGISTRY_MTLS_CA_FILE" not in os.environ
    ):
        managed_url, managed_ca_file = managed_endpoint
        managed_probe = get_managed_registry_mtls_probe_materials(local_service_runtime)
        if managed_probe is not None and not wait_for_service(
            f"{managed_url}/health",
            verify=False,
            cert=(str(managed_probe[0]), str(managed_probe[1])),
        ):
            return RegistryMtlsSettings(
                base_url=managed_url,
                ca_file=managed_ca_file,
                skip_reason=(
                    "registry-server 9002 listener 已启动，但真实 mTLS /health 探测失败；"
                    "当前环境下跳过 register-entity 集成测试。"
                ),
            )
        return RegistryMtlsSettings(base_url=managed_url, ca_file=managed_ca_file)
    return detect_registry_mtls_settings(
        registry_mtls_url=REGISTRY_MTLS_URL,
        registry_mtls_ca_file=REGISTRY_MTLS_CA_FILE,
        wait_for_service=wait_for_service,
    )


# ─── 测试工作目录 fixture ────────────────────────────────────────────────────


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """创建带有标准目录结构的临时工作目录。"""
    (tmp_path / "private").mkdir()
    (tmp_path / "certs").mkdir()
    (tmp_path / "csr").mkdir()
    return tmp_path


# ─── registry-client 配置文件 fixture ────────────────────────────────────────


@pytest.fixture()
def reg_conf(
    work_dir: Path,
    registry_url: str,
    registry_mtls_settings: RegistryMtlsSettings,
) -> Path:
    """在工作目录写入 acps-cli.toml（registry section），返回配置文件路径。"""
    conf = work_dir / CONFIG_FILE_NAME
    user_token_path = work_dir / TOKEN_DIR_NAME / "tokens" / "registry-user.json"
    admin_token_path = work_dir / TOKEN_DIR_NAME / "tokens" / "registry-admin.json"
    content = f'[registry]\nbase_url = "{registry_url}"\nmtls_base_url = "{registry_mtls_settings.base_url}"\n'
    if registry_mtls_settings.ca_file is not None:
        content += f'mtls_server_ca_file = "{registry_mtls_settings.ca_file}"\n'
    content += f'\n[auth]\nuser_token_file = "{user_token_path}"\nadmin_token_file = "{admin_token_path}"\n'
    conf.write_text(content, encoding="utf-8")
    return conf


@pytest.fixture()
def disco_conf(reg_conf: Path, disco_url: str) -> Path:
    """在工作目录写入 acps-cli.toml（discovery section），返回配置文件路径。"""
    conf = reg_conf
    existing_content = conf.read_text(encoding="utf-8") if conf.exists() else ""
    with conf.open("a", encoding="utf-8") as file:
        if existing_content and not existing_content.endswith("\n"):
            file.write("\n")
        file.write(f'[discovery]\nbase_url = "{disco_url}"\n')
    return conf


@pytest.fixture()
def monitor_conf(work_dir: Path, monitor_url: str) -> Path:
    """在工作目录写入 acps-cli.toml（monitor section），返回配置文件路径。"""
    conf = work_dir / CONFIG_FILE_NAME
    conf.write_text(
        "\n".join(
            [
                "[monitor]",
                f'base_url = "{monitor_url}"',
                'api_prefix = "/acps-amp-v1"',
                "timeout_seconds = 15",
            ]
        ),
        encoding="utf-8",
    )
    return conf


# ─── cert 命令配置文件 fixture ────────────────────────────────────────────────


@pytest.fixture()
def ca_conf(reg_conf: Path, ca_url: str) -> Path:
    """在工作目录写入 acps-cli.toml（ca section），返回配置文件路径。"""
    work_dir = reg_conf.parent
    keyfiles_dir = work_dir / "keyfiles"
    accounts_dir = keyfiles_dir / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    conf = reg_conf
    admin_api_token = _resolve_ca_admin_api_token()
    existing_content = conf.read_text(encoding="utf-8") if conf.exists() else ""
    with conf.open("a", encoding="utf-8") as file:
        if existing_content and not existing_content.endswith("\n"):
            file.write("\n")
        file.write(
            "[ca]\n"
            f'base_url = "{ca_url}"\n'
            f'admin_api_token = "{admin_api_token}"\n'
            f'account_keys_dir = "{accounts_dir}"\n'
            f'private_keys_dir = "{keyfiles_dir}/private"\n'
            f'certs_dir = "{keyfiles_dir}/certs"\n'
            f'csr_dir = "{keyfiles_dir}/csr"\n'
            f'trust_bundle_path = "{keyfiles_dir}/trust-bundle.pem"\n'
        )
    return conf


# ─── 测试用 ACS JSON fixture ──────────────────────────────────────────────────


@pytest.fixture()
def acs_file(work_dir: Path) -> tuple[Path, str, str]:
    """生成测试用 ACS JSON 文件，返回 (路径, name, version)。"""
    name = f"test-agent-{uuid.uuid4().hex[:6]}"
    version = "1.0.0"
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    acs = {
        "aic": "",
        "active": False,
        "lastModifiedTime": now,
        "protocolVersion": "02.02",
        "name": name,
        "version": version,
        "description": "集成测试用 Agent",
        "provider": {
            "organization": "Test Org",
            "url": "https://test.example.org",
            "license": "TEST-LICENSE",
        },
        "securitySchemes": {
            "mtls": {
                "type": "mutualTLS",
                "description": "Agent 间 mTLS 双向认证",
            }
        },
        "endPoints": [
            {
                "url": "https://localhost:9000/rpc",
                "transport": "JSONRPC",
                "security": [{"mtls": []}],
            }
        ],
        "capabilities": {"streaming": False, "notification": False, "messageQueue": []},
        "defaultInputModes": [TEXT_PLAIN_MODE],
        "defaultOutputModes": [TEXT_PLAIN_MODE],
        "skills": [
            {
                "id": f"{name}.skill",
                "name": "Test Skill",
                "description": "集成测试用技能",
                "version": "1.0.0",
                "tags": ["test"],
                "examples": ["test query"],
                "inputModes": [TEXT_PLAIN_MODE],
                "outputModes": [TEXT_PLAIN_MODE],
            }
        ],
    }
    path = work_dir / "acs.json"
    path.write_text(json.dumps(acs, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, name, version


# ─── 已登录的用户 fixture ────────────────────────────────────────────────────


@pytest.fixture()
def user_credentials(registry_url: str) -> tuple[str, str]:
    """返回用于集成测试的测试用户凭据 (username, password)。

    用户名每次唯一以避免不同测试间状态干扰。
    """
    return _unique_username(), "Test@12345"


# ─── Token 文件路径 fixture ───────────────────────────────────────────────────


@pytest.fixture()
def token_file(work_dir: Path) -> Path:
    """返回 token 文件路径（不预先创建）。"""
    return work_dir / TOKEN_DIR_NAME / "tokens" / "registry-user.json"


# ─── Admin 凭据 fixture ───────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def admin_credentials() -> tuple[str, str]:
    """返回管理员凭据，从环境变量读取或使用 dev-infra 默认值。"""
    username = os.getenv("REGISTRY_ADMIN_USERNAME", DEFAULT_REGISTRY_ADMIN_USERNAME)
    admin_secret = os.getenv("REGISTRY_ADMIN_PASSWORD", DEFAULT_REGISTRY_ADMIN_SECRET)
    return username, admin_secret


@pytest.fixture()
def admin_token_file(work_dir: Path) -> Path:
    """返回管理员 token 文件路径。"""
    return work_dir / TOKEN_DIR_NAME / "tokens" / "registry-admin.json"


# ─── 等待服务就绪的工具函数 ────────────────────────────────────────────────────


def wait_until(condition_fn, timeout: int = 10, interval: float = 0.5) -> bool:
    """轮询 condition_fn 直到返回 True 或超时，返回最终结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False
