from __future__ import annotations

import pytest

from acps_sdk.aip import InboxGroupInvitation, InboxGroupInvitationError, Message

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


def _group() -> dict:
    return {
        "groupId": "group-1",
        "leader": {"aic": LEADER_AIC},
        "partners": [{"aic": PARTNER_AIC}],
    }


def _amqp() -> dict:
    return {"exchange": "group-1", "exchangeType": "fanout", "routingKey": ""}


def test_inbox_group_invitation_is_message_like_and_backfills_legacy_fields() -> None:
    invitation = InboxGroupInvitation.model_validate(
        {
            "type": "group-invitation",
            "protocol": "rabbitmq:4.2",
            "expiresAt": "2026-06-24T00:00:00Z",
            "invitationToken": "token-1",
            "group": _group(),
            "amqp": _amqp(),
        }
    )
    assert isinstance(invitation, Message)
    assert invitation.senderRole == "leader"
    assert invitation.senderId == LEADER_AIC
    assert invitation.groupId == "group-1"
    assert invitation.id.startswith("invite-")
    assert invitation.sentAt


def test_inbox_group_invitation_error_backfills_sender_from_partner_aic() -> None:
    message = InboxGroupInvitationError.model_validate(
        {
            "type": "group-invitation-error",
            "groupId": "group-1",
            "partnerAic": PARTNER_AIC,
            "invitationToken": "token-1",
            "error": {"code": -32020, "message": "rejected"},
        }
    )
    assert isinstance(message, Message)
    assert message.senderRole == "partner"
    assert message.senderId == PARTNER_AIC
    assert message.partnerAic == PARTNER_AIC
    assert message.id.startswith("invite-err-")
    assert message.sentAt


def test_inbox_group_invitation_error_new_payload_does_not_require_partner_aic() -> None:
    message = InboxGroupInvitationError(
        id="invite-err-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="partner",
        senderId=PARTNER_AIC,
        groupId="group-1",
        invitationToken="token-1",
        error={"code": -32020, "message": "rejected"},
    )
    assert message.partnerAic is None


def test_inbox_group_invitation_rejects_sender_id_mismatch_with_group_leader() -> None:
    with pytest.raises(ValueError, match="senderId must equal group.leader.aic"):
        InboxGroupInvitation.model_validate(
            {
                "type": "group-invitation",
                "id": "invite-1",
                "sentAt": "2026-06-24T00:00:00Z",
                "senderRole": "leader",
                "senderId": PARTNER_AIC,
                "groupId": "group-1",
                "protocol": "rabbitmq:4.2",
                "expiresAt": "2026-06-24T00:00:00Z",
                "invitationToken": "token-1",
                "group": _group(),
                "amqp": _amqp(),
            }
        )


def test_inbox_group_invitation_rejects_group_id_mismatch_with_group_payload() -> None:
    with pytest.raises(ValueError, match="groupId must equal group.groupId"):
        InboxGroupInvitation.model_validate(
            {
                "type": "group-invitation",
                "id": "invite-1",
                "sentAt": "2026-06-24T00:00:00Z",
                "senderRole": "leader",
                "senderId": LEADER_AIC,
                "groupId": "group-x",
                "protocol": "rabbitmq:4.2",
                "expiresAt": "2026-06-24T00:00:00Z",
                "invitationToken": "token-1",
                "group": _group(),
                "amqp": _amqp(),
            }
        )


def test_inbox_group_invitation_error_rejects_partner_aic_sender_mismatch() -> None:
    with pytest.raises(ValueError, match="partnerAic must equal senderId"):
        InboxGroupInvitationError.model_validate(
            {
                "type": "group-invitation-error",
                "id": "invite-err-1",
                "sentAt": "2026-06-24T00:00:00Z",
                "senderRole": "partner",
                "senderId": PARTNER_AIC,
                "partnerAic": LEADER_AIC,
                "groupId": "group-1",
                "invitationToken": "token-1",
                "error": {"code": -32020, "message": "rejected"},
            }
        )
