"""基于 OIDC discovery 与 JWKS 的 access token 校验器。"""

from __future__ import annotations

from typing import Any

import httpx
import jwt

from acps_sdk.oidc.config import OidcProviderConfig
from acps_sdk.oidc.discovery import OidcDiscoveryCache
from acps_sdk.oidc.errors import InvalidAccessTokenError
from acps_sdk.oidc.jwks import JwksCache
from acps_sdk.oidc.keycloak import claims_to_principal
from acps_sdk.oidc.principal import HumanPrincipal


class OidcTokenValidator:
    """为单个 Resource Server 校验 OIDC access token。"""

    def __init__(
        self,
        *,
        config: OidcProviderConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=config.http_timeout_seconds)
        self._discovery = OidcDiscoveryCache(
            ttl_seconds=config.discovery_cache_ttl_seconds,
            require_https=config.require_https,
            http_client=self._http_client,
        )
        self._jwks = JwksCache(
            ttl_seconds=config.jwks_cache_ttl_seconds,
            require_https=config.require_https,
            http_client=self._http_client,
        )

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def validate_access_token(self, token: str) -> HumanPrincipal:
        """校验 bearer access token，并返回规范化后的 principal。"""

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidAccessTokenError("invalid JWT header") from exc

        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or not algorithm:
            raise InvalidAccessTokenError("JWT header alg is required")
        if algorithm not in self.config.algorithms:
            raise InvalidAccessTokenError(f"JWT algorithm {algorithm!r} is not allowed")
        if algorithm == "none" or algorithm.startswith("HS"):
            raise InvalidAccessTokenError(f"JWT algorithm {algorithm!r} is not allowed")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidAccessTokenError("JWT header kid is required")

        discovery = await self._discovery.get(self.config.issuer)
        jwk = await self._jwks.get_jwk(jwks_uri=discovery.jwks_uri, kid=kid)
        self._validate_jwk_metadata(jwk=jwk, algorithm=algorithm, kid=kid)

        try:
            jwt_key = jwt.PyJWK.from_dict(jwk).key
        except jwt.PyJWTError as exc:
            raise InvalidAccessTokenError(f"failed to construct JWK for kid {kid}") from exc

        options = {
            "require": ["exp", "iss", "aud"],
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_nbf": True,
            "verify_iss": True,
            "verify_aud": True,
        }
        try:
            claims = jwt.decode(
                token,
                key=jwt_key,
                algorithms=[algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options=options,
            )
        except jwt.PyJWTError as exc:
            raise InvalidAccessTokenError(str(exc)) from exc

        if not isinstance(claims, dict):
            raise InvalidAccessTokenError("JWT payload must be a JSON object")
        self._validate_authorized_party(claims)
        try:
            return claims_to_principal(claims=claims, claim_mapping=self.config.claim_mapping)
        except (TypeError, ValueError) as exc:
            raise InvalidAccessTokenError(str(exc)) from exc

    def _validate_authorized_party(self, claims: dict[str, Any]) -> None:
        if not self.config.allowed_azp:
            return
        azp = claims.get("azp")
        if not isinstance(azp, str) or azp not in self.config.allowed_azp:
            allowed = ", ".join(self.config.allowed_azp)
            raise InvalidAccessTokenError(f"azp must be one of: {allowed}")

    @staticmethod
    def _validate_jwk_metadata(*, jwk: dict[str, Any], algorithm: str, kid: str) -> None:
        jwk_alg = jwk.get("alg")
        if jwk_alg is not None and jwk_alg != algorithm:
            raise InvalidAccessTokenError(f"JWK alg mismatch for kid {kid}")
        jwk_use = jwk.get("use")
        if jwk_use is not None and jwk_use != "sig":
            raise InvalidAccessTokenError(f"JWK use mismatch for kid {kid}")

        kty = jwk.get("kty")
        crv = jwk.get("crv")
        if algorithm == "EdDSA":
            if kty != "OKP" or crv != "Ed25519":
                raise InvalidAccessTokenError("EdDSA access tokens require OKP/Ed25519 JWKs")
            return
        if algorithm == "ES256":
            if kty != "EC" or crv != "P-256":
                raise InvalidAccessTokenError("ES256 access tokens require EC/P-256 JWKs")
            return
        if algorithm in {"PS256", "RS256"}:
            if kty != "RSA":
                raise InvalidAccessTokenError(f"{algorithm} access tokens require RSA JWKs")
            return
        raise InvalidAccessTokenError(f"unsupported JWT algorithm {algorithm!r}")
