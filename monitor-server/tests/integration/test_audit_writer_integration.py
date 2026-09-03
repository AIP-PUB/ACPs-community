"""tests/integration/test_audit_writer_integration.py — Audit Writer 写入链路集成测试。

在真实 PostgreSQL（agent_monitor_test）上验证：
- 正常写入后四张表状态（identity / records / chain_head / watermark）
- 重复投递不创建新 chain_seq
- 验签失败入库标记
- watermark 推进独立于主事务
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.chain import compute_chain_id
from app.audit.model import AuditChainHead, AuditRecord, AuditRecordIdentity
from app.core.amp_schema import LogRecord
from tests.integration.conftest import make_log_record, make_signed_log_record


class TestNormalWrite:
    async def test_identity_row_created(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """正常写入后 audit_record_identity 应有对应行。"""
        writer, priv, kid = audit_writer_with_mock_keys
        raw = make_signed_log_record(priv, kid=kid, log_id=str(uuid.uuid4()))
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        # 用新 session 查询（writer 已提交）
        row = await db_session.scalar(
            select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == record.log_id)  # type: ignore[arg-type]
        )
        assert row is not None, "audit_record_identity 未创建行"
        assert str(row.log_id) == record.log_id

    async def test_audit_record_row_created_with_correct_fields(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """正常写入后 audit_records 应有行且字段正确。"""
        writer, priv, kid = audit_writer_with_mock_keys
        log_id = str(uuid.uuid4())
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id, actor_id="user-verify")
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        identity = await db_session.scalar(
            select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == log_id)  # type: ignore[arg-type]
        )
        assert identity is not None
        audit_row = await db_session.scalar(
            select(AuditRecord).where(AuditRecord.audit_id == identity.audit_id)  # type: ignore[arg-type]
        )
        assert audit_row is not None, "audit_records 未创建行"
        assert audit_row.signature_verified is True
        assert audit_row.chain_seq >= 0
        assert audit_row.current_hash != ""
        assert audit_row.actor_id == "user-verify"

    async def test_chain_head_last_chain_seq_updated(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """写入后 audit_chain_head 的 last_chain_seq 应从 -1 更新为 0。"""
        writer, priv, kid = audit_writer_with_mock_keys
        raw = make_signed_log_record(priv, kid=kid)
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        expected_chain_id = compute_chain_id(record.aic, writer._logical_chain_count)
        head = await db_session.scalar(
            select(AuditChainHead).where(AuditChainHead.chain_id == expected_chain_id)  # type: ignore[arg-type]
        )
        assert head is not None
        assert head.last_chain_seq == 0, f"last_chain_seq={head.last_chain_seq}，期望 0"
        assert head.last_current_hash is not None

    async def test_watermark_advanced_after_write(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """写入后 watermark 应推进到记录的 timestamp。"""
        writer, priv, kid = audit_writer_with_mock_keys
        ts = "2026-06-09T10:00:00+00:00"
        raw = make_signed_log_record(priv, kid=kid, timestamp=ts)
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        # 刷新 session 缓存
        db_session.expire_all()
        # 全局水位 = MIN(partition_watermark)；至少有一行 partition_key=0（默认分区）
        from sqlalchemy import text as sa_text

        min_wm = await db_session.scalar(
            sa_text("SELECT MIN(partition_watermark) FROM audit_read_model_watermark WHERE stream_name = 'amp.audit'")
        )
        assert min_wm is not None, "watermark 行不存在"
        expected_ts = datetime.fromisoformat(ts)
        assert min_wm >= expected_ts, f"global watermark={min_wm} 未推进到 {expected_ts}"


class TestIdempotency:
    async def test_duplicate_delivery_creates_only_one_identity_row(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """同一 log_id 两次投递，audit_record_identity 只保留一行。"""
        writer, priv, kid = audit_writer_with_mock_keys
        log_id = str(uuid.uuid4())
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id)
        record = LogRecord.model_validate(raw)

        await writer._process_audit_record(record, raw)
        await writer._process_audit_record(record, raw)  # 第二次

        result = await db_session.execute(
            select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == log_id)  # type: ignore[arg-type]
        )
        rows = result.scalars().all()
        assert len(rows) == 1, f"重复投递后有 {len(rows)} 行，期望 1"

    async def test_duplicate_delivery_does_not_increment_chain_seq(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """同一 log_id 两次投递，chain_seq 只消耗一次（head 仍为 0）。"""
        writer, priv, kid = audit_writer_with_mock_keys
        log_id = str(uuid.uuid4())
        raw = make_signed_log_record(priv, kid=kid, log_id=log_id)
        record = LogRecord.model_validate(raw)

        await writer._process_audit_record(record, raw)
        await writer._process_audit_record(record, raw)

        chain_id = compute_chain_id(record.aic, writer._logical_chain_count)
        head = await db_session.scalar(
            select(AuditChainHead).where(AuditChainHead.chain_id == chain_id)  # type: ignore[arg-type]
        )
        assert head is not None
        assert head.last_chain_seq == 0, f"chain_seq 被重复消耗：{head.last_chain_seq}"


class TestSignatureHandling:
    async def test_missing_integrity_stored_with_failure_mark(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """无 integrity 字段的记录应入库，但 signature_verified=False。"""
        writer, _priv, _kid = audit_writer_with_mock_keys
        raw = make_log_record(integrity=None)  # 不带签名
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        identity = await db_session.scalar(
            select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == record.log_id)  # type: ignore[arg-type]
        )
        assert identity is not None

        audit_row = await db_session.scalar(
            select(AuditRecord).where(AuditRecord.audit_id == identity.audit_id)  # type: ignore[arg-type]
        )
        assert audit_row is not None
        assert audit_row.signature_verified is False
        assert audit_row.verification_failure_type is not None

    async def test_bad_signature_stored_with_failure_mark(
        self,
        ed25519_keypair: tuple,
        db_session: AsyncSession,
    ) -> None:
        """签名无效的记录应入库，但 signature_verified=False。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from app.audit.key_resolver import MockKeyResolver
        from app.audit.writer import AuditWriter

        priv, _ = ed25519_keypair
        kid = "bad-sig-kid"
        # 使用正确公钥的 PEM，但签名将是错误的（另一个私钥生成的）
        pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        other_priv = Ed25519PrivateKey.generate()
        resolver = MockKeyResolver({kid: pub_pem})
        writer = AuditWriter(key_resolver=resolver)

        raw = make_signed_log_record(other_priv, kid=kid)  # 用不同私钥签名
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)

        identity = await db_session.scalar(
            select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == record.log_id)  # type: ignore[arg-type]
        )
        assert identity is not None, "签名失败时仍应入库"
        audit_row = await db_session.scalar(
            select(AuditRecord).where(AuditRecord.audit_id == identity.audit_id)  # type: ignore[arg-type]
        )
        assert audit_row is not None
        assert audit_row.signature_verified is False
        assert audit_row.verification_failure_type == "signature"


class TestMultipleWrites:
    async def test_chain_seq_increments_on_successive_writes(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """同一 AIC 连续写入 3 条，chain_seq 应为 0, 1, 2。"""
        writer, priv, kid = audit_writer_with_mock_keys
        aic = "aic-seq-test"
        raws = [
            make_signed_log_record(priv, kid=kid, aic=aic, timestamp=f"2026-06-09T1{i}:00:00+00:00") for i in range(3)
        ]
        for raw in raws:
            record = LogRecord.model_validate(raw)
            await writer._process_audit_record(record, raw)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        head = await db_session.scalar(
            select(AuditChainHead).where(AuditChainHead.chain_id == chain_id)  # type: ignore[arg-type]
        )
        assert head is not None
        assert head.last_chain_seq == 2, f"最终 chain_seq={head.last_chain_seq}，期望 2"
