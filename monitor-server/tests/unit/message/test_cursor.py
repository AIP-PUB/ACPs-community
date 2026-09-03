"""单元测试：B-6 cursor.py — Keyset 游标编解码。"""

from __future__ import annotations

import base64
import json

import pytest

from app.message.cursor import (
    CursorState,
    decode_cursor,
    encode_cursor,
    query_fingerprint,
)
from app.message.exception import CursorInvalidError


def _make_fp(**kwargs: object) -> str:
    return query_fingerprint(
        api=kwargs.get("api", "events"),  # type: ignore[arg-type]
        time_range=kwargs.get("time_range"),
        filter_=kwargs.get("filter_"),
        sort=kwargs.get("sort"),
        extra=kwargs.get("extra", {}),  # type: ignore[arg-type]
    )


# ── encode / decode 往返 ───────────────────────────────────────────────────────


class TestEncodeDecode:
    def test_roundtrip_events_cursor(self) -> None:
        fp = _make_fp(api="events")
        encoded = encode_cursor(
            sort_values=["2026-06-01T00:00:00Z"],
            tiebreak={"timestamp": "2026-06-01T00:00:00Z", "log_id": "abc123"},
            fingerprint=fp,
        )
        decoded = decode_cursor(encoded, expected_fingerprint=fp)
        assert decoded is not None
        assert decoded.fingerprint == fp
        assert decoded.sort_values == ["2026-06-01T00:00:00Z"]
        assert decoded.tiebreak["log_id"] == "abc123"

    def test_roundtrip_lifecycle_cursor(self) -> None:
        fp = _make_fp(api="lifecycles")
        encoded = encode_cursor(
            sort_values=["2026-06-01T00:00:00Z"],
            tiebreak={"last_seen_at": "2026-06-01T00:00:00Z", "lifecycle_key": "mid:x"},
            fingerprint=fp,
        )
        decoded = decode_cursor(encoded, expected_fingerprint=fp)
        assert decoded is not None
        assert decoded.tiebreak["lifecycle_key"] == "mid:x"

    def test_none_returns_none(self) -> None:
        fp = _make_fp(api="events")
        result = decode_cursor(None, expected_fingerprint=fp)
        assert result is None

    def test_encode_produces_valid_base64(self) -> None:
        fp = _make_fp(api="events")
        encoded = encode_cursor(sort_values=[], tiebreak={}, fingerprint=fp)
        raw = base64.b64decode(encoded.encode())
        payload = json.loads(raw)
        assert "v" in payload
        assert "tb" in payload
        assert "fp" in payload


# ── 指纹不匹配 ──────────────────────────────────────────────────────────────


class TestFingerprintValidation:
    def test_fingerprint_mismatch_raises(self) -> None:
        fp1 = _make_fp(api="events")
        fp2 = _make_fp(api="lifecycles")
        encoded = encode_cursor(sort_values=[], tiebreak={}, fingerprint=fp1)
        with pytest.raises(CursorInvalidError):
            decode_cursor(encoded, expected_fingerprint=fp2)

    def test_corrupted_base64_raises(self) -> None:
        fp = _make_fp(api="events")
        with pytest.raises(CursorInvalidError):
            decode_cursor("not-valid-base64!!!", expected_fingerprint=fp)

    def test_corrupted_json_raises(self) -> None:
        fp = _make_fp(api="events")
        corrupted = base64.b64encode(b"not-json").decode()
        with pytest.raises(CursorInvalidError):
            decode_cursor(corrupted, expected_fingerprint=fp)

    def test_missing_fp_field_raises(self) -> None:
        fp = _make_fp(api="events")
        payload = json.dumps({"v": [], "tb": {}})
        encoded = base64.b64encode(payload.encode()).decode()
        with pytest.raises(CursorInvalidError):
            decode_cursor(encoded, expected_fingerprint=fp)


# ── query_fingerprint ─────────────────────────────────────────────────────────


class TestQueryFingerprint:
    def test_same_params_same_fingerprint(self) -> None:
        fp1 = _make_fp(api="events", extra={"x": 1})
        fp2 = _make_fp(api="events", extra={"x": 1})
        assert fp1 == fp2

    def test_different_api_different_fingerprint(self) -> None:
        fp1 = _make_fp(api="events")
        fp2 = _make_fp(api="lifecycles")
        assert fp1 != fp2

    def test_different_extra_different_fingerprint(self) -> None:
        fp1 = _make_fp(api="events", extra={"limit": 50})
        fp2 = _make_fp(api="events", extra={"limit": 100})
        assert fp1 != fp2

    def test_fingerprint_length(self) -> None:
        fp = _make_fp(api="events")
        assert len(fp) == 16

    def test_fingerprint_is_hex(self) -> None:
        fp = _make_fp(api="events")
        assert all(c in "0123456789abcdef" for c in fp)


# ── CursorState dataclass ─────────────────────────────────────────────────────


class TestCursorState:
    def test_frozen(self) -> None:
        state = CursorState(sort_values=[1], tiebreak={"k": "v"}, fingerprint="abc")
        with pytest.raises((AttributeError, TypeError)):
            state.fingerprint = "new"  # type: ignore[misc]

    def test_fields(self) -> None:
        state = CursorState(sort_values=[1, 2], tiebreak={"a": "b"}, fingerprint="fp")
        assert state.sort_values == [1, 2]
        assert state.tiebreak == {"a": "b"}
        assert state.fingerprint == "fp"
