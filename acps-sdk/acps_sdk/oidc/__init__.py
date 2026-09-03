"""供 ACPs 各服务复用的 OIDC 辅助能力。"""

from acps_sdk.oidc.client import (
    OidcDeviceAuthorizationResponse,
    OidcDeviceClient,
    OidcDeviceClientConfig,
    OidcDeviceTokenPollingResult,
    OidcDeviceTokenPollingStatus,
    OidcTokenResponse,
    OidcTokenRevocationResult,
)
from acps_sdk.oidc.config import KeycloakClaimMapping, OidcProviderConfig
from acps_sdk.oidc.discovery import OidcDiscoveryCache, OidcDiscoveryDocument
from acps_sdk.oidc.errors import (
    InvalidAccessTokenError,
    MissingBearerTokenError,
    MissingRoleError,
    MissingScopeError,
    OidcAuthenticationError,
    OidcAuthorizationError,
    OidcClientError,
    OidcDeviceAuthorizationDeniedError,
    OidcDeviceAuthorizationExpiredError,
    OidcDeviceAuthorizationNotSupportedError,
    OidcError,
    OidcProviderUnavailableError,
)
from acps_sdk.oidc.fastapi import (
    audit_actor_from_principal,
    optional_principal,
    require_principal,
    require_roles,
    require_scopes,
)
from acps_sdk.oidc.keycloak import (
    claims_to_principal,
    normalize_audiences,
    parse_multi_value_claim,
    parse_scope_claim,
)
from acps_sdk.oidc.principal import (
    HumanPrincipal,
    build_principal_id,
    build_principal_key,
    canonical_json_bytes,
)

try:
    from acps_sdk.oidc.validator import OidcTokenValidator
except (
    ModuleNotFoundError
) as exc:  # pragma: no cover - exercised by acps-cli env without oidc extra synced
    if exc.name != "jwt":
        raise
    _JWT_IMPORT_ERROR = exc

    class OidcTokenValidator:  # type: ignore[no-redef]
        """占位符，提示调用方安装 OIDC 可选依赖。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ModuleNotFoundError(
                "OidcTokenValidator requires optional dependency "
                "'PyJWT'. Install acps-sdk[oidc]."
            ) from _JWT_IMPORT_ERROR


__all__ = [
    "HumanPrincipal",
    "KeycloakClaimMapping",
    "OidcProviderConfig",
    "OidcDeviceClientConfig",
    "OidcDeviceAuthorizationResponse",
    "OidcTokenResponse",
    "OidcDeviceTokenPollingStatus",
    "OidcDeviceTokenPollingResult",
    "OidcTokenRevocationResult",
    "OidcDeviceClient",
    "OidcDiscoveryCache",
    "OidcDiscoveryDocument",
    "OidcError",
    "OidcClientError",
    "OidcAuthenticationError",
    "OidcAuthorizationError",
    "OidcProviderUnavailableError",
    "OidcDeviceAuthorizationNotSupportedError",
    "OidcDeviceAuthorizationDeniedError",
    "OidcDeviceAuthorizationExpiredError",
    "MissingBearerTokenError",
    "InvalidAccessTokenError",
    "MissingRoleError",
    "MissingScopeError",
    "build_principal_key",
    "build_principal_id",
    "canonical_json_bytes",
    "normalize_audiences",
    "parse_scope_claim",
    "parse_multi_value_claim",
    "claims_to_principal",
    "require_principal",
    "optional_principal",
    "require_roles",
    "require_scopes",
    "audit_actor_from_principal",
    "OidcTokenValidator",
]
