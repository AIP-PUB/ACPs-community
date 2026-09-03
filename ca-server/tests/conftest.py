import os
from collections.abc import AsyncGenerator, Generator, MutableMapping
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Session


def _load_dotenv_file(dotenv_path: Path) -> None:
    """将项目根目录 .env 中的配置加载到当前测试进程环境。"""

    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = raw_line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key or normalized_key in os.environ:
            continue

        normalized_value = value.strip()
        if " #" in normalized_value and not normalized_value.startswith(('"', "'")):
            normalized_value = normalized_value.split(" #", 1)[0].rstrip()

        os.environ[normalized_key] = normalized_value.strip().strip('"').strip("'")


def _set_env_default_if_blank(env: MutableMapping[str, str], key: str, value: str) -> None:
    """为空字符串或缺失时写入测试默认值。"""

    if not env.get(key, "").strip():
        env[key] = value


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_file(PROJECT_ROOT / ".env")

# 在导入应用配置和数据库底座前固定测试环境，确保 async engine 使用测试专用配置。
os.environ["APP_ENV"] = "testing"
os.environ["REGISTRY_SERVER_MOCK"] = "true"
_set_env_default_if_blank(os.environ, "REGISTRY_SERVER_INTERNAL_API_TOKEN", "local-registry-server-internal-api-token")
_set_env_default_if_blank(os.environ, "CA_SERVER_INTERNAL_API_TOKEN", "test-ca-internal-token")
_set_env_default_if_blank(os.environ, "CA_SERVER_ADMIN_API_TOKEN", "local-ca-admin-token")

TEST_DATABASE_NAME = "agent_ca_test"
DEFAULT_TEST_DATABASE_URL = "postgresql://ca:ca@localhost:5432/agent_ca_test"


def _extract_database_name(database_url: str) -> str:
    """从数据库连接串中提取数据库名。"""
    database_name = urlsplit(database_url).path.lstrip("/")
    if not database_name:
        raise RuntimeError("测试启动失败：数据库连接串缺少数据库名。")
    return database_name


def _configure_test_database_url() -> None:
    """在导入数据库底座前强制切换到测试专用数据库。"""
    test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    current_database_url = os.environ.get("DATABASE_URL", "").strip()

    if test_database_url:
        candidate_url = test_database_url
        candidate_name = "TEST_DATABASE_URL"
    elif current_database_url:
        candidate_url = current_database_url
        candidate_name = "DATABASE_URL"
    else:
        candidate_url = DEFAULT_TEST_DATABASE_URL
        candidate_name = "DEFAULT_TEST_DATABASE_URL"

    database_name = _extract_database_name(candidate_url)
    if database_name != TEST_DATABASE_NAME:
        raise RuntimeError(
            f"测试启动失败：{candidate_name} 当前指向 {database_name}，"
            f"pytest 只允许连接测试数据库 {TEST_DATABASE_NAME}。"
            "请在 .env 中配置或显式导出 TEST_DATABASE_URL=postgresql://ca:ca@localhost:5432/agent_ca_test。"
        )

    os.environ["DATABASE_URL"] = candidate_url
    os.environ["TEST_DATABASE_URL"] = candidate_url


_configure_test_database_url()


def _setup_test_ca_certs() -> None:
    """校验 certs/ 目录包含完整 CA 套件，缺失时报错提示执行 bootstrap。

    不自动生成证书——证书由 `just test bootstrap`（内含 `just prep certs`）负责准备。
    """
    project_root = Path(__file__).resolve().parent.parent
    certs_dir = project_root / "certs"
    target_files = ["ca.crt", "ca.key", "ca-chain.pem", "trust-bundle.pem"]
    missing = [f for f in target_files if not (certs_dir / f).exists()]
    if missing:
        raise RuntimeError(
            f"测试启动失败：certs/ 目录缺少以下文件：{missing}。请先执行 `just test bootstrap` 初始化测试环境。"
        )


_setup_test_ca_certs()


@pytest.fixture(scope="function", autouse=True)
def reset_public_read_rate_limiter() -> Generator[None]:
    """在每个测试前后清空公开读取限流状态。"""
    from app.core.public_access import PUBLIC_READ_RATE_LIMITER

    PUBLIC_READ_RATE_LIMITER.reset()
    yield
    PUBLIC_READ_RATE_LIMITER.reset()


@pytest.fixture(scope="module")
def client() -> Generator[TestClient]:
    # 在导入 app 之前确保 Registry mock 已启用。
    from app.main import app

    with TestClient(
        app,
        headers={"Authorization": f"Bearer {os.environ['CA_SERVER_ADMIN_API_TOKEN']}"},
    ) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def anonymous_client() -> Generator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="function", autouse=True)
def clean_db() -> Generator[None]:
    """为每个测试函数提供干净的数据库环境 - 自动应用到所有测试"""
    from app.core.db_session import engine
    from tests.integration.test_data_setup import cleanup_test_data

    # 测试前清理
    with Session(engine) as session:
        cleanup_test_data(session)

    yield

    # 测试后清理
    with Session(engine) as session:
        cleanup_test_data(session)


@pytest.fixture
def db_session() -> Generator[Session]:
    """提供数据库会话"""
    from app.core.db_session import engine

    with Session(engine) as session:
        yield session


@pytest.fixture
async def async_db_session() -> AsyncGenerator[AsyncSession]:
    """提供直接驱动异步 service 的数据库会话。"""
    from app.core.db_session import async_session_factory

    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
