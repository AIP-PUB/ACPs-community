from __future__ import annotations

from typing import Any

from .aip_identity import (
    PeerAicMissingError,
    SenderIdentityMismatchError,
    normalize_aic,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    normalize_and_validate_aic,
)


def extract_amqp_user_id(incoming_message: Any) -> str | None:
    """Extract a normalized AMQP user_id property from an incoming message."""
    user_id = getattr(incoming_message, "user_id", None)
    if isinstance(user_id, bytes):
        user_id = user_id.decode("utf-8")
    if user_id is None:
        return None
    return normalize_and_validate_aic(str(user_id), field_name="user_id")


def build_outgoing_amqp_user_id(
    message_or_sender: Any,
    *,
    local_aic: str | None,
    identity_binding_enabled: bool,
) -> str | None:
    """Build the outgoing AMQP user_id for a message."""
    if not identity_binding_enabled:
        return None
    return assert_sender_matches_expected(message_or_sender, local_aic)


def assert_incoming_group_message_identity(
    message_or_sender: Any,
    *,
    amqp_user_id: str | None,
    expected_group_id: str | None = None,
) -> str:
    """Assert AMQP-authenticated sender identity and group context."""
    if amqp_user_id is None:
        raise PeerAicMissingError("AMQP user_id is required for group identity binding")
    sender_id = assert_sender_matches_peer(message_or_sender, amqp_user_id)

    if isinstance(message_or_sender, dict):
        group_id = message_or_sender.get("groupId")
    else:
        group_id = getattr(message_or_sender, "groupId", None)
    normalized_group_id = str(group_id).strip() if group_id is not None else ""
    if not normalized_group_id:
        raise SenderIdentityMismatchError("groupId is required for group identity binding")
    if expected_group_id is not None and normalized_group_id != expected_group_id:
        raise SenderIdentityMismatchError(
            f"groupId {normalized_group_id} does not match expected groupId {expected_group_id}"
        )
    return sender_id


def _extract_group_leader_aic(request_or_params: Any) -> str | None:
    if isinstance(request_or_params, dict):
        if "params" in request_or_params:
            return _extract_group_leader_aic(request_or_params.get("params"))
        group = request_or_params.get("group")
        if isinstance(group, dict):
            leader = group.get("leader")
            if isinstance(leader, dict):
                aic = leader.get("aic")
                return str(aic) if aic is not None else None
        return None

    params = getattr(request_or_params, "params", None)
    if params is not None:
        return _extract_group_leader_aic(params)

    group = getattr(request_or_params, "group", None)
    if group is None:
        return None

    leader = getattr(group, "leader", None)
    if leader is None:
        return None

    aic = getattr(leader, "aic", None)
    return str(aic) if aic is not None else None


def assert_direct_group_request_identity(
    request_or_params: Any,
    *,
    peer_aic: str | None,
) -> str:
    """Assert that a direct group invitation request is sent by the mTLS peer."""
    normalized_peer = normalize_aic(peer_aic)
    if normalized_peer is None:
        raise PeerAicMissingError(
            "peer AIC is required for direct group invitation identity binding"
        )

    leader_aic = normalize_and_validate_aic(
        _extract_group_leader_aic(request_or_params),
        field_name="group.leader.aic",
    )
    if leader_aic != normalized_peer:
        raise SenderIdentityMismatchError(
            f"group.leader.aic {leader_aic} does not match peer AIC {normalized_peer}"
        )
    return leader_aic
