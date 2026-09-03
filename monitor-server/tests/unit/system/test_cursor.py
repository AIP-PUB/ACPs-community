"""tests/unit/system/test_cursor.py — cursor.py 单元测试。"""

from __future__ import annotations

import pytest

from app.system.cursor import (
    SystemCursorState,
    decode_cursor,
    encode_cursor,
    query_fingerprint,
)
from app.system.exception import CursorInvalidError


class TestEncodeDecodeCursor:
    """编解码往返无损。"""

    def _fp(self) -> str:
        return query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")

    def test_roundtrip_preserves_pit_id(self) -> None:
        fp = self._fp()
        token = encode_cursor(pit_id="pit-abc123", search_after=[1718323200000, "log-001"], fingerprint=fp)
        state = decode_cursor(token, expected_fingerprint=fp)
        assert state is not None
        assert state.pit_id == "pit-abc123"

    def test_roundtrip_preserves_search_after(self) -> None:
        fp = self._fp()
        token = encode_cursor(pit_id="pit-xyz", search_after=[9999999999000, "log-zz"], fingerprint=fp)
        state = decode_cursor(token, expected_fingerprint=fp)
        assert state is not None
        assert state.search_after == [9999999999000, "log-zz"]

    def test_roundtrip_preserves_fingerprint(self) -> None:
        fp = self._fp()
        token = encode_cursor(pit_id="pit-1", search_after=[1000, "x"], fingerprint=fp)
        state = decode_cursor(token, expected_fingerprint=fp)
        assert state is not None
        assert state.fingerprint == fp

    def test_none_cursor_returns_none(self) -> None:
        """首页无游标 → decode 返回 None。"""
        result = decode_cursor(None, expected_fingerprint="any")
        assert result is None

    def test_state_is_frozen_dataclass(self) -> None:
        fp = self._fp()
        token = encode_cursor(pit_id="p", search_after=[1, "a"], fingerprint=fp)
        state = decode_cursor(token, expected_fingerprint=fp)
        assert state is not None
        assert isinstance(state, SystemCursorState)
        with pytest.raises((AttributeError, TypeError)):
            state.pit_id = "mutate"  # type: ignore[misc]


class TestFingerprintMismatch:
    """指纹漂移检测。"""

    def _make_token(self, fp: str) -> str:
        return encode_cursor(pit_id="p", search_after=[1, "a"], fingerprint=fp)

    def test_different_fingerprint_raises_cursor_invalid(self) -> None:
        fp1 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")
        fp2 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="warn")
        token = self._make_token(fp1)
        with pytest.raises(CursorInvalidError):
            decode_cursor(token, expected_fingerprint=fp2)

    def test_corrupted_base64_raises_cursor_invalid(self) -> None:
        with pytest.raises(CursorInvalidError):
            decode_cursor("not-valid-base64!!!", expected_fingerprint="any")

    def test_empty_string_raises_cursor_invalid(self) -> None:
        with pytest.raises(CursorInvalidError):
            decode_cursor("", expected_fingerprint="any")


class TestQueryFingerprint:
    """query_fingerprint 随参数变化而变化（防止换参数复用游标）。"""

    def test_keyword_change_changes_fingerprint(self) -> None:
        fp1 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")
        fp2 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="warn")
        assert fp1 != fp2

    def test_none_keyword_vs_keyword(self) -> None:
        fp1 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword=None)
        fp2 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")
        assert fp1 != fp2

    def test_same_params_same_fingerprint(self) -> None:
        fp1 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")
        fp2 = query_fingerprint(time_range=None, filter_=None, sort=None, keyword="error")
        assert fp1 == fp2

    def test_fingerprint_is_16_hex_chars(self) -> None:
        fp = query_fingerprint(time_range=None, filter_=None, sort=None, keyword=None)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_sort_change_changes_fingerprint(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        sort1 = [AMPSortSpec(field="timestamp", order="desc")]
        sort2 = [AMPSortSpec(field="timestamp", order="asc")]
        fp1 = query_fingerprint(time_range=None, filter_=None, sort=sort1, keyword=None)
        fp2 = query_fingerprint(time_range=None, filter_=None, sort=sort2, keyword=None)
        assert fp1 != fp2
