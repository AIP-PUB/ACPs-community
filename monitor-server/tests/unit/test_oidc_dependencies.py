"""OIDC dependency tests for monitor-server."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from typing import Any, cast

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from acps_sdk.oidc.errors import InvalidAccessTokenError, OidcProviderUnavailableError
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import oidc as oidc_module
from app.core.config import settings


def _principal() -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-monitor"
    subject = "monitor-user-001"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("monitor-api",),
        azp="monitor-web",
        username="monitor-user",
        name="Monitor User",
        email="monitor@example.com",
        roles=("viewer",),
        scopes=("system:read",),
        allowed_aics=("aic-001",),
        raw_claims={"sub": subject},
    )


class StubValidator:
    async def validate_access_token(self, token: str) -> HumanPrincipal:
        if token == "good-token":
            return _principal()
        if token == "provider-down":
            raise OidcProviderUnavailableError("JWKS unavailable")
        if token == "wrong-azp":
            raise InvalidAccessTokenError("Unexpected azp")
        if token == "id-token":
            raise InvalidAccessTokenError("Audience does not include monitor-api")
        if token == "cross-realm":
            raise InvalidAccessTokenError("Unexpected issuer")
        raise InvalidAccessTokenError("Invalid bearer token")


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/me", response_model=HumanPrincipal)
    async def me(principal: HumanPrincipal = Depends(oidc_module.get_request_principal)) -> HumanPrincipal:
        return principal

    return app


@pytest.fixture(autouse=True)
def _restore_state() -> Generator[None]:
    original_toml = deepcopy(settings._toml)
    original_validator = oidc_module._validator
    try:
        yield
    finally:
        settings._toml = original_toml
        oidc_module._validator = original_validator


def _enable_oidc() -> None:
    settings._toml.setdefault("oidc", {})["enabled"] = True


def test_missing_bearer_token_returns_401_when_oidc_enabled() -> None:
    _enable_oidc()
    cast("Any", oidc_module)._validator = StubValidator()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("token", ["wrong-azp", "id-token", "cross-realm"])
def test_invalid_project_tokens_return_401(token: str) -> None:
    _enable_oidc()
    cast("Any", oidc_module)._validator = StubValidator()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_provider_unavailable_returns_503() -> None:
    _enable_oidc()
    cast("Any", oidc_module)._validator = StubValidator()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.get("/me", headers={"Authorization": "Bearer provider-down"})

    assert response.status_code == 503
    assert response.json()["detail"] == "JWKS unavailable"


def test_valid_token_resolves_principal_without_internal_claim_fields() -> None:
    _enable_oidc()
    cast("Any", oidc_module)._validator = StubValidator()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.get("/me", headers={"Authorization": "Bearer good-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == build_principal_id(
        issuer="https://keycloak.example.com/realms/acps-monitor",
        subject="monitor-user-001",
    )
    assert "subject" not in body
    assert "principal_key" not in body
    assert "raw_claims" not in body
