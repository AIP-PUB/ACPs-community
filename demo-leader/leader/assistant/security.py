"""Authorization helpers for leader session ownership and group operations."""

from __future__ import annotations

from typing import Any

from acps_sdk.oidc import HumanPrincipal
from fastapi import HTTPException, status

from .auth import is_admin, is_operator


def ensure_session_owner(
    session: Any,
    principal: HumanPrincipal,
    *,
    allow_operator: bool = False,
) -> None:
    """Ensure the principal can access or manage the target session."""
    if getattr(session, "user_id", None) == principal.principal_id:
        return
    if allow_operator and (is_operator(principal) or is_admin(principal)):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You are not allowed to access this session",
    )


def can_manage_group(principal: HumanPrincipal) -> bool:
    """Return whether the principal can operate on group membership."""
    return is_operator(principal) or is_admin(principal)


def principal_to_user_context(principal: HumanPrincipal) -> dict[str, Any]:
    """Build a safe user context fragment derived from the principal."""
    return {
        "principalId": principal.principal_id,
        "username": principal.username,
        "name": principal.name,
        "email": principal.email,
        "tenantId": principal.tenant_id,
        "roles": list(principal.roles),
        "scopes": list(principal.scopes),
    }


def bind_session_principal(session: Any, principal: HumanPrincipal) -> None:
    """Persist the safe principal binding on the session aggregate."""
    session.user_id = principal.principal_id
    session.principal_issuer = principal.issuer
    session.principal_subject = principal.subject
    session.principal_username = principal.username
    session.principal_email = principal.email
    if not isinstance(getattr(session, "user_context", None), dict):
        session.user_context = {}
    session.user_context.setdefault("principal", principal_to_user_context(principal))
