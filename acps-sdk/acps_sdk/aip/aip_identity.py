from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from acps_sdk.aic import validate_aic_format

from .aip_rpc_model import JSONRPCError

AUTHENTICATION_REQUIRED_CODE = -32008
AUTHORIZATION_FAILED_CODE = -32009


class AipIdentityError(Exception):
    """Base error for AIP identity binding failures."""


class PeerAicMissingError(AipIdentityError):
    """Raised when identity binding requires a peer AIC but none is available."""


class SenderIdentityMismatchError(AipIdentityError):
    """Raised when a payload sender identity does not match the peer identity."""


class InvalidPeerCertificateError(AipIdentityError):
    """Raised when a peer certificate cannot produce a valid AIC identity."""


@dataclass(frozen=True)
class PeerIdentity:
    """Normalized peer identity extracted from a peer certificate."""

    aic: str
    common_name: str | None = None
    san_aic: str | None = None


@dataclass(frozen=True)
class AipIdentityBindingConfig:
    """Global identity binding switch."""

    enabled: bool = True


def normalize_aic(aic: str | None) -> str | None:
    """Normalize an AIC for consistent comparison."""
    if aic is None:
        return None
    normalized = str(aic).strip().upper()
    return normalized or None


def normalize_and_validate_aic(
    aic: str | None,
    *,
    field_name: str = "senderId",
) -> str:
    """Normalize and validate an AIC carried in a protocol payload."""
    normalized = normalize_aic(aic)
    if normalized is None:
        raise SenderIdentityMismatchError(f"{field_name} is required")
    valid, error = validate_aic_format(normalized)
    if not valid:
        raise SenderIdentityMismatchError(f"{field_name} is invalid: {error}")
    return normalized


def normalize_and_validate_expected_aic(
    aic: str | None,
    *,
    field_name: str = "expected_aic",
) -> str:
    """Normalize and validate a configured local/expected AIC."""
    normalized = normalize_aic(aic)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    valid, error = validate_aic_format(normalized)
    if not valid:
        raise ValueError(f"{field_name} is invalid: {error}")
    return normalized


def extract_common_name(peer_certificate: dict[str, Any] | None) -> str | None:
    """Extract the peer certificate common name from ssl.getpeercert() output."""
    if peer_certificate is None:
        return None

    subject = peer_certificate.get("subject")
    if not isinstance(subject, tuple):
        return None

    for rdns in subject:
        if not isinstance(rdns, tuple):
            continue
        for attribute in rdns:
            if (
                isinstance(attribute, tuple)
                and len(attribute) == 2
                and attribute[0] == "commonName"
                and isinstance(attribute[1], str)
            ):
                return attribute[1]
    return None


def extract_acps_uri_san(peer_certificate: dict[str, Any] | None) -> str | None:
    """Extract the SAN URI AIC from ssl.getpeercert() output."""
    if peer_certificate is None:
        return None

    subject_alt_name = peer_certificate.get("subjectAltName")
    if not isinstance(subject_alt_name, tuple):
        return None

    acps_uris: list[str] = []
    for entry in subject_alt_name:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        kind, value = entry
        if kind != "URI" or not isinstance(value, str):
            continue
        if value.lower().startswith("acps://"):
            acps_uris.append(value[len("acps://") :])

    if not acps_uris:
        return None

    normalized_values = {
        normalized
        for normalized in (normalize_aic(value) for value in acps_uris)
        if normalized is not None
    }
    if len(normalized_values) > 1:
        raise InvalidPeerCertificateError(
            "peer certificate contains multiple distinct ACPs SAN URI AIC values"
        )
    return acps_uris[0]


def extract_peer_identity(
    peer_certificate: dict[str, Any] | None,
) -> PeerIdentity | None:
    """Extract a normalized peer identity from a TLS peer certificate."""
    common_name = extract_common_name(peer_certificate)
    san_aic = extract_acps_uri_san(peer_certificate)

    if common_name is None:
        return None

    normalized_cn = normalize_aic(common_name)
    valid_cn, cn_error = validate_aic_format(normalized_cn or "")
    if not valid_cn:
        raise InvalidPeerCertificateError(f"invalid peer certificate CN: {cn_error}")

    normalized_san: str | None = None
    if san_aic is not None:
        normalized_san = normalize_aic(san_aic)
        valid_san, san_error = validate_aic_format(normalized_san or "")
        if not valid_san:
            raise InvalidPeerCertificateError(
                f"invalid peer certificate SAN URI: {san_error}"
            )
        if normalized_san != normalized_cn:
            raise InvalidPeerCertificateError(
                "peer certificate CN and SAN URI AIC do not match"
            )

    return PeerIdentity(
        aic=normalized_cn,
        common_name=common_name,
        san_aic=normalized_san,
    )


def _extract_sender_id(message_or_sender: Any) -> str | None:
    if isinstance(message_or_sender, str):
        return message_or_sender
    if isinstance(message_or_sender, dict):
        sender = message_or_sender.get("senderId")
        return str(sender) if sender is not None else None
    sender = getattr(message_or_sender, "senderId", None)
    return str(sender) if sender is not None else None


def assert_sender_matches_peer(message_or_sender: Any, peer_aic: str | None) -> str:
    """Assert that a payload senderId matches the authenticated peer AIC."""
    normalized_peer = normalize_aic(peer_aic)
    if normalized_peer is None:
        raise PeerAicMissingError("peer AIC is required for identity binding")

    sender_id = normalize_and_validate_aic(
        _extract_sender_id(message_or_sender),
        field_name="senderId",
    )
    if sender_id != normalized_peer:
        raise SenderIdentityMismatchError(
            f"senderId {sender_id} does not match peer AIC {normalized_peer}"
        )
    return sender_id


def assert_sender_matches_expected(
    message_or_sender: Any,
    expected_aic: str | None,
) -> str:
    """Assert that a payload senderId matches a configured expected/local AIC."""
    normalized_expected = normalize_and_validate_expected_aic(
        expected_aic,
        field_name="expected_aic",
    )
    sender_id = normalize_and_validate_aic(
        _extract_sender_id(message_or_sender),
        field_name="senderId",
    )
    if sender_id != normalized_expected:
        raise SenderIdentityMismatchError(
            f"senderId {sender_id} does not match expected AIC {normalized_expected}"
        )
    return sender_id


def assert_aic_matches_expected(
    actual_aic: str | None,
    expected_aic: str | None,
    *,
    actual_label: str = "peer AIC",
    expected_label: str = "expected_aic",
) -> str:
    """Assert that an actual AIC matches a configured expected AIC."""
    normalized_expected = normalize_and_validate_expected_aic(
        expected_aic,
        field_name=expected_label,
    )
    normalized_actual = normalize_aic(actual_aic)
    if normalized_actual is None:
        raise PeerAicMissingError(f"{actual_label} is required for identity binding")

    valid_actual, actual_error = validate_aic_format(normalized_actual)
    if not valid_actual:
        raise InvalidPeerCertificateError(f"{actual_label} is invalid: {actual_error}")
    if normalized_actual != normalized_expected:
        raise SenderIdentityMismatchError(
            f"{actual_label} {normalized_actual} does not match {expected_label} {normalized_expected}"
        )
    return normalized_actual


def extract_peer_aic_from_httpx_response(response: httpx.Response) -> str | None:
    """Extract the normalized TLS peer AIC from an httpx response."""
    network_stream = response.extensions.get("network_stream")
    if network_stream is None:
        return None

    get_extra_info = getattr(network_stream, "get_extra_info", None)
    if not callable(get_extra_info):
        return None

    ssl_object = get_extra_info("ssl_object")
    if ssl_object is None:
        return None

    getpeercert = getattr(ssl_object, "getpeercert", None)
    if not callable(getpeercert):
        return None

    peer_identity = extract_peer_identity(getpeercert())
    return peer_identity.aic if peer_identity else None


def identity_error_to_jsonrpc(error: AipIdentityError) -> JSONRPCError:
    """Map identity binding errors into stable JSON-RPC error objects."""
    if isinstance(error, (PeerAicMissingError, InvalidPeerCertificateError)):
        return JSONRPCError(
            code=AUTHENTICATION_REQUIRED_CODE,
            message="AuthenticationRequiredError",
            data=str(error),
        )
    if isinstance(error, SenderIdentityMismatchError):
        return JSONRPCError(
            code=AUTHORIZATION_FAILED_CODE,
            message="AuthorizationFailedError",
            data=str(error),
        )
    return JSONRPCError(
        code=AUTHORIZATION_FAILED_CODE,
        message="AuthorizationFailedError",
        data=str(error),
    )
