from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from acps_sdk.aip import GroupLeader, GroupPartnerMqClient
from acps_sdk.aip.aip_identity import SenderIdentityMismatchError

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"


def _legacy_invitation_body() -> dict:
    return {
        "type": "group-invitation",
        "id": "invite-1",
        "sentAt": "2026-06-24T00:00:00Z",
        "senderRole": "leader",
        "groupId": "group-1",
        "protocol": "rabbitmq:4.2",
        "expiresAt": "2026-06-24T00:05:00Z",
        "invitationToken": "token-1",
        "group": {
            "groupId": "group-1",
            "leader": {"aic": LEADER_AIC},
            "partners": [{"aic": PARTNER_AIC}],
        },
        "amqp": {
            "exchange": "group_foo",
            "exchangeType": "fanout",
            "routingKey": "",
        },
    }


def _legacy_invitation_error_body(*, partner_aic: str = PARTNER_AIC) -> dict:
    return {
        "type": "group-invitation-error",
        "id": "invite-err-1",
        "sentAt": "2026-06-24T00:00:01Z",
        "senderRole": "partner",
        "groupId": "group-1",
        "partnerAic": partner_aic,
        "invitationToken": "token-1",
        "error": {
            "code": -32020,
            "message": "Invitation rejected by partner policy",
            "data": {"errorType": "INVITATION_REJECTED"},
        },
    }


def _incoming_message(body: dict, *, user_id: str | None) -> MagicMock:
    message = MagicMock()
    message.user_id = user_id
    message.body = json.dumps(body, ensure_ascii=False).encode()
    message.headers = None
    message.routing_key = ""
    message.redelivered = False

    @asynccontextmanager
    async def _process():
        yield

    message.process = MagicMock(return_value=_process())
    return message


@pytest.mark.asyncio
async def test_partner_inbox_consumer_normalizes_legacy_invitation_from_amqp_user_id() -> None:
    client = GroupPartnerMqClient(partner_aic=PARTNER_AIC, identity_binding_enabled=True)
    inbox_queue = MagicMock()
    captured: dict[str, object] = {}
    received: dict[str, object] = {}

    async def _consume(handler):
        captured["handler"] = handler
        return "tag-1"

    inbox_queue.consume = _consume
    client.ensure_inbox_queue = AsyncMock(return_value=inbox_queue)  # type: ignore[method-assign]

    async def _handler(invitation):
        received["invitation"] = invitation

    await client.start_inbox_consuming(_handler)
    await captured["handler"](
        _incoming_message(_legacy_invitation_body(), user_id=LEADER_AIC)
    )

    invitation = received["invitation"]
    assert invitation.senderId == LEADER_AIC
    assert invitation.groupId == "group-1"
    assert invitation.group.leader.aic == LEADER_AIC


@pytest.mark.asyncio
async def test_leader_inbox_consumer_normalizes_legacy_invitation_error_from_amqp_user_id() -> None:
    leader = GroupLeader(
        LEADER_AIC,
        {"host": "mq.local", "port": 5671, "vhost": "acps"},
        identity_binding_enabled=True,
    )
    channel = MagicMock()
    queue = MagicMock()
    queue.is_closed = False
    exchange = MagicMock()
    connection = MagicMock()
    connection.is_closed = False
    connection.channel = AsyncMock(return_value=channel)
    channel.declare_queue = AsyncMock(return_value=queue)
    channel.declare_exchange = AsyncMock(return_value=exchange)
    queue.bind = AsyncMock()
    captured: dict[str, object] = {}

    async def _consume(handler):
        captured["handler"] = handler
        return "tag-1"

    queue.consume = _consume
    leader._connection = connection
    leader._handle_invitation_error = AsyncMock()  # type: ignore[method-assign]

    await leader._ensure_inbox()
    await captured["handler"](
        _incoming_message(_legacy_invitation_error_body(), user_id=PARTNER_AIC)
    )

    error_message = leader._handle_invitation_error.await_args.args[0]
    assert error_message.senderId == PARTNER_AIC
    assert error_message.partnerAic == PARTNER_AIC


@pytest.mark.asyncio
async def test_leader_inbox_consumer_rejects_legacy_invitation_error_when_user_id_mismatches() -> None:
    leader = GroupLeader(
        LEADER_AIC,
        {"host": "mq.local", "port": 5671, "vhost": "acps"},
        identity_binding_enabled=True,
    )
    channel = MagicMock()
    queue = MagicMock()
    queue.is_closed = False
    exchange = MagicMock()
    connection = MagicMock()
    connection.is_closed = False
    connection.channel = AsyncMock(return_value=channel)
    channel.declare_queue = AsyncMock(return_value=queue)
    channel.declare_exchange = AsyncMock(return_value=exchange)
    queue.bind = AsyncMock()
    captured: dict[str, object] = {}

    async def _consume(handler):
        captured["handler"] = handler
        return "tag-1"

    queue.consume = _consume
    leader._connection = connection
    leader._handle_invitation_error = AsyncMock()  # type: ignore[method-assign]

    await leader._ensure_inbox()
    with pytest.raises(SenderIdentityMismatchError):
        await captured["handler"](
            _incoming_message(_legacy_invitation_error_body(), user_id=OTHER_AIC)
        )

    leader._handle_invitation_error.assert_not_awaited()
