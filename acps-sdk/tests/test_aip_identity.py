from __future__ import annotations

import httpx
import pytest

from acps_sdk.aip import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHORIZATION_FAILED_CODE,
    InvalidPeerCertificateError,
    PeerAicMissingError,
    SenderIdentityMismatchError,
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_acps_uri_san,
    extract_common_name,
    extract_peer_aic_from_httpx_response,
    extract_peer_identity,
    identity_error_to_jsonrpc,
    normalize_aic,
)
from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType

VALID_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


def _cert(
    *,
    common_name: str | None = VALID_AIC,
    san_aic: str | None = None,
    san_entries: tuple[tuple[str, str], ...] | None = None,
) -> dict:
    cert: dict = {}
    if common_name is not None:
        cert["subject"] = ((("commonName", common_name),),)
    if san_entries is not None:
        cert["subjectAltName"] = san_entries
    elif san_aic is not None:
        cert["subjectAltName"] = (("URI", f"acps://{san_aic}"),)
    return cert


def _command(sender_id: str = VALID_AIC) -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="leader",
        senderId=sender_id,
        command=TaskCommandType.Start,
        taskId="task-1",
        sessionId="session-1",
    )


class _FakeSslObject:
    def __init__(self, cert: dict | None) -> None:
        self._cert = cert

    def getpeercert(self) -> dict | None:
        return self._cert


class _FakeNetworkStream:
    def __init__(self, cert: dict | None) -> None:
        self._ssl_object = _FakeSslObject(cert) if cert is not None else None

    def get_extra_info(self, name: str):
        if name == "ssl_object":
            return self._ssl_object
        return None


def test_extract_common_name() -> None:
    assert extract_common_name(_cert()) == VALID_AIC


def test_extract_acps_uri_san() -> None:
    assert extract_acps_uri_san(_cert(san_aic=VALID_AIC)) == VALID_AIC


def test_extract_acps_uri_san_rejects_multiple_distinct_acps_values() -> None:
    with pytest.raises(InvalidPeerCertificateError):
        extract_acps_uri_san(
            _cert(
                san_entries=(
                    ("URI", f"acps://{VALID_AIC}"),
                    ("URI", f"acps://{OTHER_AIC}"),
                )
            )
        )


def test_extract_peer_identity_with_matching_cn_and_san() -> None:
    peer = extract_peer_identity(_cert(common_name=VALID_AIC.lower(), san_aic=VALID_AIC))
    assert peer is not None
    assert peer.aic == VALID_AIC
    assert peer.san_aic == VALID_AIC


def test_extract_peer_identity_rejects_cn_san_mismatch() -> None:
    with pytest.raises(InvalidPeerCertificateError):
        extract_peer_identity(_cert(common_name=VALID_AIC, san_aic=OTHER_AIC))


def test_extract_peer_identity_returns_none_when_cn_missing() -> None:
    assert extract_peer_identity(_cert(common_name=None, san_aic=VALID_AIC)) is None


def test_extract_peer_identity_rejects_invalid_cn() -> None:
    with pytest.raises(InvalidPeerCertificateError):
        extract_peer_identity(_cert(common_name="not-aic"))


def test_normalize_aic_strips_whitespace_and_uppercases() -> None:
    assert normalize_aic(f"  {VALID_AIC.lower()} \n") == VALID_AIC


def test_normalize_aic_preserves_internal_whitespace_for_validation() -> None:
    assert normalize_aic(f"{VALID_AIC[:10]} {VALID_AIC[10:]}") == f"{VALID_AIC[:10]} {VALID_AIC[10:]}"


def test_assert_sender_matches_peer_passes() -> None:
    assert assert_sender_matches_peer(_command(), VALID_AIC.lower()) == VALID_AIC


def test_assert_sender_matches_peer_rejects_missing_peer_aic() -> None:
    with pytest.raises(PeerAicMissingError):
        assert_sender_matches_peer(_command(), None)


def test_assert_sender_matches_peer_rejects_missing_sender_id() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_sender_matches_peer({"senderId": None}, VALID_AIC)


def test_assert_sender_matches_peer_rejects_invalid_sender_id() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_sender_matches_peer(_command(sender_id="bad-aic"), VALID_AIC)


def test_assert_sender_matches_peer_rejects_sender_id_with_internal_whitespace() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_sender_matches_peer(
            _command(sender_id=f"{VALID_AIC[:10]} {VALID_AIC[10:]}"),
            VALID_AIC,
        )


def test_assert_sender_matches_peer_rejects_mismatch() -> None:
    with pytest.raises(SenderIdentityMismatchError):
        assert_sender_matches_peer(_command(sender_id=OTHER_AIC), VALID_AIC)


def test_assert_sender_matches_expected_passes() -> None:
    assert assert_sender_matches_expected(_command(), VALID_AIC.lower()) == VALID_AIC


def test_assert_aic_matches_expected_passes() -> None:
    assert assert_aic_matches_expected(VALID_AIC.lower(), VALID_AIC) == VALID_AIC


def test_extract_peer_aic_from_httpx_response() -> None:
    response = httpx.Response(
        200,
        extensions={"network_stream": _FakeNetworkStream(_cert(san_aic=VALID_AIC))},
    )
    assert extract_peer_aic_from_httpx_response(response) == VALID_AIC


def test_extract_peer_aic_from_httpx_response_without_network_stream() -> None:
    response = httpx.Response(200)
    assert extract_peer_aic_from_httpx_response(response) is None


def test_extract_peer_aic_from_httpx_response_without_ssl_object() -> None:
    class _NoSslNetworkStream:
        def get_extra_info(self, name: str):
            return None

    response = httpx.Response(200, extensions={"network_stream": _NoSslNetworkStream()})
    assert extract_peer_aic_from_httpx_response(response) is None


def test_identity_error_to_jsonrpc_authentication_mapping() -> None:
    error = identity_error_to_jsonrpc(PeerAicMissingError("missing peer"))
    assert error.code == AUTHENTICATION_REQUIRED_CODE
    assert error.message == "AuthenticationRequiredError"


def test_identity_error_to_jsonrpc_authorization_mapping() -> None:
    error = identity_error_to_jsonrpc(
        SenderIdentityMismatchError("sender mismatch")
    )
    assert error.code == AUTHORIZATION_FAILED_CODE
    assert error.message == "AuthorizationFailedError"
