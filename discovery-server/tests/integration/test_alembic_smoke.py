from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration


def _to_sync_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgresql+psycopg2://"):
        return database_url
    raise ValueError("TEST_DATABASE_URL 必须是 PostgreSQL URL")


def test_test_database_has_applied_alembic_revision(test_database_url: str) -> None:
    engine = create_engine(_to_sync_database_url(test_database_url), pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one_or_none()
    finally:
        engine.dispose()

    assert revision, "测试数据库缺少 alembic_version 记录，请先执行 just test bootstrap。"


def test_alive_sync_tables_and_index_exist(test_database_url: str) -> None:
    engine = create_engine(_to_sync_database_url(test_database_url), pool_pre_ping=True, future=True)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "agent_alive_status" in table_names
        assert "alive_sync_shard_state" in table_names

        index_names = {item["name"] for item in inspector.get_indexes("agent_alive_status")}
        assert "idx_agent_alive_status_shard" in index_names
    finally:
        engine.dispose()
