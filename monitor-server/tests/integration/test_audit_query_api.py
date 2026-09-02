"""tests/integration/test_audit_query_api.py — Audit Query API 集成测试。

使用真实 PostgreSQL（agent_monitor_test）验证：
- records/query 带 timeRange 返回结果
- 无 timeRange 返回 400
- auditId 精确查询
- actor 过滤
- 不支持字段返回 422
- summary/aggregate 按 action_type 分组
- export 异步路径
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient

from app.core.amp_schema import LogRecord
from tests.integration.conftest import make_signed_log_record

# ─── 辅助：写入若干测试记录 ─────────────────────────────────────────────────────


async def _insert_record(
    writer: Any,
    priv: Any,
    kid: str,
    **kwargs: Any,
) -> dict:
    raw = make_signed_log_record(priv, kid=kid, **kwargs)
    record = LogRecord.model_validate(raw)
    await writer._process_audit_record(record, raw)
    return raw


_TIME_RANGE = {
    "startAt": "2026-01-01T00:00:00Z",
    "endAt": "2026-12-31T23:59:59Z",
}


class TestRecordsQuery:
    async def test_query_with_time_range_returns_200(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """带 timeRange 的查询应返回 200 及有效 meta.dataFreshnessAt。"""
        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert "meta" in body
        assert body["meta"]["dataFreshnessAt"] is not None

    async def test_query_without_time_range_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """缺少 timeRange 时应返回 400（AMP_INVALID_TIME_RANGE）。"""
        resp = await http_client_integration.post("/acps-amp-v1/audit/records/query", json={})
        assert resp.status_code == 400, resp.text

    async def test_query_returns_inserted_record(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """写入一条记录后查询，items 应包含该记录。"""
        writer, priv, kid = audit_writer_with_mock_keys
        log_id = str(uuid.uuid4())
        await _insert_record(writer, priv, kid, log_id=log_id, timestamp="2026-06-09T10:00:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        log_ids = [item["logId"] for item in items]
        assert log_id in log_ids, f"记录 {log_id} 不在查询结果中"

    async def test_query_with_unsupported_field_returns_422(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """使用不支持的过滤字段应返回 422（AMP_UNSUPPORTED_FIELD）。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={
                "timeRange": _TIME_RANGE,
                "filter": {"conditions": [{"field": "raw_log.secret", "op": "eq", "value": "x"}]},
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_query_actor_filter(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """按 actor_id 过滤应只返回匹配记录。"""
        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, actor_id="alice", timestamp="2026-06-09T10:01:00+00:00")
        await _insert_record(writer, priv, kid, actor_id="bob", timestamp="2026-06-09T10:02:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={
                "timeRange": _TIME_RANGE,
                "filter": {"conditions": [{"field": "body.actor.id", "op": "eq", "value": "alice"}]},
            },
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert all(item["body"]["actor"]["id"] == "alice" for item in items), "过滤后结果包含非 alice 的记录"
        assert len(items) >= 1


class TestSingleRecordGet:
    async def test_get_existing_record_returns_200(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/records/{auditId} 对已存在记录返回 200。"""
        writer, priv, kid = audit_writer_with_mock_keys
        raw = await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")
        log_id = raw["log_id"]

        # 先通过查询获取 auditId
        query_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        items = query_resp.json()["items"]
        record = next((i for i in items if i["logId"] == log_id), None)
        assert record is not None

        audit_id = record["auditId"]
        get_resp = await http_client_integration.get(f"/acps-amp-v1/audit/records/{audit_id}")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["auditId"] == audit_id

    async def test_get_nonexistent_record_returns_404(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/records/{auditId} 对不存在的 ID 返回 404。"""
        fake_id = str(uuid.uuid4())
        resp = await http_client_integration.get(f"/acps-amp-v1/audit/records/{fake_id}")
        assert resp.status_code == 404, resp.text

    async def test_get_record_response_includes_freshness_header(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/records/{auditId} 响应应包含 X-AMP-Data-Freshness-At 头。"""
        writer, priv, kid = audit_writer_with_mock_keys
        raw = await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        query_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        items = query_resp.json()["items"]
        record = next((i for i in items if i["logId"] == raw["log_id"]), None)
        assert record is not None

        get_resp = await http_client_integration.get(f"/acps-amp-v1/audit/records/{record['auditId']}")
        assert "x-amp-data-freshness-at" in get_resp.headers


class TestExportEndpoint:
    async def test_export_without_time_range_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/export 无 timeRange 应返回 400。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/export",
            json={"format": "ndjson"},
        )
        assert resp.status_code == 400, resp.text

    async def test_export_with_time_range_returns_202(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/export 带 timeRange 应一律返回 202 + taskId。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/export",
            json={
                "timeRange": _TIME_RANGE,
                "format": "ndjson",
            },
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "taskId" in body

    async def test_get_export_task_not_found_returns_404(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/export/{taskId} 对不存在任务返回 404。"""
        fake_id = str(uuid.uuid4())
        resp = await http_client_integration.get(f"/acps-amp-v1/audit/export/{fake_id}")
        assert resp.status_code == 404, resp.text

    async def test_get_export_task_returns_200(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """提交 export 后，GET /audit/export/{taskId} 返回 200。"""
        create_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/export",
            json={"timeRange": _TIME_RANGE, "format": "ndjson"},
        )
        assert create_resp.status_code == 202
        task_id = create_resp.json()["taskId"]

        get_resp = await http_client_integration.get(f"/acps-amp-v1/audit/export/{task_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["taskId"] == task_id


class TestAggregateEndpoint:
    async def test_aggregate_by_action_type(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/summary/aggregate 按 action_type 分组应返回正确分布。"""
        writer, priv, kid = audit_writer_with_mock_keys
        for i in range(3):
            await _insert_record(
                writer,
                priv,
                kid,
                action_type="auth",
                timestamp=f"2026-06-09T1{i}:00:00+00:00",
            )
        await _insert_record(writer, priv, kid, action_type="data_access", timestamp="2026-06-09T14:00:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/summary/aggregate",
            json={
                "timeRange": _TIME_RANGE,
                "groupBy": ["body.action.type"],
            },
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        group_counts = {item["groupKey"]["body.action.type"]: item["count"] for item in items}
        assert group_counts.get("auth", 0) >= 3
        assert group_counts.get("data_access", 0) >= 1


class TestIntegrityVerifyEndpoint:
    async def test_integrity_verify_not_found_returns_404(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/integrity/verify/{taskId} 对不存在任务返回 404。"""
        resp = await http_client_integration.get(f"/acps-amp-v1/audit/integrity/verify/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text

    async def test_integrity_verify_all_missing_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/integrity/verify 三者全缺 → 400 AMP_INVALID_FILTER。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/integrity/verify",
            json={},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("error_code") == "AMP_INVALID_FILTER"

    async def test_integrity_verify_filter_without_time_range_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/integrity/verify 带 filter 但无 timeRange → 400 AMP_INVALID_TIME_RANGE。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/integrity/verify",
            json={"filter": {"conditions": [{"field": "body.actor.id", "op": "eq", "value": "alice"}]}},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("error_code") == "AMP_INVALID_TIME_RANGE"

    async def test_integrity_verify_sync_with_record_ids_returns_200(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/integrity/verify 带 recordIds（小范围）→ 同步路径返回 200 + checkedCount。"""
        writer, priv, kid = audit_writer_with_mock_keys
        raw = await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        query_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        items = query_resp.json()["items"]
        record = next((i for i in items if i["logId"] == raw["log_id"]), None)
        assert record is not None
        audit_id = record["auditId"]

        verify_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/integrity/verify",
            json={"recordIds": [audit_id]},
        )
        assert verify_resp.status_code == 200, verify_resp.text
        body = verify_resp.json()
        assert "summary" in body, f"响应缺少 summary 字段: {body}"
        summary = body["summary"]
        assert "checkedCount" in summary
        assert summary["checkedCount"] == 1
        assert "failedCount" in summary

    async def test_integrity_verify_large_count_returns_202_async_task(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/integrity/verify 超过同步阈值的时间范围 → 202 异步任务。

        先写入 1 条记录，再将 audit_verify_sync_max_records 压低到 0，
        使 record_count=1 > threshold=0，触发异步路径。
        """
        from unittest.mock import patch

        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        with patch("app.audit.service.settings") as mock_settings:
            mock_settings.audit_verify_sync_max_records = 0  # 阈值为 0，DB 中有 1 条即触发异步
            mock_settings.audit_max_event_lag_hours = 48  # 整数，避免 MagicMock 传入 timedelta

            resp = await http_client_integration.post(
                "/acps-amp-v1/audit/integrity/verify",
                json={"timeRange": _TIME_RANGE},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "taskId" in body

    async def test_get_integrity_task_pending_returns_200(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/integrity/verify/{taskId} 对 pending 任务返回 200 + 正确 status。"""
        from unittest.mock import patch

        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        with patch("app.audit.service.settings") as mock_settings:
            mock_settings.audit_verify_sync_max_records = 0
            mock_settings.audit_max_event_lag_hours = 48  # 整数，避免 MagicMock 传入 timedelta

            create_resp = await http_client_integration.post(
                "/acps-amp-v1/audit/integrity/verify",
                json={"timeRange": _TIME_RANGE},
            )
        assert create_resp.status_code == 202
        task_id = create_resp.json()["taskId"]

        get_resp = await http_client_integration.get(f"/acps-amp-v1/audit/integrity/verify/{task_id}")
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["taskId"] == task_id
        assert body["status"] == "pending"


class TestAnchorLatestEndpoint:
    async def test_anchors_latest_returns_200_with_empty_items(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """GET /audit/anchors/latest 无锚点时返回 200 + 空 items 列表。"""
        resp = await http_client_integration.get("/acps-amp-v1/audit/anchors/latest")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert isinstance(body["items"], list)
        assert "meta" in body

    async def test_anchors_latest_with_unknown_chain_id_returns_empty_items(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """带不存在 chain_id 的 GET → 200 + 空列表（不是 404）。"""
        fake_chain_id = "chain-" + "0" * 59
        resp = await http_client_integration.get(f"/acps-amp-v1/audit/anchors/latest?chain_id={fake_chain_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["items"] == []


class TestRecordsQueryExtended:
    async def test_query_cursor_pagination(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """分页游标：第一页 limit=1，nextCursor 非空；第二页用游标查到第二条记录。"""
        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, timestamp="2026-06-09T11:00:00+00:00")
        raw2 = await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:00:00+00:00")

        # 第一页 limit=1（按 timestamp DESC，先拿到 raw1）
        page1_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE, "page": {"limit": 1}},
        )
        assert page1_resp.status_code == 200
        page1 = page1_resp.json()
        assert len(page1["items"]) == 1
        next_cursor = page1["meta"].get("nextCursor")
        assert next_cursor is not None, "limit=1 时 meta.nextCursor 应非空"

        # 第二页使用 cursor
        page2_resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE, "page": {"limit": 1, "cursor": next_cursor}},
        )
        assert page2_resp.status_code == 200
        page2_items = page2_resp.json()["items"]
        assert len(page2_items) == 1
        second_log_id = page2_items[0]["logId"]
        assert second_log_id == raw2["log_id"], f"第二页应返回 raw2，实际 {second_log_id}"

    async def test_query_invalid_cursor_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """非法 cursor（非 base64 或结构不对）应返回 400 AMP_CURSOR_INVALID。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE, "page": {"limit": 10, "cursor": "not-a-valid-cursor!!!"}},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("error_code") == "AMP_CURSOR_INVALID"

    async def test_query_keyword_exact_match_by_log_id(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """keyword 等于 log_id 时，精确命中对应记录（log_id 列使用 = 匹配）。"""
        writer, priv, kid = audit_writer_with_mock_keys
        log_id = str(uuid.uuid4())
        await _insert_record(writer, priv, kid, log_id=log_id, timestamp="2026-06-09T10:00:00+00:00")
        await _insert_record(writer, priv, kid, timestamp="2026-06-09T10:01:00+00:00")  # 噪音

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE, "keyword": log_id},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["logId"] == log_id

    async def test_query_keyword_prefix_matches_actor_id(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """keyword 前缀应 ILIKE 匹配 actor_id 等字段。"""
        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(writer, priv, kid, actor_id="prefixed-actor-001", timestamp="2026-06-09T10:00:00+00:00")
        await _insert_record(writer, priv, kid, actor_id="other-actor", timestamp="2026-06-09T10:01:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE, "keyword": "prefixed-actor"},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        actor_ids = [i["body"]["actor"]["id"] for i in items]
        assert any("prefixed-actor" in aid for aid in actor_ids)
        assert not any(aid == "other-actor" for aid in actor_ids), "other-actor 不应出现在结果中"

    async def test_query_filter_contains_op(
        self,
        audit_writer_with_mock_keys: tuple,
        http_client_integration: AsyncClient,
    ) -> None:
        """filter op=contains 应用 ILIKE %value% 语义。"""
        writer, priv, kid = audit_writer_with_mock_keys
        await _insert_record(
            writer, priv, kid, action_name="upload-file-resource", timestamp="2026-06-09T10:00:00+00:00"
        )
        await _insert_record(writer, priv, kid, action_name="delete", timestamp="2026-06-09T10:01:00+00:00")

        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/records/query",
            json={
                "timeRange": _TIME_RANGE,
                "filter": {"conditions": [{"field": "body.action.name", "op": "contains", "value": "file"}]},
            },
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        assert all("file" in i["body"]["action"]["name"] for i in items)

    async def test_aggregate_without_time_range_returns_400(
        self,
        http_client_integration: AsyncClient,
    ) -> None:
        """POST /audit/summary/aggregate 无 timeRange 应返回 400。"""
        resp = await http_client_integration.post(
            "/acps-amp-v1/audit/summary/aggregate",
            json={"groupBy": ["body.actor.id"]},
        )
        assert resp.status_code == 400, resp.text
