"""tests/e2e/test_audit_ingest_flow.py — Audit 摄取完整链路 E2E 测试。

通过真实 Kafka（Redpanda）投递事件，等待 Writer 写入 PostgreSQL，再通过 Query API 验证结果。

前置条件：
- `just infra up kafka` 已启动 Redpanda（localhost:19092）
- `just test bootstrap` 已完成 DB schema 迁移
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import make_signed_log_record
from tests.support.kafka_helper import produce_audit_event, wait_for_record_ingested, wait_for_watermark_advance

pytestmark = pytest.mark.e2e

_TIME_RANGE = {
    "startAt": "2026-01-01T00:00:00Z",
    "endAt": "2026-12-31T23:59:59Z",
}


class TestSingleRecordIngest:
    async def test_audit_event_appears_in_query_api(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """投递一条合法 audit 事件 → 等待 watermark 推进 → Query API 查到记录（含签名验证字段）。"""
        _writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T12:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, timestamp=ts)

        # 1. 投递到 Kafka
        await produce_audit_event(raw)

        # 2. 等待 Writer 写入，watermark 推进到 ts
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        # 3. 通过 Query API 查询
        resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        found = next((i for i in items if i["logId"] == log_id), None)
        assert found is not None, f"记录 {log_id} 未出现在查询结果中"

    async def test_ingest_event_has_correct_signature_fields(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """写入合法事件后，Query API 返回的 integrity 字段应显示 signatureVerified=true。"""
        _writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T13:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, timestamp=ts)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        found = next((i for i in items if i["logId"] == log_id), None)
        assert found is not None
        assert found["integrity"]["signatureVerified"] is True


class TestMultipleRecordsAggregate:
    async def test_aggregate_returns_correct_actor_distribution(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """投递 6 条事件（3 种 actor）→ aggregate 统计按 actor 分组后数量正确。"""
        _writer, priv, kid = e2e_writer
        actor_ids = ["alice", "alice", "bob", "bob", "carol", "alice"]
        last_log_id = ""
        for i, actor_id in enumerate(actor_ids):
            ts = f"2026-06-09T1{i + 4}:00:00+00:00"
            log_id = str(uuid.uuid4())
            raw = make_signed_log_record(
                priv,
                kid=kid,
                log_id=log_id,
                actor_id=actor_id,
                timestamp=ts,
            )
            await produce_audit_event(raw)
            last_log_id = log_id

        # 等待最后一条记录入库（不依赖全局 MIN 水位，用 log_id 轮询 identity 表）
        await wait_for_record_ingested(db_session_e2e, last_log_id, timeout=30)

        resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/summary/aggregate",
            json={
                "timeRange": _TIME_RANGE,
                "groupBy": ["body.actor.id"],
            },
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        count_by_actor = {item["groupKey"]["body.actor.id"]: item["count"] for item in items}
        assert count_by_actor.get("alice", 0) >= 3
        assert count_by_actor.get("bob", 0) >= 2
        assert count_by_actor.get("carol", 0) >= 1

    async def test_meta_data_freshness_reflects_latest_write(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """写入事件后，records/query 返回的 meta.dataFreshnessAt 应反映最新写入时间。"""
        _writer, priv, kid = e2e_writer
        ts = "2026-06-09T15:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, timestamp=ts)
        await produce_audit_event(raw)

        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert resp.status_code == 200
        freshness_at = resp.json()["meta"]["dataFreshnessAt"]
        assert freshness_at is not None
        # dataFreshnessAt 应 >= 写入的 ts
        freshness_dt = datetime.fromisoformat(freshness_at)
        expected_dt = datetime.fromisoformat(ts)
        assert freshness_dt >= expected_dt, f"dataFreshnessAt={freshness_dt} 早于写入 ts={expected_dt}"
