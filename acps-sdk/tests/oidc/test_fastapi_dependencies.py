from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from acps_sdk.oidc import KeycloakClaimMapping, OidcProviderConfig, OidcTokenValidator, optional_principal, require_principal, require_roles


ISSUER = "https://issuer.example/realms/acps-registry"
AUDIENCE = "registry-api"


def _b64url(data: bytes) -> str:
    return jwt.utils.base64url_encode(data).decode()


def _validator(*, transport: httpx.BaseTransport) -> OidcTokenValidator:
    return OidcTokenValidator(
        config=OidcProviderConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            allowed_azp=("registry-web",),
            algorithms=("EdDSA",),
            claim_mapping=KeycloakClaimMapping(resource_client_id=AUDIENCE),
        ),
        http_client=httpx.AsyncClient(transport=transport),
    )


def _token_and_transport(*, roles: list[str]) -> tuple[str, httpx.MockTransport]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid,
        "alg": "EdDSA",
        "use": "sig",
        "x": _b64url(public_raw),
    }
    now = datetime.now(tz=timezone.utc)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "user-123",
            "aud": AUDIENCE,
            "azp": "registry-web",
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "nbf": now,
            "preferred_username": "alice",
            "scope": "account:read",
            "resource_access": {AUDIENCE: {"roles": roles}},
        },
        key=private_key,
        algorithm="EdDSA",
        headers={"kid": kid},
    )

    discovery = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
    jwks = {"keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return token, httpx.MockTransport(handler)


def _broken_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    return httpx.MockTransport(handler)


def _app(validator: OidcTokenValidator) -> FastAPI:
    app = FastAPI()
    principal_dep = require_principal(validator)
    admin_dep = require_roles(validator, ["ADMIN"])
    optional_dep = optional_principal(validator)

    @app.get("/principal")
    async def principal_endpoint(principal=Depends(principal_dep)) -> dict[str, Any]:
        return {"principalId": principal.principal_id, "roles": list(principal.roles)}

    @app.get("/admin")
    async def admin_endpoint(principal=Depends(admin_dep)) -> dict[str, Any]:
        return {"principalId": principal.principal_id}

    @app.get("/optional")
    async def optional_endpoint(principal=Depends(optional_dep)) -> dict[str, Any]:
        return {"authenticated": principal is not None}

    return app


def test_require_principal_returns_401_when_token_missing() -> None:
    _, transport = _token_and_transport(roles=["USER"])
    validator = _validator(transport=transport)
    client = TestClient(_app(validator))

    response = client.get("/principal")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_require_roles_returns_403_when_role_missing() -> None:
    token, transport = _token_and_transport(roles=["USER"])
    validator = _validator(transport=transport)
    client = TestClient(_app(validator))

    response = client.get("/admin", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert "Missing required roles" in response.text


def test_principal_dependency_injects_authenticated_principal() -> None:
    token, transport = _token_and_transport(roles=["USER", "ADMIN"])
    validator = _validator(transport=transport)
    client = TestClient(_app(validator))

    response = client.get("/principal", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["roles"] == ["USER", "ADMIN"]


def test_optional_principal_returns_none_when_token_invalid() -> None:
    _, transport = _token_and_transport(roles=["USER"])
    validator = _validator(transport=transport)
    client = TestClient(_app(validator))

    response = client.get("/optional", headers={"Authorization": "Bearer invalid-token"})

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_dependency_returns_503_when_discovery_or_jwks_unavailable() -> None:
    token, _ = _token_and_transport(roles=["USER"])
    validator = _validator(transport=_broken_transport())
    client = TestClient(_app(validator))

    response = client.get("/principal", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
