"""tests/integration/system/test_system_store.py — store.py 真实 OpenSearch 集成测试（C-1）。

覆盖 schema bootstrap 幂等、bulk_index 写入、PIT 搜索、ISM 补挂幂等。
需要真实 OpenSearch（dev-infra dev-opensearch 服务）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.system import dsl
from app.system.planner import ResolvedSort
from app.system.store import (
    close_pit,
    ensure_ism_attached,
    ensure_system_schema,
    open_pit,
    search_events,
)
from tests.support.opensearch_helper import bulk_insert, make_test_doc


@pytest.fixture(autouse=True)
async def _schema_ready(_require_opensearch: None) -> None:
    """本文件所有测试依赖 OpenSearch schema 已就绪。"""


class TestEnsureSystemSchema:
    async def test_idempotent_bootstrap(self) -> None:
        """连续两次 ensure_system_schema 不报错（幂等）。"""
        from app.core.config import settings

        kwargs = {
            "number_of_shards": settings.system_index_number_of_shards,
            "number_of_replicas": settings.system_index_number_of_replicas,
            "hot_days": settings.system_event_hot_retention_days,
            "warm_days": settings.system_event_warm_retention_days,
            "archive_days": settings.system_archive_retention_days,
        }
        await ensure_system_schema(**kwargs)
        await ensure_system_schema(**kwargs)


class TestBulkIndex:
    async def test_bulk_index_writes_and_reads_back(self) -> None:
        """bulk_index 写入后 refresh，文档 _id = log_id。"""
        log_id = str(uuid.uuid4())
        doc = make_test_doc(log_id=log_id, message="bulk integration test")
        indexed = await bulk_insert([doc])
        assert indexed == 1

        from app.core.opensearch_client import get_opensearch_client

        client = await get_opensearch_client()
        resp = await client.get(index=doc.index, id=log_id)
        assert resp["_source"]["log_id"] == log_id
        assert resp["_source"]["message"] == "bulk integration test"


class TestSearchEvents:
    async def test_pit_search_include_raw_log_flag(self) -> None:
        """search_events PIT 搜索；include_raw_log 控制 rawBody 投影。"""
        log_id = str(uuid.uuid4())
        doc = make_test_doc(log_id=log_id, message="pit search test")
        await bulk_insert([doc])

        now = datetime.now(UTC)
        start_ms = int((now.timestamp() - 3600) * 1000)
        end_ms = int(now.timestamp() * 1000)

        time_clause = dsl.build_time_range_clause(from_ms=start_ms, to_ms=end_ms)
        search_body = dsl.build_search_body(
            filter_clauses=[],
            keyword_query=None,
            time_clause=time_clause,
            scope_clauses=[],
            sort=dsl.build_sort([ResolvedSort("timestamp", "timestamp", "desc")]),
            search_after=None,
            size=10,
        )

        pit_id = await open_pit(keep_alive="2m")
        try:
            hits_with_raw = await search_events(
                search_body,
                pit_id=pit_id,
                keep_alive="2m",
                include_raw_log=True,
            )
            hits_without_raw = await search_events(
                search_body,
                pit_id=pit_id,
                keep_alive="2m",
                include_raw_log=False,
            )
        finally:
            await close_pit(pit_id)

        matched_with = [h for h in hits_with_raw if h.view.log_id == log_id]
        matched_without = [h for h in hits_without_raw if h.view.log_id == log_id]
        assert len(matched_with) == 1
        assert matched_with[0].view.raw_body is not None
        assert len(matched_without) == 1
        assert matched_without[0].view.raw_body is None
        assert "search_text" not in matched_with[0].view.model_dump(by_alias=True)
        assert "indexed_at" not in matched_with[0].view.model_dump(by_alias=True)


class TestEnsureIsmAttached:
    async def test_idempotent_ism_attach(self) -> None:
        """ensure_ism_attached 重复调用无副作用。"""
        await ensure_ism_attached()
        await ensure_ism_attached()
