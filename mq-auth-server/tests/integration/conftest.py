"""集成测试 fixtures。

使用真实 Redis（需要 REDIS_URL 环境变量指向测试 Redis 实例）。
需要运行 Redis 服务时才能执行，不可用时自动 skip。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable
from typing import TYPE_CHECKING

import pytest
from cachetools import TTLCache

from tests.conftest import VALID_GROUP_ID, VALID_LEADER_AIC, FakeRabbitMqManagementClient

if TYPE_CHECKING:
    from app.services.group_acl import GroupAclService


@pytest.fixture(autouse=True)
def reset_lru_cache() -> None:
    """每个测试前清除 get_settings() 缓存。"""
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/1")


@pytest.fixture(scope="session")
async def redis_available(redis_url: str) -> bool:
    """检查 Redis 是否可用，不可用时跳过整个 session。"""
    from redis import asyncio as redis_asyncio
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    client = redis_asyncio.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
    try:
        ping_result = client.ping()
        if isinstance(ping_result, Awaitable):
            return bool(await ping_result)
        return bool(ping_result)
    except RedisConnectionError, RedisTimeoutError, OSError:
        pytest.skip("Redis not available — 跳过集成测试")
    finally:
        await client.aclose()


@pytest.fixture
async def redis_group_acl_service(redis_url: str, redis_available: bool) -> AsyncIterator[GroupAclService]:
    """创建基于真实 Redis 的 GroupAclService，每个测试独立隔离 key 前缀。"""
    from app.services.group_acl import GroupAclService, RedisGroupAclStore

    store = RedisGroupAclStore(redis_url, tls_ca_cert=None, tls_check_hostname=False)
    service = GroupAclService(
        store=store,
        management_client=FakeRabbitMqManagementClient(),
        local_cache=TTLCache(maxsize=128, ttl=5),
        key_ttl_seconds=60,
    )
    # 清理测试数据
    cache_key = GroupAclService.build_cache_key(VALID_LEADER_AIC, VALID_GROUP_ID)
    await store.delete(cache_key)
    yield service
    # 清理
    await store.delete(cache_key)
    await store.aclose()
