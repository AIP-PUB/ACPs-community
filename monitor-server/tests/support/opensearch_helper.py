"""tests/support/opensearch_helper.py — OpenSearch 集成/E2E 测试辅助函数。

提供：
- create_test_index：确保索引存在（依赖已 bootstrap 的 index template）
- bulk_insert：批量写入并可选刷新
- refresh_index：强制刷新使写入立即可见
- delete_indices：按 pattern 删除测试索引（隔离用）
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from app.core.opensearch_client import get_opensearch_client
from app.system import indices
from app.system.normalizer import SystemEventDoc, build_document
from app.system.store import bulk_index


async def create_test_index(
    *,
    timestamp_iso: str | None = None,
    number_of_shards: int = 1,
    number_of_replicas: int = 0,
) -> str:
    """创建单日测试索引（名称由 timestamp 推导），返回索引名。

    索引 mapping 来自已 bootstrap 的 index template；若模板不存在会先 ensure schema。
    """
    from app.core.config import settings
    from app.system.store import ensure_system_schema

    await ensure_system_schema(
        number_of_shards=number_of_shards,
        number_of_replicas=number_of_replicas,
        hot_days=settings.system_event_hot_retention_days,
        warm_days=settings.system_event_warm_retention_days,
        archive_days=settings.system_archive_retention_days,
    )

    if timestamp_iso is None:
        timestamp_iso = datetime.now(UTC).isoformat()
    dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    index_name = indices.index_for_timestamp(int(dt.timestamp() * 1000))

    client = await get_opensearch_client()
    if not await client.indices.exists(index=index_name):
        await client.indices.create(index=index_name)
    return index_name


async def bulk_insert(
    docs: list[SystemEventDoc],
    *,
    indexed_at_iso: str | None = None,
    refresh: bool = True,
) -> int:
    """bulk_index 封装；默认 refresh 使文档立即可搜。"""
    if indexed_at_iso is None:
        indexed_at_iso = datetime.now(UTC).isoformat()
    result = await bulk_index(docs, indexed_at_iso=indexed_at_iso)
    if refresh and result.indexed > 0:
        affected = {doc.index for doc in docs}
        for index_name in affected:
            await refresh_index(index_name)
    return result.indexed


def make_test_doc(
    *,
    log_id: str,
    message: str = "integration test message",
    aic: str = "aic-int-test",
    timestamp_iso: str | None = None,
    include_raw_log: bool = True,
) -> SystemEventDoc:
    """从最小 LogRecord 字段构造 SystemEventDoc（集成测试用）。"""
    from acps_sdk.amp.models import LogRecord

    if timestamp_iso is None:
        timestamp_iso = datetime.now(UTC).isoformat()

    record = LogRecord.model_validate(
        {
            "schema_version": "1.0",
            "log_id": log_id,
            "log_type": "system",
            "timestamp": timestamp_iso,
            "aic": aic,
            "severity_number": 9,
            "body": {"message": message},
            "resource": {"service.name": "integration-test"},
        }
    )
    return build_document(record, log_id=log_id, search_text_max_length=4096)


async def refresh_index(index: str = indices.INDEX_PATTERN) -> None:
    """强制刷新索引，使 Bulk 写入立即可搜（测试专用）。"""
    client = await get_opensearch_client()
    await client.indices.refresh(index=index, ignore_unavailable=True)


async def delete_indices(pattern: str = indices.INDEX_PATTERN) -> None:
    """删除匹配 pattern 的索引（测试隔离；忽略不存在）。"""
    client = await get_opensearch_client()
    with contextlib.suppress(Exception):
        await client.indices.delete(index=pattern, ignore_unavailable=True)
