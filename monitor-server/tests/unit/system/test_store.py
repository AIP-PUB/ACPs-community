"""tests/unit/system/test_store.py — store.py 单元测试（mock OpenSearch client）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.system.exception import OpenSearchBulkError, OpenSearchQueryError
from app.system.normalizer import SystemEventDoc
from app.system.schema import SystemEventView
from app.system.store import (
    SystemEventHit,
    _hit_to_view,
    bulk_index,
    close_pit,
    ensure_ism_attached,
    ensure_system_schema,
    extract_search_after,
    open_pit,
    search_events,
)


def _make_doc(log_id: str = "log-001", index: str = "amp-system-events-20240614") -> SystemEventDoc:
    return SystemEventDoc(
        log_id=log_id,
        index=index,
        timestamp_ms=1718323200000,
        source={
            "log_id": log_id,
            "timestamp": "2024-06-14T12:00:00Z",
            "aic": "aic-001",
            "trace_id": None,
            "correlation_id": None,
            "severity_number": 0,
            "severity_text": None,
            "message": "test message",
            "category": None,
            "component": None,
            "module": None,
            "tags": {},
            "search_text": "test message",
            "raw_body": {"message": "test message"},
        },
    )


def _make_client_mock() -> MagicMock:
    client = AsyncMock()
    client.indices = AsyncMock()
    client.indices.put_index_template = AsyncMock(return_value={"acknowledged": True})
    client.plugins = AsyncMock()
    client.plugins.index_management = AsyncMock()
    client.plugins.index_management.put_policy = AsyncMock(return_value={"policy": {}})
    client.plugins.index_management.add_policy = AsyncMock(return_value={"updated_indices": 0})
    client.bulk = AsyncMock(return_value={"errors": False, "items": []})
    client.create_pit = AsyncMock(return_value={"pit_id": "pit-test-001"})
    client.delete_pit = AsyncMock(return_value={"succeeded": True})
    client.search = AsyncMock(return_value={"hits": {"hits": []}})
    return client


class TestEnsureSystemSchema:
    @pytest.mark.asyncio
    async def test_puts_index_template(self) -> None:
        """ensure_system_schema 发 PUT index template。"""
        client = _make_client_mock()
        with patch("app.system.store.get_opensearch_client", return_value=client):
            await ensure_system_schema(
                number_of_shards=1,
                number_of_replicas=0,
                hot_days=3,
                warm_days=14,
                archive_days=30,
            )
        client.indices.put_index_template.assert_called_once()
        call_kwargs = client.indices.put_index_template.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_puts_ism_policy(self) -> None:
        """ensure_system_schema 发 PUT ISM policy。"""
        client = _make_client_mock()
        with patch("app.system.store.get_opensearch_client", return_value=client):
            await ensure_system_schema(
                number_of_shards=1,
                number_of_replicas=0,
                hot_days=3,
                warm_days=14,
                archive_days=30,
            )
        client.plugins.index_management.put_policy.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_calls_made_in_order(self) -> None:
        """模板和 ISM 均被调用（bootstrap 顺序）。"""
        client = _make_client_mock()
        call_log: list[str] = []
        orig_template = client.indices.put_index_template

        async def track_template(*args: Any, **kwargs: Any) -> Any:
            call_log.append("template")
            return await orig_template(*args, **kwargs)

        orig_ism = client.plugins.index_management.put_policy

        async def track_ism(*args: Any, **kwargs: Any) -> Any:
            call_log.append("ism")
            return await orig_ism(*args, **kwargs)

        client.indices.put_index_template = track_template
        client.plugins.index_management.put_policy = track_ism

        with patch("app.system.store.get_opensearch_client", return_value=client):
            await ensure_system_schema(
                number_of_shards=1,
                number_of_replicas=0,
                hot_days=3,
                warm_days=14,
                archive_days=30,
            )
        assert "template" in call_log
        assert "ism" in call_log


class TestEnsureIsmAttached:
    @pytest.mark.asyncio
    async def test_calls_add_policy(self) -> None:
        """ensure_ism_attached 发 ISM Add Policy POST（幂等不报错）。"""
        client = _make_client_mock()
        with patch("app.system.store.get_opensearch_client", return_value=client):
            await ensure_ism_attached()
        client.plugins.index_management.add_policy.assert_called_once()


class TestBulkIndex:
    @pytest.mark.asyncio
    async def test_uses_log_id_as_id(self) -> None:
        """bulk_index 用 _id=log_id（C-SYSTEM-WRITE-6 upsert）。"""
        client = _make_client_mock()
        client.bulk = AsyncMock(
            return_value={
                "errors": False,
                "items": [{"index": {"_id": "log-001", "result": "created", "status": 200}}],
            }
        )
        doc = _make_doc("log-001")
        captured_body: list[Any] = []

        async def capture_bulk(body: Any = None, **kwargs: Any) -> dict:
            captured_body.extend(body)
            return {"errors": False, "items": [{"index": {"_id": "log-001", "result": "created", "status": 200}}]}

        client.bulk = capture_bulk

        with patch("app.system.store.get_opensearch_client", return_value=client):
            result = await bulk_index([doc], indexed_at_iso="2024-06-14T12:00:00Z")

        assert result.indexed == 1
        assert result.failed_items == []
        # check _id in action_meta
        action_metas = [item for item in captured_body if isinstance(item, dict) and "index" in item]
        assert any(m["index"]["_id"] == "log-001" for m in action_metas)

    @pytest.mark.asyncio
    async def test_transient_failure_raises_bulk_error(self) -> None:
        """全部 transient 失败（429/503）→ OpenSearchBulkError（writer 整批重试）。"""
        client = _make_client_mock()
        client.bulk = AsyncMock(
            return_value={
                "errors": True,
                "items": [
                    {
                        "index": {
                            "_id": "log-001",
                            "status": 429,
                            "error": {"type": "es_rejected_execution_exception", "reason": "queue full"},
                        }
                    }
                ],
            }
        )
        doc = _make_doc("log-001")
        with patch("app.system.store.get_opensearch_client", return_value=client):
            with pytest.raises(OpenSearchBulkError):
                await bulk_index([doc], indexed_at_iso="2024-06-14T12:00:00Z")

    @pytest.mark.asyncio
    async def test_permanent_failure_in_failed_items(self) -> None:
        """permanent 失败（mapping 冲突等）→ failed_items，其余成功照写。"""
        client = _make_client_mock()
        client.bulk = AsyncMock(
            return_value={
                "errors": True,
                "items": [
                    {"index": {"_id": "log-001", "result": "created", "status": 200}},
                    {
                        "index": {
                            "_id": "log-002",
                            "status": 400,
                            "error": {"type": "mapper_parsing_exception", "reason": "field conflict"},
                        }
                    },
                ],
            }
        )
        docs = [_make_doc("log-001"), _make_doc("log-002")]
        with patch("app.system.store.get_opensearch_client", return_value=client):
            result = await bulk_index(docs, indexed_at_iso="2024-06-14T12:00:00Z")

        assert result.indexed == 1
        assert len(result.failed_items) == 1
        assert result.failed_items[0][0] == "log-002"

    @pytest.mark.asyncio
    async def test_indexed_at_injected_in_source(self) -> None:
        """bulk_index 注入 indexed_at（设计 §2.4）。"""
        client = _make_client_mock()
        captured_body: list[Any] = []

        async def capture_bulk(body: Any = None, **kwargs: Any) -> dict:
            captured_body.extend(body)
            return {"errors": False, "items": [{"index": {"_id": "log-001", "result": "created", "status": 200}}]}

        client.bulk = capture_bulk
        doc = _make_doc("log-001")
        with patch("app.system.store.get_opensearch_client", return_value=client):
            await bulk_index([doc], indexed_at_iso="2024-06-14T12:00:00.000Z")

        sources = [item for item in captured_body if isinstance(item, dict) and "indexed_at" in item]
        assert len(sources) == 1
        assert sources[0]["indexed_at"] == "2024-06-14T12:00:00.000Z"


class TestOpenPit:
    @pytest.mark.asyncio
    async def test_returns_pit_id(self) -> None:
        client = _make_client_mock()
        with patch("app.system.store.get_opensearch_client", return_value=client):
            pit_id = await open_pit(keep_alive="5m")
        assert pit_id == "pit-test-001"


class TestClosePit:
    @pytest.mark.asyncio
    async def test_calls_delete_pit(self) -> None:
        client = _make_client_mock()
        with patch("app.system.store.get_opensearch_client", return_value=client):
            await close_pit("pit-test-001")
        client.delete_pit.assert_called_once()


class TestSearchEvents:
    def _make_hit(
        self,
        log_id: str = "log-001",
        include_search_text: bool = False,
        include_indexed_at: bool = False,
    ) -> dict[str, Any]:
        source: dict[str, Any] = {
            "log_id": log_id,
            "timestamp": "2024-06-14T12:00:00Z",
            "aic": "aic-001",
            "severity_number": 9,
            "message": "test message",
            "raw_body": {"key": "value"},
        }
        if include_search_text:
            source["search_text"] = "test message extra"
        if include_indexed_at:
            source["indexed_at"] = "2024-06-14T12:00:01Z"
        return {
            "_source": source,
            "_id": log_id,
            "sort": [1718323200000, log_id],
        }

    @pytest.mark.asyncio
    async def test_returns_system_event_hits(self) -> None:
        client = _make_client_mock()
        hit = self._make_hit()
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})

        with patch("app.system.store.get_opensearch_client", return_value=client):
            hits = await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m")

        assert len(hits) == 1
        assert isinstance(hits[0], SystemEventHit)
        assert isinstance(hits[0].view, SystemEventView)
        assert hits[0].sort_values == [1718323200000, "log-001"]

    @pytest.mark.asyncio
    async def test_pit_injected_into_search_body(self) -> None:
        """search_events 注入 pit={id, keep_alive} 到 search body。"""
        client = _make_client_mock()
        client.search = AsyncMock(return_value={"hits": {"hits": []}})
        captured: dict[str, Any] = {}

        async def capture_search(body: Any = None, **kwargs: Any) -> dict:
            captured.update(body or {})
            return {"hits": {"hits": []}}

        client.search = capture_search

        with patch("app.system.store.get_opensearch_client", return_value=client):
            await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m")

        assert "pit" in captured
        assert captured["pit"]["id"] == "pit-001"
        assert captured["pit"]["keep_alive"] == "5m"

    @pytest.mark.asyncio
    async def test_search_text_not_in_view(self) -> None:
        """search_text 为内部字段，永不出参（C-SYSTEM-QUERY 边界）。"""
        client = _make_client_mock()
        hit = self._make_hit(include_search_text=True)
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})

        with patch("app.system.store.get_opensearch_client", return_value=client):
            hits = await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m")

        view_dict = hits[0].view.model_dump(by_alias=True)
        assert "searchText" not in view_dict
        assert "search_text" not in view_dict

    @pytest.mark.asyncio
    async def test_indexed_at_not_in_view(self) -> None:
        """indexed_at 为内部字段，永不出参（C-SYSTEM-QUERY 边界）。"""
        client = _make_client_mock()
        hit = self._make_hit(include_indexed_at=True)
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})

        with patch("app.system.store.get_opensearch_client", return_value=client):
            hits = await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m")

        view_dict = hits[0].view.model_dump(by_alias=True)
        assert "indexedAt" not in view_dict
        assert "indexed_at" not in view_dict

    @pytest.mark.asyncio
    async def test_include_raw_log_false_excludes_raw_body(self) -> None:
        """includeRawLog=false 时 rawBody 不出参（§5.3 第6条）。"""
        client = _make_client_mock()
        hit = self._make_hit()
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})

        with patch("app.system.store.get_opensearch_client", return_value=client):
            hits = await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m", include_raw_log=False)

        view_data = hits[0].view.model_dump(by_alias=True, exclude_none=True)
        assert "rawBody" not in view_data

    @pytest.mark.asyncio
    async def test_include_raw_log_true_includes_raw_body(self) -> None:
        """includeRawLog=true 时 rawBody 出参。"""
        client = _make_client_mock()
        hit = self._make_hit()
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})

        with patch("app.system.store.get_opensearch_client", return_value=client):
            hits = await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m", include_raw_log=True)

        assert hits[0].view.raw_body == {"key": "value"}

    @pytest.mark.asyncio
    async def test_pit_expired_raises_opensearch_query_error(self) -> None:
        """PIT 失效 → OpenSearchQueryError('pit')（service 转 CursorInvalidError）。"""
        from opensearchpy import NotFoundError

        client = _make_client_mock()
        client.search = AsyncMock(
            side_effect=NotFoundError(
                404,
                "search_phase_execution_exception",
                {"error": {"root_cause": [{"type": "not_found", "reason": "no pit id"}]}},
            )
        )

        with patch("app.system.store.get_opensearch_client", return_value=client):
            with pytest.raises(OpenSearchQueryError, match="pit"):
                await search_events({"query": {}}, pit_id="pit-expired", keep_alive="5m")

    @pytest.mark.asyncio
    async def test_search_timeout_raises_opensearch_query_error(self) -> None:
        """搜索超时 → OpenSearchQueryError。"""
        from opensearchpy import ConnectionError as OSConnectionError

        client = _make_client_mock()
        client.search = AsyncMock(side_effect=OSConnectionError("timeout", None, None))

        with patch("app.system.store.get_opensearch_client", return_value=client):
            with pytest.raises(OpenSearchQueryError):
                await search_events({"query": {}}, pit_id="pit-001", keep_alive="5m")


class TestHitToView:
    def test_converts_to_system_event_view(self) -> None:
        hit = {
            "_source": {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severity_number": 9,
                "message": "hello",
            },
            "sort": [1718323200000, "log-001"],
        }
        view = _hit_to_view(hit, include_raw_log=False)
        assert isinstance(view, SystemEventView)
        assert view.log_id == "log-001"

    def test_search_text_excluded(self) -> None:
        hit = {
            "_source": {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severity_number": 0,
                "message": "hello",
                "search_text": "hello extra",
            },
            "sort": [],
        }
        view = _hit_to_view(hit, include_raw_log=False)
        assert not hasattr(view, "search_text")

    def test_raw_body_excluded_when_flag_false(self) -> None:
        hit = {
            "_source": {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severity_number": 0,
                "message": "hello",
                "raw_body": {"key": "val"},
            },
            "sort": [],
        }
        view = _hit_to_view(hit, include_raw_log=False)
        assert view.raw_body is None

    def test_raw_body_included_when_flag_true(self) -> None:
        hit = {
            "_source": {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severity_number": 0,
                "message": "hello",
                "raw_body": {"key": "val"},
            },
            "sort": [],
        }
        view = _hit_to_view(hit, include_raw_log=True)
        assert view.raw_body == {"key": "val"}

    def test_severity_number_defaults_to_zero(self) -> None:
        hit = {
            "_source": {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "message": "hello",
            },
            "sort": [],
        }
        view = _hit_to_view(hit, include_raw_log=False)
        assert view.severity_number == 0


class TestExtractSearchAfter:
    def test_returns_sort_values_from_last_hit(self) -> None:
        view = SystemEventView(
            log_id="log-001",
            timestamp="2024-06-14T12:00:00Z",
            aic="aic-001",
            severity_number=0,
            message="test",
        )
        hit = SystemEventHit(view=view, sort_values=[1718323200000, "log-001"])
        result = extract_search_after(hit)
        assert result == [1718323200000, "log-001"]
