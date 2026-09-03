"""tests/amp/test_trace_context.py — W3C traceparent 工具测试。"""

from __future__ import annotations

import re

from acps_sdk.amp.trace_context import (
    TraceContext,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)


def test_new_trace_id_is_32_hex() -> None:
    trace_id = new_trace_id()
    assert len(trace_id) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)


def test_new_span_id_is_16_hex() -> None:
    span_id = new_span_id()
    assert len(span_id) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", span_id)


def test_format_and_parse_roundtrip() -> None:
    ctx = TraceContext(trace_id=new_trace_id(), span_id=new_span_id(), sampled=True)
    header = format_traceparent(ctx)
    parsed = parse_traceparent(header)
    assert parsed == ctx


def test_parse_invalid_or_missing_returns_none() -> None:
    assert parse_traceparent(None) is None
    assert parse_traceparent("") is None
    assert parse_traceparent("bad-header") is None
    assert parse_traceparent("00-abc-def-01") is None
