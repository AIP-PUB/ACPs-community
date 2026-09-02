from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from app.core.base_exception import AppError


class AuthErrorCode(StrEnum):
    """认证模块错误码。"""

    PHONE_ALREADY_REGISTERED = "PHONE_ALREADY_REGISTERED"
    USERNAME_ALREADY_TAKEN = "USERNAME_ALREADY_TAKEN"
    INVALID_VERIFICATION_CODE = "INVALID_VERIFICATION_CODE"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"  # nosec B105 - public error code, not a credential
    EXPIRED_TOKEN = "EXPIRED_TOKEN"  # nosec B105 - public error code, not a credential
    INACTIVE_USER = "INACTIVE_USER"
    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"
    INVALID_TOKEN = "INVALID_TOKEN"  # nosec B105 - public error code, not a credential
    TOKEN_VALIDATION_ERROR = "TOKEN_VALIDATION_ERROR"  # nosec B105 - public error code, not a credential
    LOCAL_AUTH_DISABLED = "LOCAL_AUTH_DISABLED"
    OIDC_PROVIDER_UNAVAILABLE = "OIDC_PROVIDER_UNAVAILABLE"


class AuthError(AppError):
    """认证相关异常的基类。"""

    def __init__(
        self,
        *,
        status_code: int = 401,
        code: str | AuthErrorCode | None = None,
        title: str | None = None,
        detail: str | None = None,
        input_params: dict[str, Any] | None = None,
        error_name: str | AuthErrorCode | None = None,
        error_msg: str | None = None,
    ) -> None:
        resolved_code = str(code or error_name or AuthErrorCode.INVALID_CREDENTIALS)
        resolved_detail = detail or error_msg or "An authentication error occurred"
        super().__init__(
            status_code=status_code,
            code=resolved_code,
            title=title,
            detail=resolved_detail,
            type_=f"urn:acps:error:auth:{resolved_code.lower()}",
            extensions={
                "error_group": "auth",
                "input_params": input_params or {},
            },
        )


class TokenValidationError(AuthError):
    """Bearer token 无法通过校验时抛出的异常。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=401,
            code=AuthErrorCode.TOKEN_VALIDATION_ERROR,
            detail="Could not validate credentials",
            input_params={"token": "***"},  # nosec B105 - redacted diagnostic placeholder
        )


class AuthUserNotFoundError(AuthError):
    """认证后的用户记录不存在时抛出的异常。"""

    def __init__(self, *, user_id: str) -> None:
        super().__init__(
            status_code=401,
            code=AuthErrorCode.USER_NOT_FOUND,
            detail="User not found",
            input_params={"user_id": user_id},
        )


class InactiveUserError(AuthError):
    """认证用户处于 inactive 状态时抛出的异常。"""

    def __init__(self, *, user_id: str) -> None:
        super().__init__(
            status_code=403,
            code=AuthErrorCode.INACTIVE_USER,
            detail="Inactive user",
            input_params={"user_id": user_id},
        )


class ExpiredTokenError(AuthError):
    """Bearer token 已过期时抛出的异常。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=401,
            code=AuthErrorCode.EXPIRED_TOKEN,
            detail="Token has expired",
            input_params={"token": "***"},  # nosec B105 - redacted diagnostic placeholder
        )


class InsufficientPermissionsError(AuthError):
    """用户缺少所需角色之一时抛出的异常。"""

    def __init__(
        self,
        *,
        user_id: str,
        user_roles: Sequence[str | object],
        required_roles: Sequence[str | object],
    ) -> None:
        super().__init__(
            status_code=403,
            code=AuthErrorCode.INSUFFICIENT_PERMISSIONS,
            detail=f"User does not have required roles: {required_roles}",
            input_params={
                "user_id": user_id,
                "user_roles": [str(role) for role in user_roles],
                "required_roles": [str(role) for role in required_roles],
            },
        )


class LocalAuthDisabledError(AuthError):
    """OIDC 模式下访问本地认证能力时抛出的异常。"""

    def __init__(self, *, detail: str = "Local authentication is disabled when OIDC is enabled") -> None:
        super().__init__(
            status_code=410,
            code=AuthErrorCode.LOCAL_AUTH_DISABLED,
            detail=detail,
        )


class OidcProviderUnavailableAuthError(AuthError):
    """OIDC provider discovery / JWKS 暂不可用时抛出的异常。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code=AuthErrorCode.OIDC_PROVIDER_UNAVAILABLE,
            detail="OIDC provider is unavailable",
        )
