from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acps_sdk.aip import (
    GroupLeaderMqClient,
    GroupPartnerMqClient,
    PeerAicMissingError,
    SenderIdentityMismatchError,
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
    assert_direct_group_request_identity,
    build_outgoing_amqp_user_id,
    assert_incoming_group_message_identity,
    extract_amqp_user_id,
)
from acps_sdk.aip.aip_group_model import ACSObject, AMQPConfig, GroupInfo, RabbitMQRequest, RabbitMQRequestParams, RabbitMQServerConfig

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"


def _command(sender_id: str = LEADER_AIC, group_id: str = "group-1") -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="leader",
        senderId=sender_id,
        sessionId="session-1",
        command=TaskCommandType.Start,
        taskId="task-1",
        groupId=group_id,
    )


def _result(sender_id: str = PARTNER_AIC, group_id: str = "group-1") -> TaskResult:
    return TaskResult(
        id="res-1",
        sentAt="2026-06-24T00:00:01Z",
        senderRole="partner",
        senderId=sender_id,
        sessionId="session-1",
        taskId="task-1",
        groupId=group_id,
        status=TaskStatus(state=TaskState.Working, stateChangedAt="2026-06-24T00:00:01Z"),
    )


class _Incoming:
    def __init__(self, user_id):
        self.user_id = user_id


def test_extract_amqp_user_id_accepts_str_and_bytes() -> None:
    assert extract_amqp_user_id(_Incoming(LEADER_AIC)) == LEADER_AIC
    assert extract_amqp_user_id(_Incoming(LEADER_AIC.encode())) == LEADER_AIC


def test_build_outgoing_amqp_user_id_returns_sender_when_enabled() -> None:
    assert (
        build_outgoing_amqp_user_id(
            _command(),
            local_aic=LEADER_AIC,
            identity_binding_enabled=True,
        )
        == LEADER_AIC
    )


def test_build_outgoing_amqp_user_id_returns_none_when_disabled() -> None:
    assert (
        build_outgoing_amqp_user_id(
            _command(sender_id=OTHER_AIC),
            local_aic=LEADER_AIC,
            identity_binding_enabled=False,
        )
        is None
    )


def test_assert_incoming_group_message_identity_passes() -> None:
    assert (
        assert_incoming_group_message_identity(
            _command(),
            amqp_user_id=LEADER_AIC,
            expected_group_id="group-1",
        )
        == LEADER_AIC
    )


def test_assert_incoming_group_message_identity_rejects_missing_user_id() -> None:
    with pytest.raises(PeerAicMissingError):
        assert_incoming_group_message_identity(
            _command(),
            amqp_user_id=None,
            expected_group_id="group-1",
        )


def test_assert_incoming_group_message_identity_rejects_sender_mismatch() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_incoming_group_message_identity(
            _command(sender_id=OTHER_AIC),
            amqp_user_id=LEADER_AIC,
            expected_group_id="group-1",
        )


def test_assert_incoming_group_message_identity_rejects_group_mismatch() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_incoming_group_message_identity(
            _command(group_id="group-x"),
            amqp_user_id=LEADER_AIC,
            expected_group_id="group-1",
        )


def _group_request(leader_aic: str = LEADER_AIC) -> RabbitMQRequest:
    return RabbitMQRequest(
        id="rpc-group-1",
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=GroupInfo(
                groupId="group-1",
                leader=ACSObject(aic=leader_aic),
                partners=[],
            ),
            server=RabbitMQServerConfig(host="localhost", port=5671, vhost="acps"),
            amqp=AMQPConfig(exchange="group-1", exchangeType="fanout", routingKey=""),
        ),
    )


def test_assert_direct_group_request_identity_passes() -> None:
    assert (
        assert_direct_group_request_identity(_group_request(), peer_aic=LEADER_AIC)
        == LEADER_AIC
    )


def test_assert_direct_group_request_identity_rejects_missing_peer() -> None:
    with pytest.raises(PeerAicMissingError):
        assert_direct_group_request_identity(_group_request(), peer_aic=None)


def test_assert_direct_group_request_identity_rejects_leader_mismatch() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_direct_group_request_identity(
            _group_request(leader_aic=OTHER_AIC),
            peer_aic=LEADER_AIC,
        )


@pytest.mark.asyncio
async def test_group_leader_publish_sets_amqp_user_id_when_enabled() -> None:
    client = GroupLeaderMqClient(leader_aic=LEADER_AIC, identity_binding_enabled=True)
    client._exchange = AsyncMock()
    client._exchange_name = "group-1"
    client._group_id = "group-1"

    captured: dict[str, object] = {}

    async def _publish(msg, routing_key=""):
        captured["user_id"] = msg.user_id

    client._exchange.publish = AsyncMock(side_effect=_publish)
    await client.publish_message(_command())
    assert captured["user_id"] == LEADER_AIC


@pytest.mark.asyncio
async def test_group_partner_publish_sets_amqp_user_id_when_enabled() -> None:
    client = GroupPartnerMqClient(partner_aic=PARTNER_AIC, identity_binding_enabled=True)
    client._exchange = AsyncMock()
    client._group_id = "group-1"

    captured: dict[str, object] = {}

    async def _publish(msg, routing_key=""):
        captured["user_id"] = msg.user_id

    client._exchange.publish = AsyncMock(side_effect=_publish)
    await client._publish_message(_result())
    assert captured["user_id"] == PARTNER_AIC


@pytest.mark.asyncio
async def test_group_partner_consume_rejects_missing_amqp_user_id_when_enabled() -> None:
    client = GroupPartnerMqClient(partner_aic=PARTNER_AIC, identity_binding_enabled=True)
    client._queue = MagicMock()
    client._queue_name = "partner.queue"
    client._group_id = "group-1"
    captured: dict[str, object] = {}

    async def _consume(handler):
        captured["handler"] = handler
        return "tag-1"

    client._queue.consume = _consume
    await client._start_consuming()

    message = MagicMock()
    message.user_id = None
    message.body = json.dumps(_command().model_dump(exclude_none=True)).encode()
    message.headers = None
    message.routing_key = ""
    message.redelivered = False

    @asynccontextmanager
    async def _process():
        yield

    message.process = MagicMock(return_value=_process())

    with pytest.raises(PeerAicMissingError):
        await captured["handler"](message)


@pytest.mark.asyncio
async def test_group_leader_consume_rejects_user_id_sender_mismatch_when_enabled() -> None:
    client = GroupLeaderMqClient(leader_aic=LEADER_AIC, identity_binding_enabled=True)
    client._group_id = "group-1"
    client._exchange_name = "group-1"
    client._leader_queue = MagicMock()
    client._leader_queue.name = "leader.queue"
    captured: dict[str, object] = {}

    async def _consume(handler):
        captured["handler"] = handler

    client._leader_queue.consume = _consume

    def _fake_create_task(coro):
        async def _run():
            await coro

        return asyncio.get_running_loop().create_task(_run())

    with patch("acps_sdk.aip.aip_group_leader.asyncio.create_task", side_effect=_fake_create_task):
        await client.start_consuming()
        await asyncio.sleep(0)

    message = MagicMock()
    message.user_id = OTHER_AIC
    message.body = json.dumps(_result().model_dump(exclude_none=True)).encode()
    message.headers = None
    message.routing_key = ""
    message.redelivered = False

    @asynccontextmanager
    async def _process():
        yield

    message.process = MagicMock(return_value=_process())

    with pytest.raises(SenderIdentityMismatchError):
        await captured["handler"](message)


def test_group_partner_client_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")
    GroupPartnerMqClient(partner_aic=PARTNER_AIC, identity_binding_enabled=False)
    assert "identity binding disabled for grouppartnermqclient" in caplog.text.lower()


def test_group_leader_client_logs_warning_when_identity_binding_disabled(caplog) -> None:
    caplog.set_level("WARNING")
    GroupLeaderMqClient(leader_aic=LEADER_AIC, identity_binding_enabled=False)
    assert "identity binding disabled for groupleadermqclient" in caplog.text.lower()
