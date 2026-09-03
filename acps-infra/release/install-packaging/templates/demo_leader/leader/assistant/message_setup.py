"""demo-leader Message Emitter 单例与请求级 trace 上下文。

复用 amp_setup.LEADER_AIC；消息日志写入 logs/amp_message.jsonl，由 Fluent Bit 转发到 amp.message。
Message 事件驱动（无周期任务），仅暴露 emitter 单例 + contextvar 形式的当前 trace 上下文。
"""

from __future__ import annotations

import contextvars
from pathlib import Path

from acps_sdk.amp import MessageEmitter
from acps_sdk.amp.trace_context import TraceContext, new_span_id, new_trace_id
from assistant.amp_setup import LEADER_AIC

_MESSAGE_LOG_FILE = Path(__file__).parent.parent.parent / "logs" / "amp_message.jsonl"
LEADER_MESSAGE_EMITTER = MessageEmitter(_MESSAGE_LOG_FILE, aic=LEADER_AIC)

_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "amp_message_trace",
    default=None,
)


def get_current_trace -> TraceContext | None:
    return _current_trace.get


def start_trace(trace_id: str | None = None) -> TraceContext:
 """编排入口调用：初始化请求级 trace_id，不发射消息日志。"""
    ctx = TraceContext(trace_id=trace_id or new_trace_id, span_id=new_span_id)
    _current_trace.set(ctx)
    return ctx


def set_current_trace(ctx: TraceContext | None) -> None:
    _current_trace.set(ctx)
