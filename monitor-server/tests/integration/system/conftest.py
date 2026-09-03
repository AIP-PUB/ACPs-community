"""tests/integration/system/conftest.py — System 集成测试 OpenSearch fixture。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from tests.support.opensearch_helper import delete_indices


@pytest.fixture(scope="session")
async def _require_opensearch() -> AsyncGenerator[None]:
    """确保 OpenSearch 可达且 schema 已 bootstrap。"""
    from app.core.config import settings
    from app.core.opensearch_client import check_opensearch, close_opensearch_client
    from app.system import store

    ok = await check_opensearch()
    if not ok:
        pytest.fail("OpenSearch 不可达，请先执行 just test bootstrap 或 just infra up opensearch")

    await store.ensure_system_schema(
        number_of_shards=settings.system_index_number_of_shards,
        number_of_replicas=settings.system_index_number_of_replicas,
        hot_days=settings.system_event_hot_retention_days,
        warm_days=settings.system_event_warm_retention_days,
        archive_days=settings.system_archive_retention_days,
    )

    yield

    await close_opensearch_client()


@pytest.fixture(autouse=True)
async def isolated_system_indices(_require_opensearch: None) -> AsyncGenerator[None]:
    """每个集成测试前后删除 amp-system-events-* 索引（防状态污染）。"""
    await delete_indices()
    yield
    await delete_indices()
