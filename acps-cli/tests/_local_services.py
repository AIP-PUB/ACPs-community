"""本地联调服务管理辅助。"""

from __future__ import annotations

import os
import shutil
import signal
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

CLI_REPO = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CLI_REPO.parent

DEFAULT_REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:9001")
DEFAULT_REGISTRY_MTLS_URL = os.getenv("REGISTRY_MTLS_URL", "https://127.0.0.1:9002")
DEFAULT_CA_URL = os.getenv("CA_URL", "http://localhost:9003")
DEFAULT_DISCO_URL = os.getenv("DISCO_URL", "http://localhost:9005")
DEFAULT_MONITOR_URL = os.getenv("MONITOR_URL", os.getenv("MONITOR_BASE_URL", "http://localhost:9009"))
DEFAULT_MQ_GROUP_URL = os.getenv("MQ_GROUP_API_URL", "https://localhost:9007")
DEFAULT_MQ_AUTH_URL = os.getenv("MQ_AUTH_API_URL", "https://localhost:9008")
MQ_AUTH_REPO = WORKSPACE_ROOT / "mq-auth-server"
MQ_AUTH_CERTS_DIR = MQ_AUTH_REPO / "certs"
MONITOR_REPO = WORKSPACE_ROOT / "monitor-server"
MANAGED_DISCOVERY_DATABASE_URL_ENV = "ACPS_CLI_MANAGED_DISCOVERY_DATABASE_URL"
DEFAULT_CA_INTERNAL_API_TOKEN = "test-ca-internal-token"
DEFAULT_REGISTRY_TEST_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_REGISTRY_DATABASE_URL",
    "postgresql+psycopg://registry:registry@localhost:5432/agent_registry_test",
)
DEFAULT_CA_TEST_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_CA_DATABASE_URL",
    "postgresql://ca:ca@localhost:5432/agent_ca_test",
)
DEFAULT_DISCOVERY_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_DISCOVERY_DATABASE_URL",
    "postgresql+asyncpg://discovery:discovery@localhost:5432/agent_discovery",
)
DEFAULT_DISCOVERY_TEST_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_DISCOVERY_TEST_DATABASE_URL",
    "postgresql+asyncpg://discovery:discovery@localhost:5432/agent_discovery_test",
)
DEFAULT_MONITOR_TEST_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_MONITOR_DATABASE_URL",
    "postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test",
)
DEFAULT_MONITOR_TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/3")
PUBLIC_DOCKER_CONFIG_DIR = CLI_REPO / ".tmp" / "docker-public-config"
DEFAULT_REGISTRY_ADMIN_USERNAME = "admin"
DEFAULT_REGISTRY_ADMIN_PASSWORD = "admin123"
DEFAULT_REGISTRY_STAFF_USERNAME = "staff"
DEFAULT_REGISTRY_STAFF_PASSWORD = "staff123"


@dataclass(slots=True)
class LocalServiceRuntime:
    """记录当前 pytest 进程中受管启动的 sibling 服务。"""

    ca_admin_api_token: str
    started_services: list[str] = field(default_factory=list)
    managed_processes: dict[str, list[subprocess.Popen[str]]] = field(default_factory=dict)
    temp_dirs: list[Path] = field(default_factory=list)
    registry_mtls_url: str | None = None
    registry_mtls_ca_file: Path | None = None
    registry_mtls_cert_file: Path | None = None
    registry_mtls_key_file: Path | None = None
    registry_mtls_probe_cert_file: Path | None = None
    registry_mtls_probe_key_file: Path | None = None
    prepared_infra_services: set[str] = field(default_factory=set)
    mq_cert_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class LocalServiceSpec:
    """描述一个可被测试夹具受管启动的本地服务。"""

    name: str
    repo_path: Path
    default_base_url: str
    health_urls: tuple[str, ...]
    startup_timeout_seconds: int
    prepare_commands: tuple[tuple[str, ...], ...]
    launch_commands: tuple[tuple[str, ...], ...]
    env: dict[str, str]
    # mTLS 健康检查（可选）
    health_cert: tuple[str, str] | None = None  # (cert_file, key_file)
    health_ca: str | None = None  # CA 证书文件（用于验证服务器证书）


def _load_dotenv_values(repo_path: Path) -> dict[str, str]:
    env_path = repo_path / ".env"
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _ensure_public_docker_config_dir() -> Path:
    """创建最小 Docker config，避免 public 镜像拉取卡在本机凭据助手。"""

    PUBLIC_DOCKER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_DOCKER_CONFIG_DIR / "config.json").write_text("{}\n", encoding="utf-8")
    source_cli_plugins = Path.home() / ".docker" / "cli-plugins"
    target_cli_plugins = PUBLIC_DOCKER_CONFIG_DIR / "cli-plugins"
    if source_cli_plugins.is_dir():
        if target_cli_plugins.is_symlink():
            if target_cli_plugins.resolve() != source_cli_plugins.resolve():
                target_cli_plugins.unlink()
        elif target_cli_plugins.exists():
            shutil.rmtree(target_cli_plugins)
        if not target_cli_plugins.exists():
            target_cli_plugins.symlink_to(source_cli_plugins, target_is_directory=True)
    return PUBLIC_DOCKER_CONFIG_DIR


def wait_for_service(
    url: str,
    timeout: int = 5,
    verify: str | bool = True,
    cert: tuple[str, str] | None = None,
    *,
    accept_remote_protocol_error: bool = False,
) -> bool:
    """检查 HTTP 服务是否可达；支持 mTLS 客户端证书。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 若期望客户端证书但文件尚未生成（服务启动中），本轮等待
        if cert is not None:
            cert_file, key_file = cert
            if not Path(cert_file).exists() or not Path(key_file).exists():
                time.sleep(1)
                continue
        try:
            # 构建 SSL context：支持 mTLS 客户端证书 + 自定义 CA
            if cert is not None:
                cert_file, key_file = cert
                ssl_ctx = ssl.create_default_context()
                if isinstance(verify, str) and Path(verify).exists():
                    ssl_ctx = ssl.create_default_context(cafile=verify)
                elif not verify:
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                ssl_ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
                with httpx.Client(verify=ssl_ctx) as client:
                    response = client.get(url, timeout=5)
            else:
                effective_verify: str | bool = verify
                if isinstance(verify, str) and not Path(verify).exists():
                    effective_verify = False
                with httpx.Client(verify=effective_verify) as client:
                    response = client.get(url, timeout=5)
            if response.status_code < 500:
                return True
        except (httpx.RemoteProtocolError, httpx.ReadError):
            if accept_remote_protocol_error:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(1)
    return False


def _ensure_registry_mtls_artifacts(runtime: LocalServiceRuntime) -> None:
    """复用 registry-server 开发 PKI 中已经验证可用的 mTLS 材料。"""

    if (
        runtime.registry_mtls_url is not None
        and runtime.registry_mtls_ca_file is not None
        and runtime.registry_mtls_cert_file is not None
        and runtime.registry_mtls_key_file is not None
        and runtime.registry_mtls_probe_cert_file is not None
        and runtime.registry_mtls_probe_key_file is not None
    ):
        return

    registry_cert_dir = WORKSPACE_ROOT / "registry-server" / "certs"
    server_cert_path = registry_cert_dir / "server.pem"
    server_key_path = registry_cert_dir / "server.key"
    trust_bundle_path = registry_cert_dir / "trust-bundle.pem"
    probe_cert_path = registry_cert_dir / "client.pem"
    probe_key_path = registry_cert_dir / "client.key"
    required_paths = (
        server_cert_path,
        server_key_path,
        trust_bundle_path,
        probe_cert_path,
        probe_key_path,
    )
    if not all(path.is_file() for path in required_paths):
        raise RuntimeError(
            "缺少 registry 真实 mTLS listener 所需的开发 PKI 材料：" + " / ".join(str(path) for path in required_paths)
        )

    runtime.registry_mtls_url = DEFAULT_REGISTRY_MTLS_URL
    runtime.registry_mtls_ca_file = trust_bundle_path
    runtime.registry_mtls_cert_file = server_cert_path
    runtime.registry_mtls_key_file = server_key_path
    runtime.registry_mtls_probe_cert_file = probe_cert_path
    runtime.registry_mtls_probe_key_file = probe_key_path


def _service_specs(runtime: LocalServiceRuntime) -> dict[str, LocalServiceSpec]:
    """构造当前测试会话可受管的 sibling 服务规格。"""

    _ensure_registry_mtls_artifacts(runtime)

    discovery_repo_path = WORKSPACE_ROOT / "discovery-server"
    discovery_env_values = _load_dotenv_values(discovery_repo_path)
    discovery_database_url = str(
        os.getenv(MANAGED_DISCOVERY_DATABASE_URL_ENV)
        or discovery_env_values.get("DATABASE_URL")
        or DEFAULT_DISCOVERY_DATABASE_URL
    )
    discovery_test_database_url = str(
        os.getenv("ACPS_CLI_MANAGED_DISCOVERY_TEST_DATABASE_URL")
        or discovery_env_values.get("TEST_DATABASE_URL")
        or DEFAULT_DISCOVERY_TEST_DATABASE_URL
    )

    registry_admin_username = os.getenv("REGISTRY_ADMIN_USERNAME", DEFAULT_REGISTRY_ADMIN_USERNAME)
    registry_admin_password = os.getenv("REGISTRY_ADMIN_PASSWORD", DEFAULT_REGISTRY_ADMIN_PASSWORD)
    registry_staff_username = os.getenv("REGISTRY_STAFF_USERNAME", DEFAULT_REGISTRY_STAFF_USERNAME)
    registry_staff_password = os.getenv("REGISTRY_STAFF_PASSWORD", DEFAULT_REGISTRY_STAFF_PASSWORD)
    registry_seed_script = "\n".join(
        (
            "import asyncio",
            "from sqlalchemy import select, text",
            "from app.account.model import RoleType, User",
            "from app.core.db_session import AsyncSessionLocal, async_engine",
            "from tests.support.database import TRUNCATE_TABLE_NAMES, create_user, ensure_role",
            "",
            "async def reset_registry_state() -> None:",
            "    await async_engine.dispose()",
            "    async with async_engine.begin() as connection:",
            "        rows = await connection.execute(",
            "            text("
            "\"SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'snapshot_%' AND tablename <> 'snapshot'\""
            ")",
            "        )",
            "        for table_name in rows.scalars().all():",
            "            await connection.execute(text(f'DROP TABLE IF EXISTS \"{table_name}\" CASCADE'))",
            "        rows = await connection.execute("
            "text(\"SELECT tablename FROM pg_tables WHERE schemaname = 'public'\")"
            ")",
            "        existing_tables = set(rows.scalars().all())",
            "        truncatable_tables = [",
            "            table_name",
            "            for table_name in TRUNCATE_TABLE_NAMES",
            "            if table_name != 'alembic_version' and table_name in existing_tables",
            "        ]",
            "        if truncatable_tables:",
            "            joined_table_names = ', '.join(f'\"{table_name}\"' for table_name in truncatable_tables)",
            "            await connection.execute("
            "text(f'TRUNCATE TABLE {joined_table_names} RESTART IDENTITY CASCADE')"
            ")",
            "        if 'global_seq' in existing_tables:",
            "            await connection.execute(text('ALTER SEQUENCE global_seq RESTART WITH 1'))",
            "    await async_engine.dispose()",
            "",
            "async def main() -> None:",
            "    await reset_registry_state()",
            "    async with AsyncSessionLocal() as session:",
            "        await ensure_role(session, RoleType.ADMIN)",
            "        await ensure_role(session, RoleType.STAFF)",
            "        await ensure_role(session, RoleType.CLIENT)",
            (
                "        result = await session.execute("
                f"select(User).where(User.username == {registry_admin_username!r}).limit(1)"
                ")"
            ),
            "        admin_user = result.scalar_one_or_none()",
            "        if admin_user is None:",
            "            await create_user(",
            "                session,",
            f"                username={registry_admin_username!r},",
            f"                password={registry_admin_password!r},",
            "                roles=(RoleType.ADMIN, RoleType.STAFF),",
            "                name='Bootstrap Admin',",
            "            )",
            (
                "        result = await session.execute("
                f"select(User).where(User.username == {registry_staff_username!r}).limit(1)"
                ")"
            ),
            "        staff_user = result.scalar_one_or_none()",
            "        if staff_user is None:",
            "            await create_user(",
            "                session,",
            f"                username={registry_staff_username!r},",
            f"                password={registry_staff_password!r},",
            "                roles=(RoleType.STAFF,),",
            "                name='Bootstrap Staff',",
            "            )",
            "        await session.commit()",
            "",
            "asyncio.run(main())",
        )
    )
    discovery_reset_script = "\n".join(
        (
            "import os",
            "from sqlalchemy import create_engine, text",
            "database_url = os.environ['DATABASE_URL']",
            "if database_url.startswith('postgresql+asyncpg://'):",
            "    sync_database_url = database_url.replace('postgresql+asyncpg://', 'postgresql+psycopg2://', 1)",
            "elif database_url.startswith('postgresql://'):",
            "    sync_database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)",
            "else:",
            "    sync_database_url = database_url",
            "engine = create_engine(sync_database_url, pool_pre_ping=True, future=True)",
            "with engine.begin() as connection:",
            "    connection.execute(text('TRUNCATE TABLE available_agents_runtime'))",
            "    connection.execute(text('DELETE FROM agents'))",
            "engine.dispose()",
        )
    )

    registry_port = str(urlsplit(DEFAULT_REGISTRY_URL).port or 9001)
    registry_mtls_port = str(urlsplit(DEFAULT_REGISTRY_MTLS_URL).port or 9002)
    ca_port = str(urlsplit(DEFAULT_CA_URL).port or 9003)
    discovery_port = str(urlsplit(DEFAULT_DISCO_URL).port or 9005)
    monitor_port = str(urlsplit(DEFAULT_MONITOR_URL).port or 9009)

    return {
        "registry": LocalServiceSpec(
            name="registry-server",
            repo_path=WORKSPACE_ROOT / "registry-server",
            default_base_url=DEFAULT_REGISTRY_URL,
            health_urls=(_derive_health_url(DEFAULT_REGISTRY_URL),),
            startup_timeout_seconds=10,
            prepare_commands=(
                (
                    str(WORKSPACE_ROOT / "registry-server" / ".venv/bin/alembic"),
                    "upgrade",
                    "head",
                ),
                (
                    str(WORKSPACE_ROOT / "registry-server" / ".venv/bin/python"),
                    "-c",
                    registry_seed_script,
                ),
            ),
            launch_commands=(
                (
                    str(WORKSPACE_ROOT / "registry-server" / ".venv/bin/uvicorn"),
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    registry_port,
                ),
                (
                    str(WORKSPACE_ROOT / "registry-server" / ".venv/bin/python"),
                    "-m",
                    "app.main_mtls",
                ),
            ),
            env={
                "APP_ENV": "testing",
                "DATABASE_URL": DEFAULT_REGISTRY_TEST_DATABASE_URL,
                "TEST_DATABASE_URL": DEFAULT_REGISTRY_TEST_DATABASE_URL,
                "CA_SERVER_MOCK": "false",
                "CA_SERVER_BASE_URL": DEFAULT_CA_URL,
                "CA_SERVER_INTERNAL_API_TOKEN": DEFAULT_CA_INTERNAL_API_TOKEN,
                "REGISTRY_SERVER_MTLS_CERT_FILE": str(runtime.registry_mtls_cert_file),
                "REGISTRY_SERVER_MTLS_KEY_FILE": str(runtime.registry_mtls_key_file),
                "REGISTRY_SERVER_MTLS_CA_CERT_FILE": str(runtime.registry_mtls_ca_file),
                "REGISTRY_SERVER_MTLS_PORT": registry_mtls_port,
                "PYTHONPATH": str(WORKSPACE_ROOT / "registry-server"),
            },
        ),
        "ca": LocalServiceSpec(
            name="ca-server",
            repo_path=WORKSPACE_ROOT / "ca-server",
            default_base_url=DEFAULT_CA_URL,
            health_urls=(_derive_health_url(DEFAULT_CA_URL),),
            startup_timeout_seconds=10,
            prepare_commands=(
                (
                    str(WORKSPACE_ROOT / "ca-server" / ".venv/bin/alembic"),
                    "upgrade",
                    "head",
                ),
            ),
            launch_commands=(
                (
                    str(WORKSPACE_ROOT / "ca-server" / ".venv/bin/uvicorn"),
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    ca_port,
                ),
            ),
            env={
                "APP_ENV": "testing",
                "DATABASE_URL": DEFAULT_CA_TEST_DATABASE_URL,
                "TEST_DATABASE_URL": DEFAULT_CA_TEST_DATABASE_URL,
                "REGISTRY_SERVER_MOCK": "false",
                "REGISTRY_SERVER_URL": f"{DEFAULT_REGISTRY_URL}/acps-atr-v2",
                "ACME_DIRECTORY_URL": f"{DEFAULT_CA_URL}/acps-atr-v2/acme",
                "CA_SERVER_ADMIN_API_TOKEN": runtime.ca_admin_api_token,
                "CA_SERVER_INTERNAL_API_TOKEN": DEFAULT_CA_INTERNAL_API_TOKEN,
                "PYTHONPATH": str(WORKSPACE_ROOT / "ca-server"),
            },
        ),
        "discovery": LocalServiceSpec(
            name="discovery-server",
            repo_path=discovery_repo_path,
            default_base_url=DEFAULT_DISCO_URL,
            health_urls=(_derive_health_url(DEFAULT_DISCO_URL),),
            startup_timeout_seconds=30,
            prepare_commands=(
                (
                    str(discovery_repo_path / ".venv/bin/python"),
                    "scripts/ensure_test_database.py",
                ),
                (
                    str(discovery_repo_path / ".venv/bin/alembic"),
                    "upgrade",
                    "head",
                ),
                (
                    str(discovery_repo_path / ".venv/bin/python"),
                    "-c",
                    discovery_reset_script,
                ),
            ),
            launch_commands=(
                (
                    str(discovery_repo_path / ".venv/bin/uvicorn"),
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    discovery_port,
                ),
            ),
            env={
                "APP_ENV": "development",
                "UVICORN_PORT": discovery_port,
                "UVICORN_RELOAD": "false",
                "DATABASE_URL": discovery_test_database_url,
                "TEST_DATABASE_URL": discovery_test_database_url,
                "TEST_DATABASE_ADMIN_URL": os.getenv(
                    "ACPS_CLI_MANAGED_DISCOVERY_DATABASE_ADMIN_URL",
                    "postgresql://postgres:devpass@localhost:5432/postgres",
                ),
                "DISCOVERY_MODE": "cpu",
                "DSP_BASE_URL": f"{DEFAULT_REGISTRY_URL}/acps-dsp-v2",
                "DSP_WEBHOOK_RECEIVE_URL": f"{DEFAULT_DISCO_URL}/admin/dsp/webhooks/receive",
                "DSP_AUTO_START": "false",
                "POLLING_SERVER_URL": "",
                "FORWARDER_SERVER_ENABLED": "false",
                "PYTHONPATH": str(discovery_repo_path),
            },
        ),
        "mq": LocalServiceSpec(
            name="mq-auth-server",
            repo_path=MQ_AUTH_REPO,
            default_base_url=DEFAULT_MQ_GROUP_URL,
            # 两个 listener 都需要通过健康检查
            health_urls=(
                f"{DEFAULT_MQ_GROUP_URL}/health",
                f"{DEFAULT_MQ_AUTH_URL}/health",
            ),
            startup_timeout_seconds=25,
            prepare_commands=(),
            launch_commands=((str(MQ_AUTH_REPO / ".venv/bin/mq-auth-server"),),),
            env={
                "APP_ENV": "development",
                # RABBITMQ_MGMT_PASS 为必填字段，使用 dev-infra 默认密码
                "RABBITMQ_MGMT_PASS": "devpass",
                "RABBITMQ_MGMT_URL": os.getenv("RABBITMQ_MGMT_URL", "http://localhost:15672"),
                "REDIS_URL": os.getenv("ACPS_CLI_MANAGED_MQ_REDIS_URL", "redis://localhost:6379/0"),
                "MQ_AUTH_GROUP_API_PORT": str(urlsplit(DEFAULT_MQ_GROUP_URL).port or 9007),
                "MQ_AUTH_AUTH_API_PORT": str(urlsplit(DEFAULT_MQ_AUTH_URL).port or 9008),
                "PYTHONPATH": str(MQ_AUTH_REPO),
            },
            health_cert=(
                str(MQ_AUTH_CERTS_DIR / "client.pem"),
                str(MQ_AUTH_CERTS_DIR / "client.key"),
            ),
            health_ca=str(MQ_AUTH_CERTS_DIR / "acps-root-ca.pem"),
        ),
        "monitor": LocalServiceSpec(
            name="monitor-server",
            repo_path=MONITOR_REPO,
            default_base_url=DEFAULT_MONITOR_URL,
            health_urls=(_derive_health_url(DEFAULT_MONITOR_URL),),
            startup_timeout_seconds=30,
            prepare_commands=(
                (
                    str(MONITOR_REPO / ".venv/bin/alembic"),
                    "upgrade",
                    "head",
                ),
                (
                    str(MONITOR_REPO / ".venv/bin/python"),
                    "-c",
                    (
                        "import asyncio\n"
                        "from tests.support.clickhouse_helper import ensure_test_schema\n"
                        "asyncio.run(ensure_test_schema())\n"
                    ),
                ),
            ),
            launch_commands=(
                (
                    str(MONITOR_REPO / ".venv/bin/uvicorn"),
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    monitor_port,
                ),
            ),
            env={
                "APP_ENV": "development",
                "DATABASE_URL": DEFAULT_MONITOR_TEST_DATABASE_URL,
                "TEST_DATABASE_URL": DEFAULT_MONITOR_TEST_DATABASE_URL,
                "REDIS_URL": DEFAULT_MONITOR_TEST_REDIS_URL,
                "CLICKHOUSE_DATABASE": "amp_test",
                "KAFKA_BOOTSTRAP_SERVERS": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"),
                "CLICKHOUSE_HOST": os.getenv("CLICKHOUSE_HOST", "localhost"),
                "CLICKHOUSE_PORT": os.getenv("CLICKHOUSE_PORT", "8123"),
                "VM_QUERY_URL": os.getenv("VM_QUERY_URL", "http://localhost:8428"),
                "VM_REMOTE_WRITE_URL": os.getenv("VM_REMOTE_WRITE_URL", "http://localhost:8428"),
                "MINIO_ENDPOINT": os.getenv("MINIO_ENDPOINT", "http://localhost:19000"),
                "MINIO_ACCESS_KEY": os.getenv("MINIO_ACCESS_KEY", "admin"),
                "MINIO_SECRET_KEY": os.getenv("MINIO_SECRET_KEY", "devpass"),
                "OPENSEARCH_HOSTS": os.getenv("OPENSEARCH_HOSTS", "http://localhost:9200"),
                "OPENSEARCH_VERIFY_CERTS": "false",
                "PYTHONPATH": str(MONITOR_REPO),
            },
        ),
    }


def get_mq_dev_cert_dir(runtime: LocalServiceRuntime) -> Path:
    """返回 mq-auth-server dev certs 目录（已就绪后才调用）。"""

    if runtime.mq_cert_dir is not None:
        return runtime.mq_cert_dir
    # mq 尚未被受管启动时，尝试直接使用已有路径（服务可能已独立启动）
    return MQ_AUTH_CERTS_DIR


def get_managed_registry_mtls_endpoint(
    runtime: LocalServiceRuntime,
) -> tuple[str, Path] | None:
    """返回受管 registry 真实 mTLS listener 的访问地址与信任锚。"""

    if runtime.registry_mtls_url is None or runtime.registry_mtls_ca_file is None:
        return None
    return runtime.registry_mtls_url, runtime.registry_mtls_ca_file


def get_managed_registry_mtls_probe_materials(
    runtime: LocalServiceRuntime,
) -> tuple[Path, Path] | None:
    """返回受管 registry 9002 listener 的临时 probe client 证书。"""

    if runtime.registry_mtls_probe_cert_file is None or runtime.registry_mtls_probe_key_file is None:
        return None
    return runtime.registry_mtls_probe_cert_file, runtime.registry_mtls_probe_key_file


def _merge_env(extra_env: dict[str, str]) -> dict[str, str]:
    passthrough_keys = {
        "ALL_PROXY",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DEV_INFRA_COMPOSE_FILE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMP",
        "TMPDIR",
        "TEMP",
        "USER",
    }
    env = {key: value for key, value in os.environ.items() if key in passthrough_keys}
    env.pop("VIRTUAL_ENV", None)
    if "DOCKER_CONFIG" not in env:
        env["DOCKER_CONFIG"] = str(_ensure_public_docker_config_dir())
    env.update(extra_env)
    return env


def _run_command(command: tuple[str, ...], *, cwd: Path, env: dict[str, str], purpose: str) -> None:
    """执行受管启动/停止命令，并在失败时抛出带上下文的异常。"""

    result = subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        env=_merge_env(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return

    raise RuntimeError(
        f"{purpose} 失败（cwd={cwd}, command={' '.join(command)}）\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _start_service_processes(spec: LocalServiceSpec) -> list[subprocess.Popen[str]]:
    """直接用 sibling 仓库自己的虚拟环境启动服务进程。"""

    processes: list[subprocess.Popen[str]] = []
    log_dir = spec.repo_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    for command in spec.prepare_commands:
        _run_command(
            command,
            cwd=spec.repo_path,
            env=spec.env,
            purpose=f"准备 {spec.name} 数据库",
        )

    for index, command in enumerate(spec.launch_commands, start=1):
        executable = Path(command[0])
        if not executable.exists():
            raise RuntimeError(f"{spec.name} 缺少启动可执行文件：{executable}")

        log_path = log_dir / f"pytest-managed-{spec.repo_path.name}-{index}.log"
        with log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(  # noqa: S603
                list(command),
                cwd=spec.repo_path,
                env=_merge_env(spec.env),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        processes.append(process)

    time.sleep(1)
    for process in processes:
        if process.poll() is not None:
            raise RuntimeError(
                f"{spec.name} 启动后立即退出，请检查 {spec.repo_path / 'logs'} 中的 pytest-managed 日志。"
            )

    return processes


def _stop_processes(processes: list[subprocess.Popen[str]]) -> None:
    """停止由测试夹具拉起的服务进程。"""

    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except ProcessLookupError:
            continue
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _derive_health_url(base_url: str) -> str:
    """从服务基础地址推导标准健康检查地址。"""

    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise RuntimeError(f"无法从 base_url 推导健康检查地址：{base_url}")
    return f"{parts.scheme}://{parts.netloc}/health"


def _uses_default_local_url(base_url: str, default_base_url: str) -> bool:
    return base_url.rstrip("/") == default_base_url.rstrip("/")


def _ensure_shared_infra(runtime: LocalServiceRuntime, *, required_services: list[str]) -> None:
    """按需启动共享 infra，并等待所需依赖就绪。"""

    infra_services: list[str] = ["postgres", "redis"]
    if "mq" in required_services:
        infra_services.append("rabbitmq")
    if "monitor" in required_services:
        infra_services.extend(["kafka", "victoria-metrics", "clickhouse", "opensearch"])

    missing_services = [service for service in infra_services if service not in runtime.prepared_infra_services]
    if not missing_services:
        return

    _run_command(
        ("just", "infra", "up", *missing_services),
        cwd=CLI_REPO,
        env={},
        purpose=f"启动共享 infra：{', '.join(missing_services)}",
    )
    _run_command(
        ("just", "infra", "wait", *missing_services),
        cwd=CLI_REPO,
        env={},
        purpose=f"等待共享 infra 就绪：{', '.join(missing_services)}",
    )
    runtime.prepared_infra_services.update(missing_services)


def ensure_local_services(
    runtime: LocalServiceRuntime,
    *,
    required_services: list[str],
    base_urls: dict[str, str],
) -> None:
    """确保默认本地联调服务已就绪；若缺失则自动 bootstrap + start。"""

    specs = _service_specs(runtime)
    services_to_start: list[str] = []

    for service_name in required_services:
        spec = specs[service_name]
        base_url = base_urls[service_name]
        health_url = _derive_health_url(base_url)
        # mTLS 服务（如 mq-auth-server）需要客户端证书才能连接
        verify: str | bool = spec.health_ca if spec.health_ca is not None else (not bool(spec.health_cert))
        if wait_for_service(health_url, verify=verify, cert=spec.health_cert):
            continue
        if not _uses_default_local_url(base_url, spec.default_base_url):
            raise RuntimeError(
                f"{spec.name} 不可达：{health_url}。"
                f"当前只会自动托管默认本地地址 {spec.default_base_url}，"
                "请先自行启动该自定义目标服务。"
            )
        services_to_start.append(service_name)

    if not services_to_start:
        return

    _ensure_shared_infra(runtime, required_services=services_to_start)

    for service_name in services_to_start:
        spec = specs[service_name]
        if service_name in runtime.started_services:
            _stop_processes(runtime.managed_processes.get(service_name, []))
            runtime.managed_processes.pop(service_name, None)
            runtime.started_services = [name for name in runtime.started_services if name != service_name]

        processes = _start_service_processes(spec)
        runtime.managed_processes[service_name] = processes

        verify_startup: str | bool = spec.health_ca if spec.health_ca is not None else (not bool(spec.health_cert))
        if not all(
            wait_for_service(
                url,
                timeout=spec.startup_timeout_seconds,
                verify=verify_startup,
                cert=spec.health_cert,
            )
            for url in spec.health_urls
        ):
            _stop_processes(processes)
            runtime.managed_processes.pop(service_name, None)
            raise RuntimeError(f"{spec.name} 启动后仍不可达：{', '.join(spec.health_urls)}")

        # 记录 mq-auth-server cert 目录，供夹具使用
        if service_name == "mq":
            runtime.mq_cert_dir = MQ_AUTH_CERTS_DIR
        if service_name == "discovery":
            os.environ[MANAGED_DISCOVERY_DATABASE_URL_ENV] = spec.env["DATABASE_URL"]

        runtime.started_services.append(service_name)


def stop_local_services(runtime: LocalServiceRuntime) -> None:
    """停止当前 pytest 进程中由测试夹具启动的 sibling 服务。"""

    for service_name in reversed(runtime.started_services):
        _stop_processes(runtime.managed_processes.get(service_name, []))
    for temp_dir in runtime.temp_dirs:
        shutil.rmtree(temp_dir, ignore_errors=True)
    os.environ.pop(MANAGED_DISCOVERY_DATABASE_URL_ENV, None)
