"""tests/unit/test_amp_schema.py — AMP LogRecord Pydantic schema 单元测试。"""

import pytest
from pydantic import ValidationError

from app.core.amp_schema import LogRecord, LogRecordIntegrity


class TestLogRecord:
    def test_full_record_parses_correctly(self) -> None:
        data = {
            "schema_version": "1.0",
            "log_id": "log-001",
            "log_type": "audit",
            "timestamp": "2026-06-01T12:00:00Z",
            "aic": "aic-xyz",
            "body": {"actorId": "alice", "actionName": "delete"},
            "integrity": {"alg": "EdDSA", "kid": "key-1", "sig": "abc123"},
            "trace_id": "trace-abc",
            "correlation_id": "corr-xyz",
        }
        record = LogRecord.model_validate(data)
        assert record.log_id == "log-001"
        assert record.log_type == "audit"
        assert record.trace_id == "trace-abc"
        assert record.integrity is not None
        assert record.integrity.alg == "EdDSA"

    def test_record_without_integrity_is_valid(self) -> None:
        data = {
            "schema_version": "1.0",
            "log_id": "log-002",
            "log_type": "heartbeat",
            "timestamp": "2026-06-01T12:00:00Z",
            "aic": "aic-abc",
            "body": {},
        }
        record = LogRecord.model_validate(data)
        assert record.integrity is None

    def test_timestamp_preserved_as_string(self) -> None:
        """timestamp 不能被 Pydantic 自动转为 datetime（链哈希依赖字节级一致性）。"""
        raw_ts = "2026-06-01T12:34:56.789+08:00"
        data = {
            "schema_version": "1.0",
            "log_id": "log-003",
            "log_type": "audit",
            "timestamp": raw_ts,
            "aic": "aic",
            "body": {},
        }
        record = LogRecord.model_validate(data)
        assert isinstance(record.timestamp, str)
        assert record.timestamp == raw_ts

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            LogRecord.model_validate({"log_id": "x", "log_type": "audit"})

    def test_body_is_dict(self) -> None:
        data = {
            "schema_version": "1.0",
            "log_id": "log-004",
            "log_type": "audit",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "aic",
            "body": {"key": "value", "nested": {"a": 1}},
        }
        record = LogRecord.model_validate(data)
        assert isinstance(record.body, dict)
        assert record.body["nested"]["a"] == 1

    def test_optional_fields_default_to_none(self) -> None:
        data = {
            "schema_version": "1.0",
            "log_id": "log-005",
            "log_type": "audit",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "aic",
            "body": {},
        }
        record = LogRecord.model_validate(data)
        assert record.trace_id is None
        assert record.correlation_id is None
        assert record.integrity is None


class TestLogRecordIntegrity:
    def test_integrity_fields_required(self) -> None:
        with pytest.raises(ValidationError):
            LogRecordIntegrity.model_validate({"alg": "EdDSA"})  # missing kid, sig

    def test_integrity_fields_parsed(self) -> None:
        integrity = LogRecordIntegrity.model_validate({"alg": "RS256", "kid": "key-rsa-1", "sig": "signature_value"})
        assert integrity.alg == "RS256"
        assert integrity.kid == "key-rsa-1"
