"""OIDC authentication helpers for leader human-facing APIs."""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
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

from .config import settings

STREAM_TOKEN_TTL_SECONDS = 90

_bearer = HTTPBearer(auto_error=False)
_validator: OidcTokenValidator | None = None


def oidc_enabled() -> bool:
    """Return whether leader OIDC protection is enabled."""
    return bool(settings.get("oidc", {}).get("enabled", False))


def _role_names(config_key: str) -> set[str]:
    values = settings.get("authorization", {}).get(config_key, [])
    if isinstance(values, str):
        return {values} if values else set()
    if isinstance(values, Iterable):
        return {str(item).strip() for item in values if str(item).strip()}
    return set()


def is_admin(principal: HumanPrincipal) -> bool:
    """Return whether the principal carries a configured admin role."""
    return bool(_role_names("admin_roles") & set(principal.roles))


def is_operator(principal: HumanPrincipal) -> bool:
    """Return whether the principal carries a configured operator role."""
    return bool(_role_names("operator_roles") & set(principal.roles))


def _build_provider_config() -> OidcProviderConfig:
    oidc_config = settings.get("oidc", {})
    role_source_client_id = oidc_config.get("role_source_client_id") or oidc_config.get("client_id")
    claim_mapping = KeycloakClaimMapping(resource_client_id=role_source_client_id)
    return OidcProviderConfig(
        issuer=str(oidc_config.get("issuer", "")),
        audience=str(oidc_config.get("audience", "leader-api")),
        allowed_azp=oidc_config.get("allowed_azp", []),
        client_id=oidc_config.get("client_id"),
        algorithms=oidc_config.get("algorithms", ["EdDSA"]),
        jwks_cache_ttl_seconds=int(oidc_config.get("jwks_cache_ttl_seconds", 300)),
        discovery_cache_ttl_seconds=int(oidc_config.get("discovery_cache_ttl_seconds", 300)),
        leeway_seconds=int(oidc_config.get("leeway_seconds", 60)),
        require_https=bool(oidc_config.get("require_https", True)),
        claim_mapping=claim_mapping,
    )


async def init_auth() -> None:
    """Initialize the OIDC validator if human auth is enabled."""
    global _validator
    if not oidc_enabled() or _validator is not None:
        return
    _validator = OidcTokenValidator(config=_build_provider_config())


async def close_auth() -> None:
    """Close OIDC resources and discard short-lived stream tokens."""
    global _validator
    if _validator is not None:
        await _validator.close()
        _validator = None
    _stream_tokens.clear()


def _get_validator() -> OidcTokenValidator:
    if _validator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC validator is not initialized",
        )
    return _validator


async def get_request_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> HumanPrincipal | None:
    """Return the authenticated principal when OIDC is enabled, else None."""
    if not oidc_enabled():
        return None
    dependency = sdk_require_principal(_get_validator())
    return await dependency(credentials)


async def require_leader_user(
    principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
) -> HumanPrincipal | None:
    """Require a leader user principal when OIDC is enabled."""
    if principal is None:
        return None
    if principal.has_scope("leader:submit"):
        return principal
    if _role_names("user_roles") & set(principal.roles):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing leader user role or leader:submit scope",
    )


async def require_operator(
    principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
) -> HumanPrincipal | None:
    """Require an operator-level principal when OIDC is enabled."""
    if principal is None:
        return None
    if is_operator(principal):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing operator role",
    )


async def require_admin(
    principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
) -> HumanPrincipal | None:
    """Require an admin principal when OIDC is enabled."""
    if principal is None:
        return None
    if is_admin(principal):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing admin role",
    )


@dataclass(slots=True)
class StreamTokenRecord:
    session_id: str
    principal_id: str
    expires_at: float
    allow_elevated_access: bool = False


class StreamTokenStore:
    """In-memory short-lived tokens for browser streaming endpoints."""

    def __init__(self) -> None:
        self._tokens: dict[str, StreamTokenRecord] = {}

    def issue(
        self,
        *,
        session_id: str,
        principal_id: str,
        ttl_seconds: int = STREAM_TOKEN_TTL_SECONDS,
        allow_elevated_access: bool = False,
    ) -> tuple[str, int]:
        self._cleanup()
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl_seconds
        self._tokens[token] = StreamTokenRecord(
            session_id=session_id,
            principal_id=principal_id,
            expires_at=expires_at,
            allow_elevated_access=allow_elevated_access,
        )
        return token, int(expires_at)

    def validate(self, *, session_id: str, token: str) -> StreamTokenRecord | None:
        self._cleanup()
        record = self._tokens.get(token)
        if record is None or record.session_id != session_id:
            return None
        return record

    def clear(self) -> None:
        self._tokens.clear()

    def _cleanup(self) -> None:
        now = time.time()
        expired = [token for token, record in self._tokens.items() if record.expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)


_stream_tokens = StreamTokenStore()


def issue_stream_token(
    *,
    session_id: str,
    principal: HumanPrincipal,
    allow_elevated_access: bool = False,
) -> tuple[str, int]:
    """Issue a short-lived stream token for a specific session."""
    return _stream_tokens.issue(
        session_id=session_id,
        principal_id=principal.principal_id,
        allow_elevated_access=allow_elevated_access,
    )


def validate_stream_token(*, session_id: str, token: str) -> StreamTokenRecord | None:
    """Validate a stream token bound to a specific session."""
    return _stream_tokens.validate(session_id=session_id, token=token)
