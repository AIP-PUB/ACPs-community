"""W3C Trace Context（traceparent）最小工具：生成 trace_id/span_id 与 header 编解码。"""

from __future__ import annotations

import os
from dataclasses import dataclass

TRACEPARENT_HEADER = "traceparent"


def new_trace_id() -> str:
    """生成 32 位十六进制 trace_id（16 字节）。"""
    return os.urandom(16).hex()


def new_span_id() -> str:
    """生成 16 位十六进制 span_id（8 字节）。"""
    return os.urandom(8).hex()


@dataclass(frozen=True)
class TraceContext:
    """当前 trace 上下文（W3C traceparent 语义）。"""

    trace_id: str
    span_id: str
    sampled: bool = True


def format_traceparent(ctx: TraceContext) -> str:
    """编码为 W3C traceparent header 值。"""
    flags = "01" if ctx.sampled else "00"
    return f"00-{ctx.trace_id}-{ctx.span_id}-{flags}"


def parse_traceparent(header: str | None) -> TraceContext | None:
    """解析 traceparent header；格式非法或缺失时返回 None。"""
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id, span_id, flags = parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(span_id) != 16 or len(flags) != 2:
        return None
    try:
        int(trace_id, 16)
        int(span_id, 16)
        int(flags, 16)
    except ValueError:
        return None
    return TraceContext(trace_id=trace_id, span_id=span_id, sampled=flags != "00")
