"""tests/e2e/test_audit_integrity_flow.py — Audit 签名与完整性 E2E 测试。

验证：
- 投递签名验证失败的 audit 事件 → 记录入库标记 signature_verified=false
- integrity/verify 端点对失败记录返回正确校验结果
- 合法事件 GET /audit/records/{auditId} 返回正确 chain_id 和 chain_seq
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.chain import compute_chain_id
from app.audit.model import AuditRecord, AuditRecordIdentity
from tests.integration.conftest import make_log_record, make_signed_log_record
from tests.support.kafka_helper import produce_audit_event, wait_for_watermark_advance

pytestmark = pytest.mark.e2e

_TIME_RANGE = {
    "startAt": "2026-01-01T00:00:00Z",
    "endAt": "2026-12-31T23:59:59Z",
}


class TestInvalidSignatureHandling:
    async def test_bad_signature_event_stored_with_false_flag(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """投递签名无效的 audit 事件 → 记录应入库且 signature_verified=false。"""
        _writer, _valid_priv, kid = e2e_writer
        # 使用不同私钥签名，但 kid 对应的公钥是 valid_priv 的公钥
        bad_priv = Ed25519PrivateKey.generate()
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T16:00:00+00:00"
        # make_signed_log_record 用 bad_priv 签，但 kid 对应 valid_priv 的公钥 → 验签失败
        raw = make_signed_log_record(bad_priv, kid=kid, log_id=log_id, timestamp=ts)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        db_session_e2e.expire_all()
        identity = await db_session_e2e.scalar(
            select(AuditRecordIdentity).where(
                AuditRecordIdentity.log_id == log_id  # type: ignore[arg-type]
            )
        )
        assert identity is not None, "验签失败的记录应仍然入库"

        audit_row = await db_session_e2e.scalar(
            select(AuditRecord).where(
                AuditRecord.audit_id == identity.audit_id  # type: ignore[arg-type]
            )
        )
        assert audit_row is not None
        assert audit_row.signature_verified is False
        assert audit_row.verification_failure_type == "signature"

    async def test_unsigned_event_stored_with_missing_key_mark(
        self,
        e2e_writer: tuple[Any, Any, str],
        db_session_e2e: AsyncSession,
    ) -> None:
        """无 integrity 字段的 audit 事件 → 入库标记 missing_public_key。"""
        _writer, _, _ = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T17:00:00+00:00"
        raw = make_log_record(log_id=log_id, timestamp=ts, integrity=None)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        db_session_e2e.expire_all()
        identity = await db_session_e2e.scalar(
            select(AuditRecordIdentity).where(
                AuditRecordIdentity.log_id == log_id  # type: ignore[arg-type]
            )
        )
        assert identity is not None

        audit_row = await db_session_e2e.scalar(
            select(AuditRecord).where(
                AuditRecord.audit_id == identity.audit_id  # type: ignore[arg-type]
            )
        )
        assert audit_row is not None
        assert audit_row.signature_verified is False
        assert audit_row.verification_failure_type == "missing_public_key"


class TestValidRecordFields:
    async def test_get_record_returns_chain_id_and_seq(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """合法事件写入后，GET /audit/records/{auditId} 返回正确的 chain_id 和 chain_seq。"""
        writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T18:00:00+00:00"
        aic = f"aic-e2e-{uuid.uuid4()}"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, aic=aic, timestamp=ts)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        query_resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        assert query_resp.status_code == 200
        items = query_resp.json()["items"]
        found = next((i for i in items if i["logId"] == log_id), None)
        assert found is not None

        audit_id = found["auditId"]
        get_resp = await e2e_http_client.get(f"/acps-amp-v1/audit/records/{audit_id}")
        assert get_resp.status_code == 200
        record = get_resp.json()

        expected_chain_id = compute_chain_id(aic, writer._logical_chain_count)
        assert record["chainId"] == expected_chain_id, f"chain_id={record['chainId']!r}，期望 {expected_chain_id!r}"
        assert record["chainSeq"] == 0, f"第一条记录 chain_seq={record['chainSeq']}，期望 0"

    async def test_integrity_verify_endpoint_returns_result(
        self,
        e2e_writer: tuple[Any, Any, str],
        e2e_http_client: AsyncClient,
        db_session_e2e: AsyncSession,
    ) -> None:
        """合法事件写入后，POST /audit/integrity/verify 对该 auditId 返回校验结果。"""
        _writer, priv, kid = e2e_writer
        log_id = str(uuid.uuid4())
        ts = "2026-06-09T19:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, timestamp=ts)

        await produce_audit_event(raw)
        await wait_for_watermark_advance(
            db_session_e2e,
            after=datetime.fromisoformat(ts),
            timeout=30,
        )

        # 获取 auditId
        query_resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/records/query",
            json={"timeRange": _TIME_RANGE},
        )
        items = query_resp.json()["items"]
        found = next((i for i in items if i["logId"] == log_id), None)
        assert found is not None
        audit_id = found["auditId"]

        # 提交完整性校验（同步小范围，单条 recordId 必走同步路径）
        verify_resp = await e2e_http_client.post(
            "/acps-amp-v1/audit/integrity/verify",
            json={"recordIds": [audit_id]},
        )
        assert verify_resp.status_code == 200, f"integrity/verify 返回 {verify_resp.status_code}: {verify_resp.text}"
        body = verify_resp.json()
        assert "summary" in body, f"响应缺少 summary 字段: {body}"
        summary = body["summary"]
        assert summary.get("checkedCount", 0) >= 1, "checkedCount 应 >= 1"
        assert "failedCount" in summary, "响应缺少 failedCount 字段"
