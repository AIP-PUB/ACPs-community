"""面向 OIDC bearer authentication 的 FastAPI 依赖辅助工具。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from acps_sdk.amp import AuditActor
from acps_sdk.oidc.errors import InvalidAccessTokenError, OidcProviderUnavailableError
from acps_sdk.oidc.principal import HumanPrincipal

if TYPE_CHECKING:
    from acps_sdk.oidc.validator import OidcTokenValidator


_bearer = HTTPBearer(auto_error=False)


def require_principal(
    validator: OidcTokenValidator,
) -> Callable[[HTTPAuthorizationCredentials | None], Awaitable[HumanPrincipal]]:
    """返回一个要求请求中必须携带有效真人 principal 的依赖。"""

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> HumanPrincipal:
        token = _extract_bearer_token(credentials)
        if token is None:
            raise _unauthorized("Missing bearer token")
        try:
            return await validator.validate_access_token(token)
        except OidcProviderUnavailableError as exc:
            raise _service_unavailable(str(exc)) from exc
        except InvalidAccessTokenError as exc:
            raise _unauthorized(str(exc)) from exc

    return dependency


def optional_principal(
    validator: OidcTokenValidator,
) -> Callable[[HTTPAuthorizationCredentials | None], Awaitable[HumanPrincipal | None]]:
    """返回一个可选 principal 依赖；认证缺失或无效时返回 None。"""

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> HumanPrincipal | None:
        token = _extract_bearer_token(credentials)
        if token is None:
            return None
        try:
            return await validator.validate_access_token(token)
        except InvalidAccessTokenError:
            return None
        except OidcProviderUnavailableError as exc:
            raise _service_unavailable(str(exc)) from exc

    return dependency


def require_roles(
    validator: OidcTokenValidator,
    required_roles: Iterable[str],
) -> Callable[..., Awaitable[HumanPrincipal]]:
    """返回一个要求具备全部指定 roles 的依赖。"""

    expected = tuple(sorted({role for role in required_roles if role}))
    principal_dependency = require_principal(validator)

    async def dependency(
        principal: HumanPrincipal = Depends(principal_dependency),
    ) -> HumanPrincipal:
        missing = [role for role in expected if role not in set(principal.roles)]
        if missing:
            raise _forbidden(f"Missing required roles: {', '.join(missing)}")
        return principal

    return dependency


def require_scopes(
    validator: OidcTokenValidator,
    required_scopes: Iterable[str],
) -> Callable[..., Awaitable[HumanPrincipal]]:
    """返回一个要求具备全部指定 scopes 的依赖。"""

    expected = tuple(sorted({scope for scope in required_scopes if scope}))
    principal_dependency = require_principal(validator)

    async def dependency(
        principal: HumanPrincipal = Depends(principal_dependency),
    ) -> HumanPrincipal:
        missing = [scope for scope in expected if scope not in set(principal.scopes)]
        if missing:
            raise _forbidden(f"Missing required scopes: {', '.join(missing)}")
        return principal

    return dependency


def audit_actor_from_principal(principal: HumanPrincipal) -> AuditActor:
    """根据真人 principal 构造脱敏后的 AMP audit actor。"""

    return AuditActor(
        id=principal.principal_id,
        type="human",
        name=principal.name or principal.username or principal.email,
        role=",".join(sorted(principal.roles)) or None,
    )


def _extract_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is None:
        return None
    if credentials.scheme.lower() != "bearer":
        raise _unauthorized("Unsupported authorization scheme")
    token = credentials.credentials.strip()
    if not token:
        raise _unauthorized("Missing bearer token")
    return token


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _service_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
