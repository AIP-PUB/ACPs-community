from __future__ import annotations

import pytest

from acps_sdk.oidc import (
    KeycloakClaimMapping,
    claims_to_principal,
    normalize_audiences,
    parse_multi_value_claim,
    parse_scope_claim,
)


def _claims() -> dict[str, object]:
    return {
        "iss": "https://issuer.example/realms/acps-monitor",
        "sub": "user-123",
        "aud": ["monitor-api", "account"],
        "azp": "monitor-web",
        "preferred_username": "alice",
        "name": "Alice",
        "email": "alice@example.com",
        "email_verified": True,
        "tenant_id": "tenant-1",
        "allowed_aics": ["aic-1", "aic-2"],
        "scope": "metrics:read system:query",
        "groups": ["/ops", "/monitor"],
        "resource_access": {"monitor-api": {"roles": ["AUDITOR", "SYSTEM_READER"]}},
        "realm_access": {"roles": ["REALM_ADMIN"]},
    }


def test_claim_parser_reads_client_roles_groups_and_scopes_by_default() -> None:
    principal = claims_to_principal(
        claims=_claims(),
        claim_mapping=KeycloakClaimMapping(resource_client_id="monitor-api"),
    )

    assert principal.roles == ("AUDITOR", "SYSTEM_READER")
    assert principal.groups == ("/ops", "/monitor")
    assert principal.scopes == ("metrics:read", "system:query")
    assert principal.tenant_id == "tenant-1"
    assert principal.allowed_aics == ("aic-1", "aic-2")


def test_claim_parser_ignores_realm_roles_unless_enabled() -> None:
    principal = claims_to_principal(
        claims=_claims(),
        claim_mapping=KeycloakClaimMapping(resource_client_id="monitor-api"),
    )

    assert "REALM_ADMIN" not in principal.roles


def test_claim_parser_reads_realm_roles_when_enabled() -> None:
    principal = claims_to_principal(
        claims=_claims(),
        claim_mapping=KeycloakClaimMapping(
            resource_client_id="monitor-api",
            read_realm_roles=True,
        ),
    )

    assert principal.roles == ("AUDITOR", "SYSTEM_READER", "REALM_ADMIN")


def test_parse_scope_claim_uses_oauth_space_delimiter() -> None:
    assert parse_scope_claim("session:read session:write") == ("session:read", "session:write")


def test_normalize_audiences_rejects_non_string_items() -> None:
    with pytest.raises(TypeError, match="aud claim values must be strings"):
        normalize_audiences(["monitor-api", 123])


def test_parse_multi_value_claim_rejects_non_string_items() -> None:
    with pytest.raises(TypeError, match="claim values must be strings"):
        parse_multi_value_claim(["aic-1", 2])
