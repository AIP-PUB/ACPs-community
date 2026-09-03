"""集成测试 conftest — 数据库隔离 fixture 与测试数据工厂。

每个集成测试函数在独立的数据库状态下运行：
- isolated_database（autouse）在每个测试前后调用 reset_database_state()
- reset_database_state() 直接通过 engine.begin() 操作，与 AsyncSession 解耦，
  避免 asyncpg「another operation is in progress」错误
- isolated_clickhouse（autouse=False，Access 集成测试 fixture）在每个 Access 测试前后清空三表
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.constants import DEFAULT_TEST_DATABASE_DSN
from tests.support.database import reset_database_state


@pytest.fixture(scope="session", autouse=True)
async def _close_shared_clients_on_exit() -> AsyncGenerator[None]:
    """在集成测试 session 结束时关闭进程级共享客户端，避免 event loop 关闭后残留连接告警。"""
    yield

    from app.core.clickhouse_client import close_clickhouse_client
    from app.core.opensearch_client import close_opensearch_client
    from app.core.redis_client import close_redis
    from app.metrics.tsdb import close_tsdb_client

    await close_clickhouse_client()
    await close_opensearch_client()
    await close_tsdb_client()
    await close_redis()


@pytest.fixture(scope="session", autouse=True)
def _require_test_db() -> None:
    """确保 DATABASE_URL 已指向测试库（conftest.py 顶层已设置，此处作防护断言）。"""
    url = os.environ.get("DATABASE_URL", DEFAULT_TEST_DATABASE_DSN)
    assert "agent_monitor_test" in url, f"集成测试只能使用 agent_monitor_test 库，当前 DATABASE_URL={url!r}"


@pytest.fixture(scope="session")
async def _require_clickhouse() -> None:
    """确保 ClickHouse 可达；不可达时 skip（防止集成测试随机超时）。"""
    try:
        from app.core.clickhouse_client import check_clickhouse

        ok = await check_clickhouse()
    except Exception as exc:
        pytest.skip(f"ClickHouse 不可达，跳过集成测试：{exc}")
    if not ok:
        pytest.skip("ClickHouse 健康检查失败，跳过集成测试")


@pytest.fixture(scope="session")
async def clickhouse_schema(_require_clickhouse: None) -> None:
    """Session 级建表 fixture（CREATE IF NOT EXISTS，幂等）。"""
    from tests.support.clickhouse_helper import ensure_test_schema

    await ensure_test_schema()


@pytest.fixture
async def isolated_clickhouse(clickhouse_schema: None) -> AsyncGenerator[None]:
    """每个 Access 集成测试前后清空三表（函数级隔离）。"""
    from tests.support.clickhouse_helper import truncate_access_tables

    await truncate_access_tables()
    yield
    await truncate_access_tables()


@pytest.fixture
async def isolated_message_clickhouse(clickhouse_schema: None) -> AsyncGenerator[None]:
    """每个 Message 集成测试前后清空四表（函数级隔离）。"""
    from tests.support.clickhouse_helper import truncate_message_tables

    await truncate_message_tables()
    yield
    await truncate_message_tables()


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncGenerator[None]:
    """在每个集成测试前后重置数据库状态。

    使用 reset_database_state()（直接 engine.begin()），完全独立于测试中的
    AsyncSession，安全地在任何 fixture 拆除时机运行。
    """
    from app.core.clickhouse_client import close_clickhouse_client
    from app.core.opensearch_client import close_opensearch_client
    from app.core.redis_client import close_redis
    from app.metrics.tsdb import close_tsdb_client

    await reset_database_state()
    yield
    await close_clickhouse_client()
    await close_opensearch_client()
    await close_tsdb_client()
    await close_redis()
    await reset_database_state()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """提供集成测试用的数据库 session（不回滚，供 writer 提交后验证用）。"""
    from app.core.db_session import async_session_factory

    async with async_session_factory() as session:
        yield session


@pytest.fixture
async def http_client_integration() -> AsyncGenerator[AsyncClient]:
    """提供绑定到 ASGI app 的 HTTP 客户端（集成测试用）。

    测试模式下 lifespan 跳过 Kafka Consumer 启动，只提供 Query API。
    """
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ─── 测试数据工厂 ─────────────────────────────────────────────────────────────


def make_log_record(
    log_id: str | None = None,
    timestamp: str = "2026-06-09T10:00:00+00:00",
    aic: str = "aic-test-001",
    actor_id: str = "user-001",
    actor_type: str = "human",
    action_name: str = "login",
    action_type: str = "auth",
    target_type: str = "session",
    target_id: str = "sess-001",
    result_status: str = "success",
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个合法的 audit LogRecord dict（用于直接调用 writer._process_audit_record）。

    body 采用 AMP Spec §5.6 的嵌套结构（actor / action / target / result）。
    """
    import uuid

    return {
        "schema_version": "1.0",
        "log_id": log_id or str(uuid.uuid4()),
        "log_type": "audit",
        "timestamp": timestamp,
        "aic": aic,
        "body": {
            "actor": {"id": actor_id, "type": actor_type},
            "action": {"name": action_name, "type": action_type},
            "target": {"type": target_type, "id": target_id},
            "result": {"status": result_status},
        },
        "integrity": integrity,
        "trace_id": None,
        "correlation_id": None,
    }


def make_signed_log_record(
    private_key: Ed25519PrivateKey,
    kid: str = "test-kid-001",
    **kwargs: Any,
) -> dict[str, Any]:
    """构造带有效 EdDSA 签名的 audit LogRecord dict。"""
    import base64

    import jcs

    record = make_log_record(**kwargs)
    signable = {k: v for k, v in record.items() if k != "integrity"}
    canonical = jcs.canonicalize(signable)
    sig_bytes = private_key.sign(canonical)
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")

    record["integrity"] = {"alg": "EdDSA", "kid": kid, "sig": sig_b64}
    return record


@pytest.fixture
def ed25519_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """生成一次性 EdDSA 测试密钥对。返回 (private_key, public_key_raw_bytes)。"""
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


@pytest.fixture
def audit_writer_with_mock_keys(
    ed25519_keypair: tuple[Ed25519PrivateKey, bytes],
) -> tuple[Any, Ed25519PrivateKey, str]:
    """返回 (AuditWriter, private_key, kid)，writer 使用 MockKeyResolver。"""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from app.audit.key_resolver import MockKeyResolver
    from app.audit.writer import AuditWriter

    priv, _pub_raw = ed25519_keypair
    kid = "test-kid-001"
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    resolver = MockKeyResolver({kid: pub_pem})
    writer = AuditWriter(key_resolver=resolver)
    return writer, priv, kid
