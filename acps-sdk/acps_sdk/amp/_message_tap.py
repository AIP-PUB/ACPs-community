"""群组 MQ 收发 → MessageBody 组装的纯函数（供 GroupLeaderMqClient / GroupPartnerMqClient 复用）。"""

from __future__ import annotations

from typing import Any

from acps_sdk.amp.models import ErrorInfo, MessageBody, MessageDestination, MessageRouting, MessageSettlement

_SETTLEMENT_EVENT_TYPES = frozenset({"ack", "nack", "reject", "timeout", "dead_letter"})


def build_send_body(
    *,
    system: str,
    exchange: str,
    virtual_host: str,
    routing_key: str,
    message_id: str,
    payload_size_bytes: int,
    attributes: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
) -> MessageBody:
    """producer 侧 send 事件 body 组装。"""
    return MessageBody(
        event_type="send",
        operation_name="publish",
        system=system,
        destination=MessageDestination(
            name=exchange,
            kind="exchange",
            virtual_host=virtual_host,
        ),
        routing=MessageRouting(key=routing_key),
        message_id=message_id,
        payload_size_bytes=payload_size_bytes,
        attributes=attributes,
        error=error,
    )


def build_receive_body(
    *,
    system: str,
    exchange: str,
    virtual_host: str,
    routing_key: str,
    queue_name: str,
    message_id: str,
    payload_size_bytes: int,
    delivery_attempt: int,
    attributes: dict[str, Any] | None = None,
) -> MessageBody:
    """consumer 侧 receive 事件 body 组装（destination.name 取 exchange，queue 记入 subscriptionName）。"""
    return MessageBody(
        event_type="receive",
        operation_name="deliver",
        system=system,
        destination=MessageDestination(
            name=exchange,
            kind="exchange",
            virtual_host=virtual_host,
        ),
        subscription_name=queue_name,
        routing=MessageRouting(key=routing_key),
        message_id=message_id,
        payload_size_bytes=payload_size_bytes,
        delivery_attempt=delivery_attempt,
        attributes=attributes,
    )


def build_settlement_body(
    event_type: str,
    *,
    system: str,
    exchange: str,
    virtual_host: str,
    queue_name: str,
    message_id: str,
    delivery_attempt: int,
    latency_ms: float,
    reason: str | None = None,
    error: ErrorInfo | None = None,
) -> MessageBody:
    """结算类事件（ack/nack/reject/timeout/dead_letter）body 组装。"""
    if event_type not in _SETTLEMENT_EVENT_TYPES:
        raise ValueError(f"invalid settlement event_type: {event_type!r}")

    settlement = MessageSettlement(latency_ms=latency_ms)
    if event_type == "nack" and reason is not None:
        settlement = MessageSettlement(latency_ms=latency_ms, reason=reason)

    return MessageBody(
        event_type=event_type,  # type: ignore[arg-type]
        operation_name=f"basic.{event_type}",
        system=system,
        destination=MessageDestination(
            name=exchange,
            kind="exchange",
            virtual_host=virtual_host,
        ),
        subscription_name=queue_name,
        message_id=message_id,
        delivery_attempt=delivery_attempt,
        settlement=settlement,
        error=error,
    )
