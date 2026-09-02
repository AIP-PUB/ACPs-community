"""tests/unit/test_heartbeat_cursor.py — 多 shard 游标编解码单元测试。"""

from __future__ import annotations

import base64
import json

import pytest

from app.heartbeat.cursor import (
    LivenessQueryCursor,
    ShardPosition,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)
from app.heartbeat.exception import CursorInvalidError
from app.heartbeat.schema import HeartbeatLivenessQueryRequest


class TestFilterFingerprint:
    def test_same_request_same_fingerprint(self) -> None:
        req = HeartbeatLivenessQueryRequest()
        assert filter_fingerprint(req) == filter_fingerprint(req)

    def test_different_requests_different_fingerprints(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition

        req1 = HeartbeatLivenessQueryRequest()
        req2 = HeartbeatLivenessQueryRequest(
            filter=AMPFilter(conditions=[AMPFilterCondition(field="aic", op="eq", value="a")])
        )
        assert filter_fingerprint(req1) != filter_fingerprint(req2)

    def test_fingerprint_length(self) -> None:
        req = HeartbeatLivenessQueryRequest()
        fp = filter_fingerprint(req)
        assert len(fp) == 32  # SHA-256 前 16 字节 = 32 hex 字符


class TestCursorRoundtrip:
    def test_encode_decode_roundtrip(self) -> None:
        cursor = LivenessQueryCursor(
            positions={
                "hb-000": ShardPosition(last_seen_at_ms=1000000, last_aic="agent-001"),
                "hb-001": ShardPosition(last_seen_at_ms=2000000, last_aic="agent-002"),
            },
            fingerprint="abcd1234abcd1234abcd1234abcd1234",
        )
        encoded = encode_cursor(cursor)
        decoded = decode_cursor(encoded, "abcd1234abcd1234abcd1234abcd1234")
        assert decoded.positions["hb-000"].last_seen_at_ms == 1000000
        assert decoded.positions["hb-000"].last_aic == "agent-001"
        assert decoded.positions["hb-001"].last_aic == "agent-002"
        assert decoded.fingerprint == cursor.fingerprint

    def test_empty_positions_roundtrip(self) -> None:
        cursor = LivenessQueryCursor(positions={}, fingerprint="abcd1234abcd1234abcd1234abcd1234")
        encoded = encode_cursor(cursor)
        decoded = decode_cursor(encoded, "abcd1234abcd1234abcd1234abcd1234")
        assert decoded.positions == {}


class TestCursorDecodeErrors:
    def test_fingerprint_mismatch_raises(self) -> None:
        cursor = LivenessQueryCursor(
            positions={"hb-000": ShardPosition(last_seen_at_ms=1000, last_aic="a")},
            fingerprint="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        encoded = encode_cursor(cursor)
        with pytest.raises(CursorInvalidError):
            decode_cursor(encoded, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_corrupted_base64_raises(self) -> None:
        with pytest.raises(CursorInvalidError):
            decode_cursor("not-valid-base64!!!!", "any_fingerprint")

    def test_truncated_base64_raises(self) -> None:
        with pytest.raises(CursorInvalidError):
            decode_cursor("YQ==", "any_fingerprint")  # valid base64 but invalid JSON structure

    def test_missing_fingerprint_field_raises(self) -> None:
        payload: dict[str, object] = {"positions": {}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        with pytest.raises(CursorInvalidError):
            decode_cursor(encoded, "any_fingerprint")
