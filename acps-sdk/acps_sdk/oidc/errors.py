"""OIDC 校验相关的异常层级。"""

from __future__ import annotations


class OidcError(Exception):
    """OIDC 相关异常的基类。"""


class OidcAuthenticationError(OidcError):
    """当 bearer token 缺失或无效时抛出。"""


class OidcAuthorizationError(OidcError):
    """当 principal 缺少所需权限时抛出。"""


class OidcClientError(OidcError):
    """当 OIDC 客户端侧协议或 HTTP 交互失败时抛出。"""

    def __init__(
        self,
        message: str,
        *,
        error: str | None = None,
        error_description: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error = error
        self.error_description = error_description
        self.status_code = status_code

    def __str__(self) -> str:
        parts = [self.message]
        if self.error:
            parts.append(f"error={self.error}")
        if self.error_description:
            parts.append(f"description={self.error_description}")
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        return "; ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"error={self.error!r}, "
            f"error_description={self.error_description!r}, "
            f"status_code={self.status_code!r}"
            f")"
        )


class OidcProviderUnavailableError(OidcError):
    """当 discovery 或 JWKS 无法加载且没有可用缓存时抛出。"""


class MissingBearerTokenError(OidcAuthenticationError):
    """当请求里没有 bearer token 时抛出。"""


class InvalidAccessTokenError(OidcAuthenticationError):
    """当 bearer token 未通过 access-token 校验时抛出。"""


class OidcDeviceAuthorizationNotSupportedError(OidcClientError):
    """当 provider 未暴露 Device Authorization endpoint 时抛出。"""


class OidcDeviceAuthorizationDeniedError(OidcClientError):
    """当终端用户拒绝授权时抛出。"""


class OidcDeviceAuthorizationExpiredError(OidcClientError):
    """当 device_code 已过期时抛出。"""


class MissingRoleError(OidcAuthorizationError):
    """当 principal 缺少 required roles 时抛出。"""


class MissingScopeError(OidcAuthorizationError):
    """当 principal 缺少 required scopes 时抛出。"""
