"""测试全局 conftest — 环境初始化与共享 fixture。

职责：
1. 加载项目根目录 .env 文件（若存在）。
2. 在导入应用代码前强制设置 APP_ENV=testing 并校验 TEST_DATABASE_URL。
3. 提供全局共享的 async session fixture（事务回滚隔离）。
"""

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


_load_dotenv_file(PROJECT_ROOT / ".env")

# 在导入应用配置和数据库底座前固定测试环境
os.environ["APP_ENV"] = "testing"
# ClickHouse 集成测试必须使用独立测试库 amp_test，与开发库 amp 隔离。
# 强制覆盖（不可 setdefault）：.env / just 可能已注入 CLICKHOUSE_DATABASE=amp。
os.environ["CLICKHOUSE_DATABASE"] = "amp_test"

from tests.support.constants import DEFAULT_TEST_DATABASE_DSN, TEST_DATABASE_NAME  # noqa: E402


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
        candidate_url = DEFAULT_TEST_DATABASE_DSN
        candidate_name = "DEFAULT_TEST_DATABASE_DSN"

    database_name = _extract_database_name(candidate_url)
    if database_name != TEST_DATABASE_NAME:
        raise RuntimeError(
            f"测试启动失败：{candidate_name} 当前指向 {database_name}，"
            f"pytest 只允许连接测试数据库 {TEST_DATABASE_NAME}。\n"
            f"请在 .env 中配置或显式导出 TEST_DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/{TEST_DATABASE_NAME}。"
        )

    os.environ["DATABASE_URL"] = candidate_url
    os.environ["TEST_DATABASE_URL"] = candidate_url


_configure_test_database_url()


import pytest  # noqa: E402  (must come after env setup)
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402


@pytest.fixture
async def async_session() -> AsyncGenerator[AsyncSession]:
    """提供事务回滚隔离的异步数据库 session（集成测试用）。

    Yields:
        AsyncSession: 每个测试函数独立的数据库 session，测试结束后自动回滚。
    """
    from app.core.db_session import async_session_factory

    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def http_client():
    """提供测试用 ASGI HTTP 客户端（单元/集成测试用）。

    Yields:
        AsyncClient: 绑定到 ASGI app 的 httpx 客户端。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
