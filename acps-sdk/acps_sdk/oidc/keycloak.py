"""Keycloak access token claim 解析辅助工具。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from acps_sdk.oidc.config import KeycloakClaimMapping
from acps_sdk.oidc.principal import HumanPrincipal, build_principal_id, build_principal_key


def normalize_audiences(value: Any) -> tuple[str, ...]:
    """把 aud claim 规范化为稳定的字符串 tuple。"""

    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, str, dict)):
        items = _require_string_iterable(value, claim_name="aud")
    else:
        raise TypeError("aud claim must be a string or array of strings")
    return _normalize_strings(items)


def parse_scope_claim(value: Any) -> tuple[str, ...]:
    """解析 OAuth 2.0 的 scope claim。"""

    if value is None:
        return ()
    if not isinstance(value, str):
        raise TypeError("scope claim must be a string")
    return _normalize_strings(value.split())


def parse_multi_value_claim(value: Any) -> tuple[str, ...]:
    """解析可能是单字符串或字符串数组的自定义 claim。"""

    if value is None:
        return ()
    if isinstance(value, str):
        if "," in value:
            return _normalize_strings(part.strip() for part in value.split(","))
        return _normalize_strings([value])
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict, str)):
        return _normalize_strings(_require_string_iterable(value, claim_name="claim"))
    raise TypeError("claim must be a string or iterable of strings")


def claims_to_principal(
    *,
    claims: dict[str, Any],
    claim_mapping: KeycloakClaimMapping,
) -> HumanPrincipal:
    """把已验签的 Keycloak claims 转换为规范化 principal。"""

    issuer = _require_string_claim(claims, "iss")
    subject = _require_string_claim(claims, "sub")
    audiences = normalize_audiences(claims.get("aud"))
    azp = _optional_string_claim(claims, "azp")

    roles: list[str] = []
    if claim_mapping.read_client_roles and claim_mapping.resource_client_id:
        resource_access = claims.get("resource_access")
        if isinstance(resource_access, dict):
            client_claims = resource_access.get(claim_mapping.resource_client_id)
            if isinstance(client_claims, dict):
                roles.extend(parse_multi_value_claim(client_claims.get("roles")))
    if claim_mapping.read_realm_roles:
        realm_access = claims.get("realm_access")
        if isinstance(realm_access, dict):
            roles.extend(parse_multi_value_claim(realm_access.get("roles")))

    groups = ()
    if claim_mapping.read_groups:
        groups = parse_multi_value_claim(claims.get("groups"))

    username = _first_string_claim(claims, claim_mapping.username_claims)
    name = _first_string_claim(claims, claim_mapping.name_claims)
    email = _optional_string_claim(claims, claim_mapping.email_claim)
    email_verified = _optional_bool_claim(claims, claim_mapping.email_verified_claim)
    tenant_id = _optional_string_claim(claims, claim_mapping.tenant_claim)
    allowed_aics = parse_multi_value_claim(claims.get(claim_mapping.allowed_aics_claim))
    scopes = parse_scope_claim(claims.get("scope"))

    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=audiences,
        azp=azp,
        username=username,
        name=name,
        email=email,
        email_verified=email_verified,
        tenant_id=tenant_id,
        allowed_aics=allowed_aics,
        roles=_normalize_strings(roles),
        scopes=scopes,
        groups=groups,
        raw_claims=dict(claims),
    )


def _normalize_strings(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _require_string_iterable(values: Iterable[Any], *, claim_name: str) -> list[str]:
    """确保 claim 数组里的每个元素都是字符串。"""

    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            label = f"{claim_name} claim" if claim_name != "claim" else "claim"
            raise TypeError(f"{label} values must be strings")
        result.append(value)
    return result


def _require_string_claim(claims: dict[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{key} claim must be a non-empty string")
    return value


def _optional_string_claim(claims: dict[str, Any], key: str) -> str | None:
    value = claims.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} claim must be a string")
    normalized = value.strip()
    return normalized or None


def _optional_bool_claim(claims: dict[str, Any], key: str) -> bool | None:
    value = claims.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{key} claim must be a boolean")
    return value


def _first_string_claim(claims: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
