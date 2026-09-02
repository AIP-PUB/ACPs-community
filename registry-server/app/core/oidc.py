from __future__ import annotations

from typing import Annotated

from acps_sdk.oidc import (
    HumanPrincipal,
    KeycloakClaimMapping,
    OidcProviderConfig,
    OidcTokenValidator,
)
from acps_sdk.oidc.errors import InvalidAccessTokenError, OidcProviderUnavailableError
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.account.exception_auth import OidcProviderUnavailableAuthError, TokenValidationError
from app.core.config import settings

_validator: OidcTokenValidator | None = None
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_str}/auth/login")


def oidc_enabled() -> bool:
    return settings.oidc_enabled


def build_oidc_provider_config() -> OidcProviderConfig:
    return OidcProviderConfig(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        allowed_azp=tuple(settings.oidc_allowed_azp),
        client_id=settings.oidc_client_id,
        algorithms=tuple(settings.oidc_algorithms),
        jwks_cache_ttl_seconds=settings.oidc_jwks_cache_ttl_seconds,
        discovery_cache_ttl_seconds=settings.oidc_discovery_cache_ttl_seconds,
        leeway_seconds=settings.oidc_leeway_seconds,
        require_https=settings.oidc_require_https,
        claim_mapping=KeycloakClaimMapping(resource_client_id=settings.oidc_role_source_client_id),
    )


async def init_oidc_validator() -> None:
    global _validator

    if not oidc_enabled():
        return
    if _validator is None:
        _validator = OidcTokenValidator(config=build_oidc_provider_config())


async def close_oidc_validator() -> None:
    global _validator

    if _validator is not None:
        await _validator.close()
        _validator = None


def _get_validator() -> OidcTokenValidator:
    if _validator is None:
        raise OidcProviderUnavailableAuthError()
    return _validator


async def validate_access_token(token: str) -> HumanPrincipal:
    try:
        return await _get_validator().validate_access_token(token)
    except InvalidAccessTokenError as exc:
        raise TokenValidationError() from exc
    except OidcProviderUnavailableError as exc:
        raise OidcProviderUnavailableAuthError() from exc


async def validate_optional_access_token(token: str | None) -> HumanPrincipal | None:
    if not token:
        return None
    try:
        return await _get_validator().validate_access_token(token)
    except InvalidAccessTokenError, OidcProviderUnavailableError:
        return None


async def get_current_principal(token: str = Depends(_oauth2_scheme)) -> HumanPrincipal:
    return await validate_access_token(token)


def get_optional_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def get_optional_principal(
    token: Annotated[str | None, Depends(get_optional_token)],
) -> HumanPrincipal | None:
    return await validate_optional_access_token(token)


def get_keycloak_end_session_endpoint() -> str:
    return f"{settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/logout"
