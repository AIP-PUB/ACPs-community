"""tests/unit/test_audit_writer.py — AuditWriter 单元测试（mock DB + mock KeyResolver）。"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jcs
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.hashes import SHA256

from app.audit.key_resolver import KeyNotFoundError, MockKeyResolver
from app.audit.writer import AuditWriter, _verify_signature


def _make_raw_log(
    log_id: str = "log-001",
    aic: str = "aic-alice",
    timestamp: str = "2026-06-01T12:00:00Z",
    actor_id: str = "user-alice",
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造符合 AMP Spec §5.6 嵌套结构的 audit LogRecord dict。"""
    body = {
        "actor": {"id": actor_id, "type": "user"},
        "action": {"name": "resource.delete", "type": "write"},
        "target": {"type": "resource", "id": "res-001"},
        "result": {"status": "success"},
    }
    raw: dict[str, Any] = {
        "schema_version": "1.0",
        "log_id": log_id,
        "log_type": "audit",
        "timestamp": timestamp,
        "aic": aic,
        "body": body,
    }
    if integrity:
        raw["integrity"] = integrity
    return raw


def _make_mock_message(raw_dict: dict[str, Any]) -> MagicMock:
    msg = MagicMock()
    msg.value = json.dumps(raw_dict).encode()
    msg.key = None
    msg.topic = "amp.audit"
    msg.partition = 0
    msg.offset = 0
    return msg


class TestAuditWriterInit:
    def test_init_with_default_key_resolver(self) -> None:
        writer = AuditWriter()
        assert writer._topic == "amp.audit"
        assert writer._dlq_topic == "amp.audit.dlq"

    def test_init_with_custom_key_resolver(self) -> None:
        resolver = MockKeyResolver({})
        writer = AuditWriter(key_resolver=resolver)
        assert writer._key_resolver is resolver


class TestHandleMessageIdempotency:
    @pytest.mark.asyncio
    async def test_skips_duplicate_log_id(self) -> None:
        """已存在 log_id 时，handle_message 不应尝试写入任何新行。"""
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log()
        msg = _make_mock_message(raw)

        # 模拟 session：scalar 返回已存在的 identity
        existing_identity = MagicMock()
        existing_identity.audit_id = uuid.uuid4()
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(return_value=existing_identity)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("app.audit.writer.async_session_factory", return_value=mock_session):
            await writer.handle_message(msg)

        # 验证：session.add 从未被调用（因为幂等跳过）
        mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_audit_log_type(self) -> None:
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log()
        raw["log_type"] = "heartbeat"
        msg = _make_mock_message(raw)

        mock_session = AsyncMock()
        with patch("app.audit.writer.async_session_factory", return_value=mock_session):
            await writer.handle_message(msg)

        # heartbeat 消息不触发 DB 查询
        mock_session.scalar.assert_not_called()


class TestSignatureVerification:
    @pytest.mark.asyncio
    async def test_missing_integrity_marks_failure(self) -> None:
        """无 integrity 字段时，failure_type 应为 missing_public_key。"""
        from datetime import UTC, datetime

        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log()  # no integrity
        msg = _make_mock_message(raw)

        added_rows: list[Any] = []

        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = -1
        mock_chain_head.last_current_hash = None

        mock_clock = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, mock_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch("app.audit.writer.AuditWriter._advance_watermark", new=AsyncMock()),
        ):
            await writer.handle_message(msg)

        # 应有两行被 add（identity + audit_record）
        assert len(added_rows) == 2
        audit_row = added_rows[1]
        assert audit_row.signature_verified is False
        assert audit_row.verification_failure_type == "missing_public_key"

    @pytest.mark.asyncio
    async def test_key_not_found_marks_missing_public_key(self) -> None:
        from datetime import UTC, datetime

        mock_resolver = AsyncMock()
        mock_resolver.resolve = AsyncMock(side_effect=KeyNotFoundError("kid-xxx"))

        writer = AuditWriter(key_resolver=mock_resolver)
        Ed25519PrivateKey.generate()
        raw = _make_raw_log(integrity={"alg": "EdDSA", "kid": "kid-xxx", "sig": "fake"})
        msg = _make_mock_message(raw)

        added_rows: list[Any] = []
        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = 0
        mock_chain_head.last_current_hash = "prev-hash"

        mock_clock = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, mock_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch("app.audit.writer.AuditWriter._advance_watermark", new=AsyncMock()),
        ):
            await writer.handle_message(msg)

        audit_row = added_rows[1]
        assert audit_row.verification_failure_type == "missing_public_key"
        assert audit_row.signature_verified is False


class TestChainSeqAllocation:
    @pytest.mark.asyncio
    async def test_chain_seq_increments_from_last_seq(self) -> None:
        """chain_seq = last_chain_seq + 1。"""
        from datetime import UTC, datetime

        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log()
        msg = _make_mock_message(raw)

        added_rows: list[Any] = []
        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = 4
        mock_chain_head.last_current_hash = "h" * 64

        mock_clock = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, mock_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch("app.audit.writer.AuditWriter._advance_watermark", new=AsyncMock()),
        ):
            await writer.handle_message(msg)

        audit_row = added_rows[1]
        assert audit_row.chain_seq == 5


class TestVerifySignature:
    """_verify_signature 独立单元测试（不涉及 DB）。"""

    def _make_signable(self, raw: dict[str, Any]) -> bytes:
        result: bytes = jcs.canonicalize({k: v for k, v in raw.items() if k != "integrity"})
        return result

    def test_eddsa_valid_signature_does_not_raise(self) -> None:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        raw = _make_raw_log()
        canonical = self._make_signable(raw)
        sig = base64.urlsafe_b64encode(priv.sign(canonical)).decode().rstrip("=")
        _verify_signature(pub, "EdDSA", raw, sig)  # 不应抛异常

    def test_eddsa_wrong_signature_raises_invalid_signature(self) -> None:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        raw = _make_raw_log()
        bad_sig = base64.urlsafe_b64encode(b"\x00" * 64).decode().rstrip("=")
        with pytest.raises(InvalidSignature):
            _verify_signature(pub, "EdDSA", raw, bad_sig)

    def test_eddsa_tampered_payload_raises_invalid_signature(self) -> None:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        raw = _make_raw_log()
        canonical = self._make_signable(raw)
        sig = base64.urlsafe_b64encode(priv.sign(canonical)).decode().rstrip("=")
        tampered = dict(raw)
        tampered["aic"] = "evil-aic"
        with pytest.raises(InvalidSignature):
            _verify_signature(pub, "EdDSA", tampered, sig)

    def test_rs256_valid_signature_does_not_raise(self) -> None:
        rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
        rsa_pub = rsa_priv.public_key()
        raw = _make_raw_log()
        canonical = self._make_signable(raw)
        sig = base64.urlsafe_b64encode(rsa_priv.sign(canonical, PKCS1v15(), SHA256())).decode().rstrip("=")
        _verify_signature(rsa_pub, "RS256", raw, sig)

    def test_rs256_wrong_signature_raises_invalid_signature(self) -> None:
        rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
        rsa_pub = rsa_priv.public_key()
        raw = _make_raw_log()
        bad_sig = base64.urlsafe_b64encode(b"\x00" * 256).decode().rstrip("=")
        with pytest.raises(InvalidSignature):
            _verify_signature(rsa_pub, "RS256", raw, bad_sig)

    def test_unsupported_algorithm_raises_value_error(self) -> None:
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        raw = _make_raw_log()
        with pytest.raises(ValueError, match="不支持的签名算法"):
            _verify_signature(pub, "HS256", raw, "fake-sig")

    def test_algorithm_key_type_mismatch_raises_value_error(self) -> None:
        rsa_priv = generate_private_key(public_exponent=65537, key_size=2048)
        rsa_pub = rsa_priv.public_key()
        raw = _make_raw_log()
        with pytest.raises(ValueError, match="不支持的签名算法"):
            _verify_signature(rsa_pub, "EdDSA", raw, "fake-sig")


class TestWatermarkAdvance:
    @pytest.mark.asyncio
    async def test_watermark_advances_independently(self) -> None:
        """_advance_watermark 必须在主事务提交后独立调用。"""
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log(timestamp="2026-06-01T12:00:00Z")
        msg = _make_mock_message(raw)

        added_rows: list[Any] = []
        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = -1
        mock_chain_head.last_current_hash = None

        from datetime import UTC, datetime

        mock_clock = datetime(2026, 6, 1, 12, 0, 1, tzinfo=UTC)
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, mock_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        advance_called = []

        async def mock_advance(self: Any, ts: str, **kwargs: Any) -> None:
            advance_called.append(ts)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch.object(AuditWriter, "_advance_watermark", mock_advance),
        ):
            await writer.handle_message(msg)

        assert len(advance_called) == 1
        assert advance_called[0] == "2026-06-01T12:00:00Z"

    @pytest.mark.asyncio
    async def test_duplicate_path_also_advances_watermark(self) -> None:
        """幂等跳过路径（log_id 已存在）同样必须推进水位（§3.1 step 8 注释）。"""
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log(timestamp="2026-06-01T10:00:00Z")
        msg = _make_mock_message(raw)

        existing_identity = MagicMock()
        existing_identity.audit_id = uuid.uuid4()

        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(return_value=existing_identity)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        advance_called = []

        async def mock_advance(self: Any, ts: str, **kwargs: Any) -> None:
            advance_called.append(ts)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch.object(AuditWriter, "_advance_watermark", mock_advance),
        ):
            await writer.handle_message(msg)

        assert len(advance_called) == 1
        assert advance_called[0] == "2026-06-01T10:00:00Z"


class TestCommittedAtFromDBClock:
    """committed_at 必须来自 DB clock_timestamp()（§4.3）。"""

    @pytest.mark.asyncio
    async def test_identity_row_committed_at_matches_db_clock(self) -> None:
        """AuditRecordIdentity 的 committed_at 应与 DB clock_timestamp() 一致。"""
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log()
        msg = _make_mock_message(raw)

        from datetime import UTC, datetime

        db_clock = datetime(2026, 6, 1, 15, 30, 0, tzinfo=UTC)

        added_rows: list[Any] = []
        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = -1
        mock_chain_head.last_current_hash = None

        # scalar 调用顺序：1=幂等检查(None), 2=chain_head, 3=clock_timestamp
        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, db_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch("app.audit.writer.AuditWriter._advance_watermark", new=AsyncMock()),
        ):
            await writer.handle_message(msg)

        assert len(added_rows) == 2
        identity_row = added_rows[0]
        audit_row = added_rows[1]
        # 两表 committed_at 应相同，且等于 DB 返回的 clock_timestamp()
        assert identity_row.committed_at == db_clock
        assert audit_row.committed_at == db_clock

    @pytest.mark.asyncio
    async def test_identity_and_audit_committed_at_are_identical(self) -> None:
        """audit_record_identity.committed_at 与 audit_records.committed_at 必须相同（§4.3）。"""
        writer = AuditWriter(key_resolver=MockKeyResolver({}))
        raw = _make_raw_log(log_id="log-sync-001")
        msg = _make_mock_message(raw)

        from datetime import UTC, datetime

        db_clock = datetime(2026, 6, 2, 9, 0, 0, 123456, tzinfo=UTC)

        added_rows: list[Any] = []
        mock_chain_head = MagicMock()
        mock_chain_head.last_chain_seq = 0
        mock_chain_head.last_current_hash = "prev-hash"

        mock_session = AsyncMock()
        mock_session.scalar = AsyncMock(side_effect=[None, mock_chain_head, db_clock])
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_rows.append)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.audit.writer.async_session_factory", return_value=mock_session),
            patch("app.audit.writer.AuditWriter._advance_watermark", new=AsyncMock()),
        ):
            await writer.handle_message(msg)

        assert len(added_rows) == 2
        assert added_rows[0].committed_at is added_rows[1].committed_at
