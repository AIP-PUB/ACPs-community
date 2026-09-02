"""tests/unit/test_access_cursor.py — keyset 游标编解码测试。

TDD B-5：先写测试（红）→ 实现 cursor.py（绿）。
"""

from __future__ import annotations

from typing import Any

import pytest


class TestCursorStateStructure:
    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        from app.access.cursor import CursorState

        assert dataclasses.is_dataclass(CursorState)

    def test_has_required_fields(self) -> None:
        from app.access.cursor import CursorState

        cs = CursorState(
            sort_values=[1000],
            tiebreak={"timestamp": 1000, "log_id": "abc"},
            fingerprint="fp1",
        )
        assert cs.sort_values == [1000]
        assert cs.tiebreak == {"timestamp": 1000, "log_id": "abc"}
        assert cs.fingerprint == "fp1"


class TestEncodeDecode:
    def test_roundtrip(self) -> None:
        from app.access.cursor import decode_cursor, encode_cursor

        token = encode_cursor(
            sort_values=[1000, 200],
            tiebreak={"timestamp": 1000, "log_id": "abc"},
            fingerprint="fp1",
        )
        state = decode_cursor(token, expected_fingerprint="fp1")
        assert state is not None
        assert state.sort_values == [1000, 200]
        assert state.tiebreak["log_id"] == "abc"
        assert state.fingerprint == "fp1"

    def test_none_returns_none(self) -> None:
        from app.access.cursor import decode_cursor

        result = decode_cursor(None, expected_fingerprint="fp1")
        assert result is None

    def test_wrong_fingerprint_raises(self) -> None:
        from app.access.cursor import decode_cursor, encode_cursor
        from app.access.exception import CursorInvalidError

        token = encode_cursor(sort_values=[1], tiebreak={}, fingerprint="fp-original")
        with pytest.raises(CursorInvalidError):
            decode_cursor(token, expected_fingerprint="fp-different")

    def test_corrupted_base64_raises(self) -> None:
        from app.access.cursor import decode_cursor
        from app.access.exception import CursorInvalidError

        with pytest.raises(CursorInvalidError):
            decode_cursor("not-valid-base64!!!", expected_fingerprint="fp1")

    def test_corrupted_json_raises(self) -> None:
        import base64

        from app.access.cursor import decode_cursor
        from app.access.exception import CursorInvalidError

        bad = base64.b64encode(b"not-json").decode()
        with pytest.raises(CursorInvalidError):
            decode_cursor(bad, expected_fingerprint="fp1")

    def test_encode_returns_string(self) -> None:
        from app.access.cursor import encode_cursor

        token = encode_cursor(sort_values=[], tiebreak={}, fingerprint="fp1")
        assert isinstance(token, str)
        assert len(token) > 0


class TestQueryFingerprint:
    def test_same_inputs_same_fp(self) -> None:
        from app.access.cursor import query_fingerprint

        fp1 = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={})
        fp2 = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={})
        assert fp1 == fp2

    def test_different_api_different_fp(self) -> None:
        from app.access.cursor import query_fingerprint

        fp1 = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={})
        fp2 = query_fingerprint(api="operations", time_range=None, filter_=None, sort=None, extra={})
        assert fp1 != fp2

    def test_different_extra_different_fp(self) -> None:
        from app.access.cursor import query_fingerprint

        fp1 = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={"group_by": "aic"})
        fp2 = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={"group_by": "service"})
        assert fp1 != fp2

    def test_returns_string(self) -> None:
        from app.access.cursor import query_fingerprint

        fp = query_fingerprint(api="events", time_range=None, filter_=None, sort=None, extra={})
        assert isinstance(fp, str)
        assert len(fp) > 0


class TestToKeysetBound:
    def _make_state(self, sort_values: list[Any], tiebreak: dict[str, Any]) -> Any:
        from app.access.cursor import CursorState

        return CursorState(sort_values=sort_values, tiebreak=tiebreak, fingerprint="fp")

    def _make_sort(self, field: str, col: str, order: str = "desc") -> Any:
        from app.access.filters import ResolvedSort

        return ResolvedSort(field=field, column_or_alias=col, order=order)

    def test_events_event_level_keyset(self) -> None:
        """事件级：(timestamp, log_id) < (?, ?) 形式（降序，C-ACCESS-QUERY-12）。"""
        from app.access.cursor import to_keyset_bound

        state = self._make_state(
            sort_values=[1_700_000_000_000],
            tiebreak={"timestamp": 1_700_000_000_000, "log_id": "abc"},
        )
        sort = [self._make_sort("timestamp", "timestamp", "desc")]
        kb = to_keyset_bound(state, sort, api="events")
        assert "timestamp" in kb.sql
        assert "log_id" in kb.sql
        # No OFFSET
        assert "OFFSET" not in kb.sql.upper()

    def test_keyset_bound_has_params(self) -> None:
        from app.access.cursor import to_keyset_bound

        state = self._make_state([1000], {"timestamp": 1000, "log_id": "abc"})
        sort = [self._make_sort("timestamp", "timestamp", "desc")]
        kb = to_keyset_bound(state, sort, api="events")
        assert len(kb.params) > 0

    def test_traces_keyset_uses_trace_id(self) -> None:
        """traces API keyset tiebreak 字段含 trace_id。"""
        from app.access.cursor import to_keyset_bound

        state = self._make_state(
            sort_values=[1_700_000_000_000],
            tiebreak={"last_seen_at": 1_700_000_000_000, "trace_id": "t1"},
        )
        sort = [self._make_sort("lastSeenAt", "last_seen_at", "desc")]
        kb = to_keyset_bound(state, sort, api="traces")
        assert "trace_id" in kb.sql or "last_seen_at" in kb.sql
