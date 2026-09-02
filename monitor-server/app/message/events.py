"""app/message/events.py — MessageBody/LogRecord → message_events 行映射（纯函数）。

实现设计 §3.1 第 2~3 步、§3.1.1、C-MESSAGE-MODEL-6。
EventRow 字段顺序与 tables.INSERT_COLUMNS 严格一致（as_tuple() 按此顺序输出）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from app.message.exception import InvalidMessageRecordError

# event_type → direction 映射（设计 §3.1.1：send→send，其余→receive）
_SEND_EVENTS: Final = frozenset({"send"})

# 含敏感信息的 key 正则（raw_log 脱敏用）
_SENSITIVE_KEY_RE: Final = re.compile(r"(password|token|secret|key)", re.IGNORECASE)


@dataclass(frozen=True)
class EventRow:
    """message_events 写入行（与 tables.INSERT_COLUMNS 列序一一对应）。

    store.insert_events 直接调 as_tuple() 按 INSERT_COLUMNS 位置映射落库。
    """

    log_id: str
    timestamp_ms: int
    observed_at_ms: int
    aic: str
    trace_id: str
    correlation_id: str
    direction: str
    event_type: str
    system: str
    destination_name: str
    destination_kind: str
    virtual_host: str
    subscription_name: str
    consumer_group_name: str
    routing_key: str
    partition: str | None
    offset: int | None
    message_id: str
    lifecycle_key: str
    payload_size_bytes: int
    delivery_attempt: int | None
    settlement_latency_ms: int | None
    settlement_reason: str
    error_code: str
    error_message: str
    attributes: dict[str, str]
    raw_log: str

    def as_tuple(self) -> tuple[object, ...]:
        """按 tables.INSERT_COLUMNS 顺序返回写入元组。"""
        from datetime import datetime

        def _ms_to_dt(ms: int) -> datetime:
            return datetime.fromtimestamp(ms / 1000.0, tz=UTC)

        return (
            self.log_id,
            _ms_to_dt(self.timestamp_ms),
            _ms_to_dt(self.observed_at_ms),
            self.aic,
            self.trace_id,
            self.correlation_id,
            self.direction,
            self.event_type,
            self.system,
            self.destination_name,
            self.destination_kind,
            self.virtual_host,
            self.subscription_name,
            self.consumer_group_name,
            self.routing_key,
            self.partition,
            self.offset,
            self.message_id,
            self.lifecycle_key,
            self.payload_size_bytes,
            self.delivery_attempt,
            self.settlement_latency_ms,
            self.settlement_reason,
            self.error_code,
            self.error_message,
            self.attributes,
            self.raw_log,
        )


def derive_direction(event_type: str) -> str:
    """设计 §3.1.1：'send' → 'send'；其余 → 'receive'。"""
    return "send" if event_type in _SEND_EVENTS else "receive"


def project_attributes(attributes: dict[str, Any] | None) -> dict[str, str]:
    """Map(String,String) 有损投影：非字符串值 JSON 编码为字符串，None → {}。

    数值/布尔/嵌套对象/数组均用 json.dumps() 序列化，以保持标准 JSON 表示（设计 §4.1 / C-MESSAGE-WRITE-4）。
    仅用于展示与等值匹配，不支持类型化比较。
    """
    if attributes is None:
        return {}
    return {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in attributes.items()}


def parse_iso_to_ms(ts: str) -> int:
    """ISO 8601（aware）→ epoch ms（UTC）；不可解析或 naive → raise InvalidMessageRecordError。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise InvalidMessageRecordError(f"无法解析时间戳: {ts!r}") from exc
    if dt.tzinfo is None:
        raise InvalidMessageRecordError(f"时间戳缺少时区信息（naive datetime）: {ts!r}")
    return int(dt.timestamp() * 1000)


def _safe_raw_log(record: Any, body: Any) -> str:
    """生成脱敏后的 raw_log 字符串（仅在 store_raw_log=True 时调用）。

    移除 attributes 中含 password/token/secret/key 键的字段（设计 §2.5）。
    """
    try:
        raw: dict[str, Any] = {}
        if hasattr(record, "__dict__"):
            for k, v in vars(record).items():
                raw[k] = v
        elif hasattr(record, "model_dump"):
            raw = record.model_dump()

        # 脱敏 body.attributes 敏感键
        attrs = getattr(body, "attributes", None)
        if attrs and isinstance(attrs, dict):
            cleaned = {k: v for k, v in attrs.items() if not _SENSITIVE_KEY_RE.search(k)}
            raw["_sanitized_attributes"] = cleaned

        return json.dumps(raw, default=str, ensure_ascii=False)
    except Exception:
        return "{}"


def build_event_row(
    *,
    record: Any,
    body: Any,
    log_id: str,
    lifecycle_key: str,
    observed_at_ms: int,
    store_raw_log: bool,
) -> EventRow:
    """MessageBody + LogRecord 顶层字段 → EventRow。

    C-MESSAGE-MODEL-6 列映射：
    - event_type = body.event_type（直写，1:1 无派生）
    - direction  = derive_direction(event_type)（send→send，其余→receive）
    - correlation_id 取自 record 顶层（非 body）
    """
    timestamp_ms = parse_iso_to_ms(str(record.timestamp))

    event_type = str(body.event_type)
    direction = derive_direction(event_type)

    dest = body.destination
    destination_name = str(dest.name) if dest else ""
    destination_kind = str(dest.kind) if dest else ""
    virtual_host = str(dest.virtual_host) if dest and dest.virtual_host else "/"

    routing = getattr(body, "routing", None)
    routing_key = str(routing.key) if routing and routing.key else ""
    partition: str | None = str(routing.partition) if routing and routing.partition is not None else None
    offset: int | None = int(routing.offset) if routing and routing.offset is not None else None

    settlement = getattr(body, "settlement", None)
    settlement_latency_ms: int | None = None
    settlement_reason = ""
    if settlement is not None:
        if hasattr(settlement, "latency_ms") and settlement.latency_ms is not None:
            settlement_latency_ms = int(settlement.latency_ms)
        if hasattr(settlement, "reason") and settlement.reason:
            settlement_reason = str(settlement.reason)

    error = getattr(body, "error", None)
    error_code = ""
    error_message = ""
    if error is not None:
        if hasattr(error, "code") and error.code is not None:
            error_code = str(error.code)
        if hasattr(error, "message") and error.message:
            error_message = str(error.message)

    delivery_attempt: int | None = None
    da = getattr(body, "delivery_attempt", None)
    if da is not None:
        delivery_attempt = int(da)

    attributes = project_attributes(getattr(body, "attributes", None))
    raw_log = _safe_raw_log(record, body) if store_raw_log else ""

    return EventRow(
        log_id=log_id,
        timestamp_ms=timestamp_ms,
        observed_at_ms=observed_at_ms,
        aic=str(getattr(record, "aic", "") or ""),
        trace_id=str(getattr(record, "trace_id", "") or ""),
        correlation_id=str(getattr(record, "correlation_id", "") or ""),
        direction=direction,
        event_type=event_type,
        system=str(body.system) if hasattr(body, "system") else "",
        destination_name=destination_name,
        destination_kind=destination_kind,
        virtual_host=virtual_host,
        subscription_name=str(getattr(body, "subscription_name", "") or ""),
        consumer_group_name=str(getattr(body, "consumer_group_name", "") or ""),
        routing_key=routing_key,
        partition=partition,
        offset=offset,
        message_id=str(getattr(body, "message_id", "") or ""),
        lifecycle_key=lifecycle_key,
        payload_size_bytes=int(getattr(body, "payload_size_bytes", 0) or 0),
        delivery_attempt=delivery_attempt,
        settlement_latency_ms=settlement_latency_ms,
        settlement_reason=settlement_reason,
        error_code=error_code,
        error_message=error_message,
        attributes=attributes,
        raw_log=raw_log,
    )
