"""tests/e2e/test_audit_dedup_flow.py — Audit 幂等去重完整链路 E2E 测试。

验证：
- 同一 log_id 投递两次，DB 中只有一条记录
- chain_seq 未重复消耗（仍为 0）
- log_id 缺省时按 JCS 内容哈希兜底，相同内容两次投递只写入一条
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from acps_sdk.amp import compute_log_id_fallback
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.chain import compute_chain_id
from app.audit.model import AuditChainHead, AuditRecordIdentity
from tests.integration.conftest import make_log_record, make_signed_log_record
from tests.support.kafka_helper import produce_audit_event, wait_for_record_ingested, wait_for_watermark_advance

pytestmark = pytest.mark.e2e


class TestDeduplication:
    async def test_duplicate_log_id_results_in_single_db_row(
        self,
        e2e_writer: tuple[Any, Any, str],
        db_session_e2e: AsyncSession,
    ) -> None:
        """同一 log_id 投递两次，audit_record_identity 中只保留一条记录。"""
        _writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T20:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, timestamp=ts)

        # 第一次投递
        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        # 第二次投递相同 log_id
        await produce_audit_event(raw)
        # 额外等待，确保 writer 处理了第二条（如果会处理的话）
        import asyncio

        await asyncio.sleep(2)

        # 验证：只有一行
        db_session_e2e.expire_all()
        result = await db_session_e2e.execute(
            select(AuditRecordIdentity).where(
                AuditRecordIdentity.log_id == log_id  # type: ignore[arg-type]
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1, f"重复投递后有 {len(rows)} 行，期望 1"

    async def test_duplicate_log_id_does_not_increment_chain_seq(
        self,
        e2e_writer: tuple[Any, Any, str],
        db_session_e2e: AsyncSession,
    ) -> None:
        """同一 log_id 投递两次，chain_seq 只消耗一次（chain_head.last_chain_seq == 0）。"""
        writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T21:00:00+00:00"
        aic = f"aic-dedup-{uuid.uuid4()}"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, timestamp=ts, aic=aic)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )
        await produce_audit_event(raw)

        import asyncio

        await asyncio.sleep(2)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        db_session_e2e.expire_all()
        head = await db_session_e2e.scalar(
            select(AuditChainHead).where(
                AuditChainHead.chain_id == chain_id  # type: ignore[arg-type]
            )
        )
        assert head is not None
        assert head.last_chain_seq == 0, f"chain_seq 被重复消耗，last_chain_seq={head.last_chain_seq}，期望 0"


class TestLogIdFallback:
    async def test_fallback_log_id_creates_single_record(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: Any,
        db_session_e2e: AsyncSession,
    ) -> None:
        """log_id 缺省的消息：Writer 按 JCS 内容哈希生成 effective_log_id；
        相同内容两次投递后，audit_record_identity 中只有一条记录（幂等保证）。
        """
        _writer, _priv, _kid = e2e_writer

        # 构造不含 log_id 的 LogRecord（由 writer 按 §5.1.3 兜底计算 effective_log_id）
        raw = make_log_record(
            log_id=None,
            aic=f"aic-fallback-{uuid.uuid4()}",
            timestamp="2026-06-09T22:00:00+00:00",
        )
        raw.pop("log_id", None)  # 确保 key 不存在

        # 提前计算兜底 log_id，用于后续轮询
        expected_log_id = compute_log_id_fallback(raw)

        # 第一次投递
        await produce_audit_event(raw)
        await wait_for_record_ingested(db_session_e2e, expected_log_id, timeout=30)

        # 第二次投递相同内容
        await produce_audit_event(raw)
        import asyncio

        await asyncio.sleep(2)

        # 只有一行
        db_session_e2e.expire_all()
        result = await db_session_e2e.execute(
            select(AuditRecordIdentity).where(
                AuditRecordIdentity.log_id == expected_log_id  # type: ignore[arg-type]
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1, f"JCS fallback 幂等失败：有 {len(rows)} 行，期望 1"

    async def test_fallback_log_id_queryable_via_api(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: Any,
        db_session_e2e: AsyncSession,
    ) -> None:
        """log_id 缺省的消息通过 JCS 兜底后，可经 Query API 查询到（keyword 精确匹配 effective_log_id）。"""
        _writer, _priv, _kid = e2e_writer
        aic = f"aic-fallback-api-{uuid.uuid4()}"
        raw = make_log_record(
            log_id=None,
            aic=aic,
            timestamp="2026-06-09T22:30:00+00:00",
        )
        raw.pop("log_id", None)

        expected_log_id = compute_log_id_fallback(raw)

        await produce_audit_event(raw)
        await wait_for_record_ingested(db_session_e2e, expected_log_id, timeout=30)

        resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={
                "timeRange": {"startAt": "2026-01-01T00:00:00Z", "endAt": "2026-12-31T23:59:59Z"},
                "keyword": expected_log_id,
            },
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert any(i["logId"] == expected_log_id for i in items), f"Query API 未找到 fallback log_id={expected_log_id}"
