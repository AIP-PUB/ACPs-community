from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from acps_sdk.oidc import (
    OidcClientError,
    OidcDeviceAuthorizationDeniedError,
    OidcDeviceAuthorizationExpiredError,
    OidcDeviceAuthorizationNotSupportedError,
    OidcDeviceClient,
    OidcDeviceClientConfig,
    OidcDeviceTokenPollingStatus,
    OidcProviderUnavailableError,
)

ISSUER = "https://issuer.example/realms/acps-registry"
CLIENT_ID = "registry-cli"


def _discovery_payload(
    *,
    issuer: str = ISSUER,
    include_device_endpoint: bool = True,
    include_revocation_endpoint: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/protocol/openid-connect/certs",
        "token_endpoint": f"{issuer}/protocol/openid-connect/token",
    }
    if include_device_endpoint:
        payload["device_authorization_endpoint"] = (
            f"{issuer}/protocol/openid-connect/auth/device"
        )
    if include_revocation_endpoint:
        payload["revocation_endpoint"] = f"{issuer}/protocol/openid-connect/revoke"
    return payload


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    issuer: str = ISSUER,
    require_https: bool = True,
    scopes: tuple[str, ...] = ("openid", "profile"),
) -> OidcDeviceClient:
    return OidcDeviceClient(
        OidcDeviceClientConfig(
            issuer=issuer,
            client_id=CLIENT_ID,
            scopes=scopes,
            require_https=require_https,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _form_payload(request: httpx.Request) -> dict[str, str]:
    encoded = request.read().decode()
    return {key: values[-1] for key, values in parse_qs(encoded).items()}


def test_device_client_config_reports_invalid_scope_types_as_validation_errors() -> (
    None
):
    with pytest.raises(ValidationError, match="scopes"):
        OidcDeviceClientConfig(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            scopes=123,
        )


def test_start_device_authorization_fails_when_provider_lacks_endpoint() -> None:
    client = _client(
        lambda _: httpx.Response(
            200, json=_discovery_payload(include_device_endpoint=False)
        )
    )

    with pytest.raises(
        OidcDeviceAuthorizationNotSupportedError,
        match="device_authorization_endpoint",
    ):
        client.start_device_authorization()


@pytest.mark.parametrize("include_complete_url", [True, False])
def test_start_device_authorization_sends_public_client_fields(
    include_complete_url: bool,
) -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        requests.append((request.url.path, _form_payload(request)))
        response_payload: dict[str, Any] = {
            "device_code": "device-code-123",
            "user_code": "USER-CODE",
            "verification_uri": "https://issuer.example/device",
            "expires_in": 600,
            "interval": 7,
        }
        if include_complete_url:
            response_payload["verification_uri_complete"] = (
                "https://issuer.example/device?user_code=USER-CODE"
            )
        return httpx.Response(200, json=response_payload)

    client = _client(handler)

    response = client.start_device_authorization()

    assert response.device_code.get_secret_value() == "device-code-123"
    assert response.user_code == "USER-CODE"
    assert response.verification_uri == "https://issuer.example/device"
    expected_complete = (
        "https://issuer.example/device?user_code=USER-CODE"
        if include_complete_url
        else None
    )
    assert response.verification_uri_complete == expected_complete
    assert response.interval == 7
    assert requests == [
        (
            "/realms/acps-registry/protocol/openid-connect/auth/device",
            {
                "client_id": CLIENT_ID,
                "scope": "openid profile",
            },
        )
    ]


@pytest.mark.parametrize(
    (
        "response_status_code",
        "response_payload",
        "expected_status",
        "expected_interval",
        "expected_exception",
    ),
    [
        (
            400,
            {"error": "authorization_pending"},
            OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING,
            7,
            None,
        ),
        (
            400,
            {"error": "slow_down"},
            OidcDeviceTokenPollingStatus.SLOW_DOWN,
            12,
            None,
        ),
        (
            400,
            {"error": "access_denied", "error_description": "operator denied"},
            None,
            None,
            OidcDeviceAuthorizationDeniedError,
        ),
        (
            400,
            {"error": "expired_token"},
            None,
            None,
            OidcDeviceAuthorizationExpiredError,
        ),
        (
            200,
            {
                "access_token": "access-token-123",
                "token_type": "bearer",
                "expires_in": 300,
                "refresh_token": "refresh-token-123",
            },
            OidcDeviceTokenPollingStatus.SUCCESS,
            7,
            None,
        ),
    ],
)
def test_poll_device_token_handles_oauth_responses(
    response_status_code: int,
    response_payload: dict[str, Any],
    expected_status: OidcDeviceTokenPollingStatus | None,
    expected_interval: int | None,
    expected_exception: type[Exception] | None,
) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        requests.append(_form_payload(request))
        return httpx.Response(response_status_code, json=response_payload)

    client = _client(handler)

    if expected_exception is not None:
        with pytest.raises(expected_exception):
            client.poll_device_token("device-code-123", interval=7)
    else:
        result = client.poll_device_token("device-code-123", interval=7)
        assert result.status == expected_status
        assert result.interval == expected_interval
        if expected_status == OidcDeviceTokenPollingStatus.SUCCESS:
            assert result.token_response is not None
            assert result.token_response.access_token_value == "access-token-123"
            assert result.token_response.refresh_token_value == "refresh-token-123"
        else:
            assert result.token_response is None

    assert requests == [
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
            "device_code": "device-code-123",
        }
    ]


def test_refresh_token_uses_public_client_form_body() -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        requests.append(_form_payload(request))
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "token_type": "Bearer",
                "expires_in": 240,
                "refresh_expires_in": 3600,
            },
        )

    client = _client(handler)

    response = client.refresh_token("refresh-token-123")

    assert response.access_token_value == "new-access-token"
    assert response.refresh_token_value == "new-refresh-token"
    assert response.token_type == "Bearer"
    assert requests == [
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": "refresh-token-123",
        }
    ]


def test_revoke_token_returns_structured_result_when_endpoint_missing() -> None:
    client = _client(
        lambda _: httpx.Response(
            200, json=_discovery_payload(include_revocation_endpoint=False)
        )
    )

    result = client.revoke_token("refresh-token-123")

    assert result.attempted is False
    assert result.revoked is False
    assert result.reason == "revocation_endpoint_unavailable"


def test_revoke_token_posts_without_client_secret() -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        requests.append(_form_payload(request))
        return httpx.Response(200, json={})

    client = _client(handler)

    result = client.revoke_token("refresh-token-123", token_type_hint="refresh_token")

    assert result.attempted is True
    assert result.revoked is True
    assert requests == [
        {
            "client_id": CLIENT_ID,
            "token": "refresh-token-123",
            "token_type_hint": "refresh_token",
        }
    ]


def test_require_https_rejects_non_https_issuer_by_default() -> None:
    client = _client(
        lambda _: httpx.Response(
            200,
            json=_discovery_payload(
                issuer="http://127.0.0.1:8080/realms/acps-registry"
            ),
        ),
        issuer="http://127.0.0.1:8080/realms/acps-registry",
    )

    with pytest.raises(OidcProviderUnavailableError, match="issuer must use https"):
        client.get_discovery_document()


def test_require_https_can_be_disabled_for_localhost() -> None:
    issuer = "http://127.0.0.1:8080/realms/acps-registry"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload(issuer=issuer))
        return httpx.Response(
            200,
            json={
                "device_code": "device-code-123",
                "user_code": "USER-CODE",
                "verification_uri": "http://127.0.0.1:8080/device",
                "expires_in": 600,
            },
        )

    client = _client(handler, issuer=issuer, require_https=False)

    response = client.start_device_authorization()

    assert response.user_code == "USER-CODE"


def test_public_client_requests_do_not_include_client_secret() -> None:
    bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        body = _form_payload(request)
        bodies.append(body)
        if request.url.path.endswith("/auth/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-code-123",
                    "user_code": "USER-CODE",
                    "verification_uri": "https://issuer.example/device",
                    "expires_in": 600,
                },
            )
        if request.url.path.endswith("/token"):
            grant_type = body["grant_type"]
            if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
                return httpx.Response(
                    200, json={"access_token": "access", "token_type": "bearer"}
                )
            return httpx.Response(
                200, json={"access_token": "access", "token_type": "Bearer"}
            )
        return httpx.Response(200, json={})

    client = _client(handler)

    client.start_device_authorization()
    client.poll_device_token("device-code-123")
    client.refresh_token("refresh-token-123")
    client.revoke_token("refresh-token-123")

    assert bodies
    assert all("client_secret" not in body for body in bodies)


def test_token_models_and_errors_redact_sensitive_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_discovery_payload())
        body = _form_payload(request)
        if body.get("grant_type") == "refresh_token":
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "refresh rejected",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "raw-access-token",
                "refresh_token": "raw-refresh-token",
                "token_type": "bearer",
            },
        )

    client = _client(handler)

    with pytest.raises(OidcClientError) as exc_info:
        client.refresh_token("raw-refresh-token")
    assert "raw-refresh-token" not in str(exc_info.value)
    assert "raw-refresh-token" not in repr(exc_info.value)

    token_response = client.poll_device_token("device-code-123").token_response
    assert token_response is not None
    assert "raw-access-token" not in repr(token_response)
    assert "raw-refresh-token" not in repr(token_response)
