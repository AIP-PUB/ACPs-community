"""OIDC Provider 配置模型。"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_distinct_strings(values: Iterable[str]) -> tuple[str, ...]:
    """去重并规范化字符串序列。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


class KeycloakClaimMapping(BaseModel):
    """定义如何把 Keycloak access token claims 映射为真人 principal。"""

    model_config = ConfigDict(extra="forbid")

    resource_client_id: str | None = Field(
        default=None,
        description="在 resource_access 下读取项目角色时使用的 client id。",
    )
    read_client_roles: bool = True
    read_realm_roles: bool = False
    read_groups: bool = True
    username_claims: tuple[str, ...] = ("preferred_username", "username")
    name_claims: tuple[str, ...] = ("name", "preferred_username")
    email_claim: str = "email"
    email_verified_claim: str = "email_verified"
    tenant_claim: str = "tenant_id"
    allowed_aics_claim: str = "allowed_aics"

    @field_validator("username_claims", "name_claims", mode="before")
    @classmethod
    def _validate_claim_lists(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return _normalize_distinct_strings([value])
        if isinstance(value, Iterable):
            return _normalize_distinct_strings(str(item) for item in value)
        raise TypeError("claim list must be a string or iterable of strings")


class OidcProviderConfig(BaseModel):
    """Resource Server 共用的 OIDC Provider 配置。"""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    audience: str
    allowed_azp: tuple[str, ...] = ()
    client_id: str | None = Field(
        default=None,
        description="本地 Resource Server 标识，不参与 token 验签。",
    )
    algorithms: tuple[str, ...] = ("EdDSA",)
    jwks_cache_ttl_seconds: int = 300
    discovery_cache_ttl_seconds: int = 300
    leeway_seconds: int = 30
    require_https: bool = True
    http_timeout_seconds: float = 5.0
    claim_mapping: KeycloakClaimMapping = Field(default_factory=KeycloakClaimMapping)

    @field_validator("allowed_azp", "algorithms", mode="before")
    @classmethod
    def _validate_string_lists(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return _normalize_distinct_strings([value])
        if isinstance(value, Iterable):
            return _normalize_distinct_strings(str(item) for item in value)
        raise TypeError("value must be a string or iterable of strings")

    @field_validator("issuer")
    @classmethod
    def _strip_issuer(cls, value: str) -> str:
        issuer = value.strip().rstrip("/")
        if not issuer:
            raise ValueError("issuer must not be empty")
        return issuer

    @field_validator("audience")
    @classmethod
    def _validate_audience(cls, value: str) -> str:
        audience = value.strip()
        if not audience:
            raise ValueError("audience must not be empty")
        return audience

    @field_validator("jwks_cache_ttl_seconds", "discovery_cache_ttl_seconds", "leeway_seconds")
    @classmethod
    def _validate_non_negative_ints(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be >= 0")
        return value

    @field_validator("http_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("http_timeout_seconds must be > 0")
        return value
