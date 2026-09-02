"""Authorization and scope-filter helpers for monitor query APIs."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from acps_sdk.oidc import HumanPrincipal
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
from app.core.config import settings
from app.core.oidc import get_request_principal, oidc_enabled, resolve_bearer_principal

_bearer = HTTPBearer(auto_error=False)


def _role_set(values: Iterable[str]) -> set[str]:
    return {str(item).strip() for item in values if str(item).strip()}


def _has_any_role(principal: HumanPrincipal, allowed_roles: Iterable[str]) -> bool:
    return bool(set(principal.roles) & _role_set(allowed_roles))


@dataclass(frozen=True)
class PrincipalScope:
    tenant_id: str | None
    allowed_aics: tuple[str, ...]
    is_admin: bool


def _require_aic_scope(principal: HumanPrincipal | None) -> set[str] | None:
    """解析 principal 的 AIC 作用域；无法从当前 principal 推导时返回 403。"""
    scope = principal_scope_filter(principal)
    if scope.is_admin:
        return None
    if scope.allowed_aics:
        return set(scope.allowed_aics)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Request scope cannot be derived for this principal",
    )


def principal_scope_filter(principal: HumanPrincipal | None) -> PrincipalScope:
    """Resolve the principal's query scope or reject unscoped non-admin access."""
    if principal is None or not oidc_enabled():
        return PrincipalScope(tenant_id=None, allowed_aics=(), is_admin=True)

    is_admin = _has_any_role(principal, settings.authorization_global_admin_roles)
    if is_admin:
        return PrincipalScope(tenant_id=principal.tenant_id, allowed_aics=principal.allowed_aics, is_admin=True)

    if principal.tenant_id or principal.allowed_aics:
        return PrincipalScope(
            tenant_id=principal.tenant_id,
            allowed_aics=tuple(principal.allowed_aics),
            is_admin=False,
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Principal has no tenant_id or allowed_aics scope",
    )


def require_read(scope: str) -> Callable[..., Awaitable[HumanPrincipal | None]]:
    """Require a read scope or one of the configured default read roles."""

    async def dependency(
        principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
    ) -> HumanPrincipal | None:
        if principal is None:
            return None
        if principal.has_scope(scope) or _has_any_role(principal, settings.authorization_default_read_roles):
            principal_scope_filter(principal)
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing {scope} scope or default read role",
        )

    return dependency


def require_auditor(scope: str) -> Callable[..., Awaitable[HumanPrincipal | None]]:
    """Require a privileged audit/export scope or auditor/admin role."""

    async def dependency(
        principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
    ) -> HumanPrincipal | None:
        if principal is None:
            return None
        if principal.has_scope(scope) or _has_any_role(principal, settings.authorization_global_auditor_roles):
            principal_scope_filter(principal)
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing {scope} scope or auditor role",
        )

    return dependency


async def require_operator(
    principal: Annotated[HumanPrincipal | None, Depends(get_request_principal)],
) -> HumanPrincipal | None:
    """Require an operator/admin principal when OIDC is enabled."""
    if principal is None:
        return None
    if _has_any_role(principal, settings.authorization_global_operator_roles):
        principal_scope_filter(principal)
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing operator role",
    )


def _matches_sync_internal_token(credentials: HTTPAuthorizationCredentials | None) -> bool:
    """Return True when Bearer matches the Monitor↔Discovery sync service token."""
    configured = settings.heartbeat_sync_internal_token.strip()
    if not configured or credentials is None or credentials.scheme.lower() != "bearer":
        return False
    provided = credentials.credentials.strip()
    if not provided:
        return False
    return secrets.compare_digest(provided, configured)


async def require_sync_access(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> HumanPrincipal | None:
    """Authorize Heartbeat Sync API (/sync/info, /sync/snapshot).

    Paths (H-compatible with Keycloak):
    1. Shared internal Bearer (`HEARTBEAT_SYNC_INTERNAL_TOKEN`) — service path for
       discovery-server alive-sync bootstrap (works with OIDC on or off).
    2. OIDC off — unauthenticated access (local-auth / default matrix).
    3. OIDC on — human operator Bearer (same roles as ``require_operator``).
    """
    if _matches_sync_internal_token(credentials):
        return None

    if not oidc_enabled():
        return None

    principal = await resolve_bearer_principal(credentials)
    if _has_any_role(principal, settings.authorization_global_operator_roles):
        principal_scope_filter(principal)
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
    if _has_any_role(principal, settings.authorization_global_admin_roles):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing admin role",
    )


def apply_request_scope(
    request: Any,
    principal: HumanPrincipal | None,
    *,
    aic_field: str | None = "aic",
    tenant_field: str | None = None,
) -> Any:
    """Inject tenant/aic scope conditions into a request model with a generic AMPFilter."""
    scope = principal_scope_filter(principal)
    if scope.is_admin:
        return request

    conditions = list(getattr(getattr(request, "filter", None), "conditions", None) or [])
    if tenant_field and scope.tenant_id:
        conditions.append(AMPFilterCondition(field=tenant_field, op="eq", value=scope.tenant_id))
    if aic_field and scope.allowed_aics:
        if len(scope.allowed_aics) == 1:
            conditions.append(AMPFilterCondition(field=aic_field, op="eq", value=scope.allowed_aics[0]))
        else:
            conditions.append(AMPFilterCondition(field=aic_field, op="in", value=list(scope.allowed_aics)))

    if not conditions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request scope cannot be derived for this principal",
        )

    filter_model = getattr(request, "filter", None)
    if isinstance(filter_model, AMPFilter):
        scoped_filter = filter_model.model_copy(update={"conditions": conditions})
    else:
        scoped_filter = AMPFilter(conditions=conditions, logic="and")
    return request.model_copy(update={"filter": scoped_filter})


def ensure_path_aic_allowed(aic: str, principal: HumanPrincipal | None) -> None:
    """Reject direct AIC path access outside the principal scope."""
    allowed_aics = _require_aic_scope(principal)
    if allowed_aics is None or aic in allowed_aics:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="AIC is outside the principal scope",
    )


def ensure_trace_view_allowed(view: Any, principal: HumanPrincipal | None) -> None:
    """Reject trace detail responses outside the principal AIC scope."""
    allowed_aics = _require_aic_scope(principal)
    if allowed_aics is None:
        return
    root_aic = getattr(getattr(view, "summary", None), "root_aic", None)
    if root_aic and root_aic in allowed_aics:
        return
    span_aics = {getattr(span, "aic", None) for span in getattr(view, "spans", [])}
    if span_aics & allowed_aics:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Trace is outside the principal scope",
    )


def ensure_any_aic_allowed(values: Iterable[str], principal: HumanPrincipal | None) -> None:
    """Reject responses whose AIC set does not overlap with the principal scope."""
    allowed_aics = _require_aic_scope(principal)
    if allowed_aics is None:
        return
    if set(values) & allowed_aics:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Resource is outside the principal scope",
    )
