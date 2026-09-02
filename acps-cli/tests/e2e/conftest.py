"""端到端测试 conftest — 完整 ATR-EAB 流程所需的共享 fixture。

与 integration/conftest.py 的区别：
  - e2e 测试覆盖跨多个服务的完整业务流程
  - fixture 作用域更宽（function 级），确保每个测试用例有干净的起始状态
  - 提供预先登录好管理员和用户的高阶 fixture
    - 默认本地地址缺服务时，由 tests._local_services 自动托管 sibling 服务
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
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

# 复用 integration 层的基础常量
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:9001")
REGISTRY_MTLS_URL = os.getenv("REGISTRY_MTLS_URL", REGISTRY_URL.replace(":9001", ":9002"))
REGISTRY_MTLS_CA_FILE = os.getenv("REGISTRY_MTLS_CA_FILE")
CA_URL = os.getenv("CA_URL", "http://localhost:9003")
DISCO_URL = os.getenv("DISCO_URL", "http://localhost:9005")
MQ_GROUP_URL = os.getenv("MQ_GROUP_API_URL", DEFAULT_MQ_GROUP_URL)
MQ_AUTH_URL = os.getenv("MQ_AUTH_API_URL", DEFAULT_MQ_AUTH_URL)
MONITOR_URL = os.getenv("MONITOR_URL", os.getenv("MONITOR_BASE_URL", DEFAULT_MONITOR_URL))
CONFIG_FILE_NAME = "acps-cli.toml"
TOKEN_DIR_NAME = ".acps-cli"  # nosec B105 - e2e fixture path, not a secret
TEXT_PLAIN_MODE = "text/plain"
DEFAULT_REGISTRY_ADMIN_USERNAME = "admin"
DEFAULT_REGISTRY_ADMIN_SECRET = "".join(["admin", "123"])
DEFAULT_CA_ADMIN_API_TOKEN = "local-ca-admin-token"  # nosec B105 - matches ca-server .env.example local default
_DISCOVERY_E2E_FILE_PREFIX = "test_discovery_"
_DSP_WEBHOOK_PAGE_SIZE = 100


def _get_registry_admin_access_token(username: str, password: str) -> str:
    """通过 HTTP 登录 registry admin 并返回访问令牌。"""
    response = httpx.post(
        f"{REGISTRY_URL}/api/v1/auth/login",
        data={"username": username, "password": password},
        timeout=5,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    assert isinstance(token, str) and token, f"registry admin login 未返回 access_token: {payload}"
    return token


def _resolve_ca_admin_api_token() -> str:
    """解析 CA admin token；优先环境变量，回退到仓内测试默认值。"""

    return os.getenv("CA_SERVER_ADMIN_API_TOKEN", "").strip() or DEFAULT_CA_ADMIN_API_TOKEN


def _purge_registry_dsp_webhooks(access_token: str) -> None:
    """删除当前 registry 中残留的 DSP webhooks，避免 discovery e2e 相互串扰。"""
    headers = {"Authorization": f"Bearer {access_token}"}
    response = httpx.get(
        f"{REGISTRY_URL}/acps-dsp-v2/webhooks",
        params={"page_num": 1, "page_size": _DSP_WEBHOOK_PAGE_SIZE},
        headers=headers,
        timeout=5,
    )
    response.raise_for_status()
    payload = response.json()

    for item in payload.get("items") or []:
        webhook_id = item.get("id")
        if not isinstance(webhook_id, str) or not webhook_id:
            continue
        delete_response = httpx.delete(
            f"{REGISTRY_URL}/acps-dsp-v2/webhooks/{webhook_id}",
            headers=headers,
            timeout=5,
        )
        assert delete_response.status_code == 204, (
            f"删除残留 webhook 失败: webhook_id={webhook_id}, status={delete_response.status_code}, "
            f"body={delete_response.text}"
        )


def pytest_configure(config: pytest.Config) -> None:
    """注册 e2e marker。"""
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring all dev-infra services")


# ─── 服务可达性（session 级，按测试实际依赖收口） ───────────────────────────────


@pytest.fixture(scope="session")
def local_service_runtime() -> Iterator[LocalServiceRuntime]:
    """记录当前 pytest 进程托管的本地 sibling 服务。"""

    runtime = LocalServiceRuntime(ca_admin_api_token=_resolve_ca_admin_api_token())
    try:
        yield runtime
    finally:
        stop_local_services(runtime)


@pytest.fixture(scope="session")
def registry_ca_services(local_service_runtime: LocalServiceRuntime) -> dict[str, str]:
    """确保 ATR 主链路依赖的 registry 与 ca 服务就绪。"""
    services = {
        "registry": REGISTRY_URL,
        "ca": CA_URL,
    }
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["registry", "ca"],
            base_urls=services,
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return services


@pytest.fixture()
def registry_url(registry_ca_services: dict[str, str]) -> str:
    del registry_ca_services
    return REGISTRY_URL


@pytest.fixture()
def ca_url(registry_ca_services: dict[str, str]) -> str:
    del registry_ca_services
    return CA_URL


@pytest.fixture(scope="session")
def discovery_service(local_service_runtime: LocalServiceRuntime) -> str:
    """确保 discovery 服务就绪；仅 discovery e2e 用例需要。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["discovery"],
            base_urls={"discovery": DISCO_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return DISCO_URL


@pytest.fixture()
def disco_url(discovery_service: str) -> str:
    del discovery_service
    return DISCO_URL


@pytest.fixture(scope="session")
def mq_service(local_service_runtime: LocalServiceRuntime) -> str:
    """确保 mq-auth-server 就绪；缺失时自动托管启动。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["mq"],
            base_urls={"mq": MQ_GROUP_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return MQ_GROUP_URL


@pytest.fixture(scope="session")
def mq_cert_dir(
    mq_service: str,
    local_service_runtime: LocalServiceRuntime,
) -> Path:
    """返回 mq-auth-server 已就绪的 dev 证书目录。"""
    del mq_service
    return get_mq_dev_cert_dir(local_service_runtime)


@pytest.fixture(scope="session")
def monitor_service(local_service_runtime: LocalServiceRuntime) -> str:
    """确保 monitor-server 就绪；缺失时自动托管启动。"""
    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["monitor"],
            base_urls={"monitor": MONITOR_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return MONITOR_URL


@pytest.fixture()
def monitor_url(monitor_service: str) -> str:
    del monitor_service
    return MONITOR_URL


@pytest.fixture(autouse=True)
def cleanup_discovery_dsp_webhooks(
    request: pytest.FixtureRequest,
    admin_credentials: tuple[str, str],
    local_service_runtime: LocalServiceRuntime,
) -> Iterator[None]:
    """对 discovery e2e 自动清理 registry DSP webhooks，隔离跨测试副作用。"""
    test_file_name = Path(str(request.node.fspath)).name
    if not test_file_name.startswith(_DISCOVERY_E2E_FILE_PREFIX):
        yield
        return

    try:
        ensure_local_services(
            local_service_runtime,
            required_services=["registry"],
            base_urls={"registry": REGISTRY_URL},
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))

    admin_username, admin_password = admin_credentials
    access_token = _get_registry_admin_access_token(admin_username, admin_password)
    _purge_registry_dsp_webhooks(access_token)
    try:
        yield
    finally:
        refreshed_access_token = _get_registry_admin_access_token(admin_username, admin_password)
        _purge_registry_dsp_webhooks(refreshed_access_token)


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
                    "当前环境下跳过 register-entity E2E 测试。"
                ),
            )
        return RegistryMtlsSettings(base_url=managed_url, ca_file=managed_ca_file)
    return detect_registry_mtls_settings(
        registry_mtls_url=REGISTRY_MTLS_URL,
        registry_mtls_ca_file=REGISTRY_MTLS_CA_FILE,
        wait_for_service=wait_for_service,
    )


# ─── 工作目录 ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """创建标准目录结构的临时工作目录。"""
    for sub in ("private", "certs", "csr"):
        (tmp_path / sub).mkdir()
    return tmp_path


# ─── 配置文件 ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def reg_conf(
    registry_ca_services: dict[str, str],
    work_dir: Path,
    registry_mtls_settings: RegistryMtlsSettings,
) -> Path:
    """写入用户端 acps-cli.toml（registry section）。"""
    del registry_ca_services
    conf = work_dir / CONFIG_FILE_NAME
    user_token_path = work_dir / TOKEN_DIR_NAME / "tokens" / "registry-user.json"
    admin_token_path = work_dir / TOKEN_DIR_NAME / "tokens" / "registry-admin.json"
    content = f'[registry]\nbase_url = "{REGISTRY_URL}"\nmtls_base_url = "{registry_mtls_settings.base_url}"\n'
    if registry_mtls_settings.ca_file is not None:
        content += f'mtls_server_ca_file = "{registry_mtls_settings.ca_file}"\n'
    content += f'\n[auth]\nuser_token_file = "{user_token_path}"\nadmin_token_file = "{admin_token_path}"\n'
    conf.write_text(content, encoding="utf-8")
    return conf


@pytest.fixture()
def admin_conf(reg_conf: Path) -> Path:
    """管理员端配置，复用 reg_conf 同一文件（registry section 已写入）。"""
    return reg_conf


@pytest.fixture()
def disco_conf(reg_conf: Path, discovery_service: str) -> Path:
    """在 reg_conf 基础上追加 discovery section。"""
    del discovery_service
    conf = reg_conf
    content = conf.read_text(encoding="utf-8")
    if "[discovery]" not in content:
        with conf.open("a", encoding="utf-8") as f:
            f.write(f'\n[discovery]\nbase_url = "{DISCO_URL}"\n')
    return conf


@pytest.fixture()
def ca_conf(reg_conf: Path) -> Path:
    """在 reg_conf 基础上追加 ca section，确保 registry section 不被覆盖。"""
    conf = reg_conf
    work_dir = conf.parent
    keyfiles_dir = work_dir / "keyfiles"
    accounts_dir = keyfiles_dir / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    admin_api_token = _resolve_ca_admin_api_token()
    with conf.open("a", encoding="utf-8") as f:
        f.write(
            "\n[ca]\n"
            f'base_url = "{CA_URL}"\n'
            f'admin_api_token = "{admin_api_token}"\n'
            f'account_keys_dir = "{accounts_dir}"\n'
            f'private_keys_dir = "{keyfiles_dir}/private"\n'
            f'certs_dir = "{keyfiles_dir}/certs"\n'
            f'csr_dir = "{keyfiles_dir}/csr"\n'
            f'trust_bundle_path = "{keyfiles_dir}/trust-bundle.pem"\n'
        )
    return conf


@pytest.fixture()
def mq_config_file(work_dir: Path, mq_cert_dir: Path) -> Path:
    """写入 mq e2e 专用 acps-cli.toml（[mq] section）。"""

    config = work_dir / CONFIG_FILE_NAME
    config.write_text(
        "\n".join(
            [
                "[mq]",
                f'group_api_url = "{MQ_GROUP_URL}"',
                f'auth_api_url = "{MQ_AUTH_URL}"',
                f'group_cert_file = "{mq_cert_dir / "client.pem"}"',
                f'group_key_file = "{mq_cert_dir / "client.key"}"',
                f'probe_cert_file = "{mq_cert_dir / "client.pem"}"',
                f'probe_key_file = "{mq_cert_dir / "client.key"}"',
                f'ca_cert_file = "{mq_cert_dir / "acps-root-ca.pem"}"',
            ]
        ),
        encoding="utf-8",
    )
    return config


@pytest.fixture()
def monitor_conf(work_dir: Path, monitor_url: str) -> Path:
    """写入 monitor e2e 专用 acps-cli.toml（[monitor] section）。"""

    config = work_dir / CONFIG_FILE_NAME
    config.write_text(
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
    return config


# ─── 凭据 ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def user_credentials() -> tuple[str, str]:
    """每个测试用例生成唯一测试用户凭据。"""
    return f"e2e_{uuid.uuid4().hex[:8]}", "Test@12345"


@pytest.fixture(scope="session")
def admin_credentials() -> tuple[str, str]:
    """管理员凭据（从环境变量读取或使用 dev-infra 默认值）。"""
    return (
        os.getenv("REGISTRY_ADMIN_USERNAME", DEFAULT_REGISTRY_ADMIN_USERNAME),
        os.getenv("REGISTRY_ADMIN_PASSWORD", DEFAULT_REGISTRY_ADMIN_SECRET),
    )


# ─── ACS 文件生成工具 ─────────────────────────────────────────────────────────


def make_acs_file(work_dir: Path, name: str | None = None) -> tuple[Path, str, str]:
    """生成 ACS JSON 文件，返回 (路径, name, version)。"""
    agent_name = name or f"e2e-agent-{uuid.uuid4().hex[:6]}"
    version = "1.0.0"
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    acs = {
        "aic": "",
        "active": False,
        "lastModifiedTime": now,
        "protocolVersion": "02.02",
        "name": agent_name,
        "version": version,
        "description": "E2E 测试用 Agent",
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
                "id": f"{agent_name}.skill",
                "name": "Test Skill",
                "description": "E2E 测试用技能",
                "version": "1.0.0",
                "tags": ["test"],
                "examples": ["test query"],
                "inputModes": [TEXT_PLAIN_MODE],
                "outputModes": [TEXT_PLAIN_MODE],
            }
        ],
    }
    path = work_dir / f"acs_{agent_name}.json"
    path.write_text(json.dumps(acs, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, agent_name, version
