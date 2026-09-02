"""tests/amp/test_message_tap.py — _message_tap 纯函数单元测试。"""

from __future__ import annotations

import pytest

from acps_sdk.amp import _message_tap as tap


def test_build_send_body_exchange_fanout() -> None:
    body = tap.build_send_body(
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        routing_key="",
        message_id="cmd-001",
        payload_size_bytes=256,
    )

    assert body.event_type == "send"
    assert body.destination.kind == "exchange"
    assert body.destination.name == "group.abc"
    assert body.destination.virtual_host == "acps"
    assert body.routing is not None
    assert body.routing.key == ""
    assert body.delivery_attempt is None
    assert body.subscription_name is None


def test_build_receive_body_uses_exchange_not_queue() -> None:
    body = tap.build_receive_body(
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        routing_key="",
        queue_name="partner.queue.1",
        message_id="cmd-001",
        payload_size_bytes=256,
        delivery_attempt=1,
    )

    assert body.event_type == "receive"
    assert body.destination.name == "group.abc"
    assert body.destination.kind == "exchange"
    assert body.subscription_name == "partner.queue.1"
    assert body.delivery_attempt == 1


def test_build_send_and_receive_share_destination_name() -> None:
    send_body = tap.build_send_body(
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        routing_key="",
        message_id="cmd-001",
        payload_size_bytes=100,
    )
    recv_body = tap.build_receive_body(
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        routing_key="",
        queue_name="partner.queue.1",
        message_id="cmd-001",
        payload_size_bytes=100,
        delivery_attempt=1,
    )

    assert send_body.destination.name == recv_body.destination.name
    assert send_body.destination.kind == recv_body.destination.kind
    assert send_body.destination.virtual_host == recv_body.destination.virtual_host


def test_build_settlement_body_ack() -> None:
    body = tap.build_settlement_body(
        "ack",
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        queue_name="partner.queue.1",
        message_id="cmd-001",
        delivery_attempt=1,
        latency_ms=12.5,
    )

    assert body.event_type == "ack"
    assert body.settlement is not None
    assert body.settlement.latency_ms == 12.5
    assert body.settlement.reason is None


def test_build_settlement_body_nack_with_reason() -> None:
    body = tap.build_settlement_body(
        "nack",
        system="rabbitmq",
        exchange="group.abc",
        virtual_host="acps",
        queue_name="partner.queue.1",
        message_id="cmd-001",
        delivery_attempt=2,
        latency_ms=5.0,
        reason="handler failed",
    )

    assert body.event_type == "nack"
    assert body.settlement is not None
    assert body.settlement.reason == "handler failed"


def test_build_settlement_body_invalid_event_type() -> None:
    with pytest.raises(ValueError, match="invalid settlement event_type"):
        tap.build_settlement_body(
            "send",
            system="rabbitmq",
            exchange="group.abc",
            virtual_host="acps",
            queue_name="q",
            message_id="cmd-001",
            delivery_attempt=1,
            latency_ms=1.0,
        )
