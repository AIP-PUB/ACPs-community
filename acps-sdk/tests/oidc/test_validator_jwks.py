from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from acps_sdk.oidc import KeycloakClaimMapping, OidcProviderConfig, OidcTokenValidator
from acps_sdk.oidc.errors import InvalidAccessTokenError, OidcProviderUnavailableError


ISSUER = "https://issuer.example/realms/acps-leader"
AUDIENCE = "leader-api"
ALLOWED_AZP = ("leader-web",)


def _b64url(data: bytes) -> str:
    return jwt.utils.base64url_encode(data).decode()


def _ed25519_jwk(public_key: ed25519.Ed25519PublicKey, *, kid: str, alg: str | None = "EdDSA") -> dict[str, Any]:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk: dict[str, Any] = {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid,
        "use": "sig",
        "x": _b64url(raw),
    }
    if alg is not None:
        jwk["alg"] = alg
    return jwk


def _ec_p256_jwk(public_key: ec.EllipticCurvePublicKey, *, kid: str, alg: str | None = "ES256") -> dict[str, Any]:
    numbers = public_key.public_numbers()
    size = 32
    jwk: dict[str, Any] = {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "use": "sig",
        "x": _b64url(numbers.x.to_bytes(size, "big")),
        "y": _b64url(numbers.y.to_bytes(size, "big")),
    }
    if alg is not None:
        jwk["alg"] = alg
    return jwk


def _rsa_jwk(
    public_key: rsa.RSAPublicKey,
    *,
    kid: str,
    alg: str | None,
) -> dict[str, Any]:
    numbers = public_key.public_numbers()
    jwk: dict[str, Any] = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    if alg is not None:
        jwk["alg"] = alg
    return jwk


def _make_claims(
    *,
    audience: str | list[str] = AUDIENCE,
    azp: str = "leader-web",
    issuer: str = ISSUER,
    expires_delta: timedelta = timedelta(minutes=5),
    issued_at_delta: timedelta = timedelta(seconds=0),
    nbf_delta: timedelta = timedelta(seconds=0),
    drop: set[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": "user-123",
        "aud": audience,
        "azp": azp,
        "exp": now + expires_delta,
        "iat": now + issued_at_delta,
        "nbf": now + nbf_delta,
        "preferred_username": "alice",
        "name": "Alice",
        "email": "alice@example.com",
        "email_verified": True,
        "scope": "session:read session:write",
        "tenant_id": "tenant-1",
        "allowed_aics": ["aic-1", "aic-2"],
        "resource_access": {AUDIENCE: {"roles": ["USER", "ADMIN"]}},
        "realm_access": {"roles": ["REALM_ADMIN"]},
        "groups": ["/leaders", "/operators"],
    }
    if drop:
        for key in drop:
            claims.pop(key, None)
    if extra:
        claims.update(extra)
    return claims


def _encode_token(
    *,
    private_key: Any,
    algorithm: str,
    kid: str,
    claims: dict[str, Any],
) -> str:
    return jwt.encode(claims, key=private_key, algorithm=algorithm, headers={"kid": kid})


def _validator(
    *,
    algorithm: str = "EdDSA",
    jwk: dict[str, Any],
    allowed_algorithms: tuple[str, ...] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> OidcTokenValidator:
    transport = transport or _transport_for_jwks(jwk)
    http_client = httpx.AsyncClient(transport=transport)
    return OidcTokenValidator(
        config=OidcProviderConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            allowed_azp=ALLOWED_AZP,
            algorithms=allowed_algorithms or (algorithm,),
            claim_mapping=KeycloakClaimMapping(resource_client_id=AUDIENCE),
        ),
        http_client=http_client,
    )


def _transport_for_jwks(jwk: dict[str, Any]) -> httpx.MockTransport:
    discovery = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
    jwks = {"keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_validator_accepts_valid_eddsa_token() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())
    validator = _validator(jwk=jwk)

    principal = await validator.validate_access_token(token)

    assert principal.principal_id
    assert principal.audiences == (AUDIENCE,)
    assert principal.azp == "leader-web"
    assert principal.roles == ("USER", "ADMIN")
    assert principal.groups == ("/leaders", "/operators")
    assert principal.scopes == ("session:read", "session:write")
    assert principal.allowed_aics == ("aic-1", "aic-2")
    await validator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_overrides", "drop_claims", "message"),
    [
        ({"iss": "https://other.example/realms/acps-leader"}, set(), "Invalid issuer"),
        ({"aud": "other-api"}, set(), "Audience doesn't match"),
        ({}, {"exp"}, 'Token is missing the "exp" claim'),
        ({}, set(), "unknown JWK kid"),
    ],
)
async def test_validator_rejects_basic_claim_failures(
    claim_overrides: dict[str, Any],
    drop_claims: set[str],
    message: str,
) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    jwk = _ed25519_jwk(private_key.public_key(), kid="eddsa-kid")
    token_kid = "eddsa-kid" if message != "unknown JWK kid" else "other-kid"
    token = _encode_token(
        private_key=private_key,
        algorithm="EdDSA",
        kid=token_kid,
        claims=_make_claims(drop=drop_claims, extra=claim_overrides),
    )
    validator = _validator(jwk=jwk)

    with pytest.raises(InvalidAccessTokenError, match=message):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_overrides", "message"),
    [
        ({"iat": datetime.now(tz=timezone.utc) + timedelta(minutes=10)}, "The token is not yet valid"),
        ({"nbf": datetime.now(tz=timezone.utc) + timedelta(minutes=10)}, "The token is not yet valid"),
        ({"exp": datetime.now(tz=timezone.utc) - timedelta(minutes=1)}, "Signature has expired"),
    ],
)
async def test_validator_rejects_time_based_claim_failures(
    claim_overrides: dict[str, Any],
    message: str,
) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(
        private_key=private_key,
        algorithm="EdDSA",
        kid=kid,
        claims=_make_claims(extra=claim_overrides),
    )
    validator = _validator(jwk=jwk)

    with pytest.raises(InvalidAccessTokenError, match=message):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
async def test_validator_rejects_wrong_authorized_party() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(
        private_key=private_key,
        algorithm="EdDSA",
        kid=kid,
        claims=_make_claims(extra={"azp": "other-web"}),
    )
    validator = _validator(jwk=jwk)

    with pytest.raises(InvalidAccessTokenError, match="azp must be one of"):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", [AUDIENCE, [AUDIENCE, "account"]])
async def test_validator_accepts_string_and_array_audiences(audience: str | list[str]) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(
        private_key=private_key,
        algorithm="EdDSA",
        kid=kid,
        claims=_make_claims(audience=audience),
    )
    validator = _validator(jwk=jwk)

    principal = await validator.validate_access_token(token)

    expected = (AUDIENCE,) if isinstance(audience, str) else (AUDIENCE, "account")
    assert principal.audiences == expected
    await validator.close()


@pytest.mark.asyncio
async def test_validator_rejects_id_token_style_bearer_token() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(
        private_key=private_key,
        algorithm="EdDSA",
        kid=kid,
        claims=_make_claims(audience="leader-web"),
    )
    validator = _validator(jwk=jwk)

    with pytest.raises(InvalidAccessTokenError, match="Audience doesn't match"):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
async def test_validator_rejects_disallowed_header_algorithm() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())
    validator = _validator(jwk=jwk, allowed_algorithms=("ES256",))

    with pytest.raises(InvalidAccessTokenError, match="is not allowed"):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("jwk_patch", "message"),
    [
        ({"kty": "EC"}, "EdDSA access tokens require OKP/Ed25519 JWKs"),
        ({"crv": "Ed448"}, "EdDSA access tokens require OKP/Ed25519 JWKs"),
        ({"alg": "ES256"}, "JWK alg mismatch"),
        ({"use": "enc"}, "JWK use mismatch"),
    ],
)
async def test_validator_rejects_jwk_metadata_mismatches(
    jwk_patch: dict[str, Any],
    message: str,
) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    jwk.update(jwk_patch)
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())
    validator = _validator(jwk=jwk)

    with pytest.raises(InvalidAccessTokenError, match=message):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
async def test_validator_accepts_jwk_without_optional_alg_and_use() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid, alg=None)
    jwk.pop("use")
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())
    validator = _validator(jwk=jwk)

    principal = await validator.validate_access_token(token)

    assert principal.username == "alice"
    await validator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("algorithm", "private_key_factory", "jwk_factory"),
    [
        ("ES256", lambda: ec.generate_private_key(ec.SECP256R1()), _ec_p256_jwk),
        ("PS256", lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048), lambda pub, kid: _rsa_jwk(pub, kid=kid, alg="PS256")),
        ("RS256", lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048), lambda pub, kid: _rsa_jwk(pub, kid=kid, alg="RS256")),
    ],
)
async def test_validator_supports_explicit_fallback_algorithms(
    algorithm: str,
    private_key_factory: Any,
    jwk_factory: Any,
) -> None:
    private_key = private_key_factory()
    kid = f"{algorithm.lower()}-kid"
    jwk = jwk_factory(private_key.public_key(), kid=kid)
    token = _encode_token(private_key=private_key, algorithm=algorithm, kid=kid, claims=_make_claims())
    validator = _validator(algorithm=algorithm, jwk=jwk)

    principal = await validator.validate_access_token(token)

    assert principal.email == "alice@example.com"
    await validator.close()


@pytest.mark.asyncio
async def test_validator_rejects_invalid_discovery_json() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, text="{broken-json")
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(404, json={"detail": "not found"})

    validator = _validator(jwk=jwk, transport=httpx.MockTransport(handler))

    with pytest.raises(OidcProviderUnavailableError, match="is not valid JSON"):
        await validator.validate_access_token(token)

    await validator.close()


@pytest.mark.asyncio
async def test_validator_rejects_invalid_jwks_json() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    kid = "eddsa-kid"
    jwk = _ed25519_jwk(private_key.public_key(), kid=kid)
    token = _encode_token(private_key=private_key, algorithm="EdDSA", kid=kid, claims=_make_claims())
    discovery = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=discovery)
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, text="{broken-json")
        return httpx.Response(404, json={"detail": "not found"})

    validator = _validator(jwk=jwk, transport=httpx.MockTransport(handler))

    with pytest.raises(OidcProviderUnavailableError, match="is not valid JSON"):
        await validator.validate_access_token(token)

    await validator.close()
