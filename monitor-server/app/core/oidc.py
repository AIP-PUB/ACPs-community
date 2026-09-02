"""OIDC validator lifecycle and FastAPI principal dependency."""

from __future__ import annotations

from typing import Annotated

from acps_sdk.oidc import (
    HumanPrincipal,
    KeycloakClaimMapping,
    OidcProviderConfig,
    OidcTokenValidator,
)
from acps_sdk.oidc import (
    require_principal as sdk_require_principal,
)
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

_bearer = HTTPBearer(auto_error=False)
_validator: OidcTokenValidator | None = None


def oidc_enabled() -> bool:
    """Return whether monitor-server OIDC protection is enabled."""
    return settings.oidc_enabled


def _build_provider_config() -> OidcProviderConfig:
    return OidcProviderConfig(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        allowed_azp=settings.oidc_allowed_azp,
        client_id=settings.oidc_client_id,
        algorithms=settings.oidc_algorithms,
        jwks_cache_ttl_seconds=settings.oidc_jwks_cache_ttl_seconds,
        discovery_cache_ttl_seconds=settings.oidc_discovery_cache_ttl_seconds,
        leeway_seconds=settings.oidc_leeway_seconds,
        require_https=settings.oidc_require_https,
        claim_mapping=KeycloakClaimMapping(resource_client_id=settings.oidc_role_source_client_id),
    )


async def init_oidc() -> None:
    """Initialize the shared OIDC validator when enabled."""
    global _validator
    if not oidc_enabled() or _validator is not None:
        return
    _validator = OidcTokenValidator(config=_build_provider_config())


async def close_oidc() -> None:
    """Release the shared OIDC validator."""
    global _validator
    if _validator is not None:
        await _validator.close()
        _validator = None


def _get_validator() -> OidcTokenValidator:
    if _validator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC validator is not initialized",
        )
    return _validator


async def resolve_bearer_principal(
    credentials: HTTPAuthorizationCredentials | None,
) -> HumanPrincipal:
    """Validate Bearer credentials as an OIDC human principal (OIDC must be enabled)."""
    dependency = sdk_require_principal(_get_validator())
    return await dependency(credentials)


async def get_request_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> HumanPrincipal | None:
    """Resolve the current human principal when OIDC is enabled."""
    if not oidc_enabled():
        return None
    return await resolve_bearer_principal(credentials)
