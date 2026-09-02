from __future__ import annotations

from acps_sdk.oidc import (
    HumanPrincipal,
    audit_actor_from_principal,
    build_principal_id,
    build_principal_key,
    canonical_json_bytes,
)


def test_principal_id_uses_structured_hash_input() -> None:
    first = build_principal_id(issuer="issuer#a", subject="subject")
    second = build_principal_id(issuer="issuer", subject="a#subject")

    assert first != second


def test_canonical_json_bytes_sorts_mapping_keys() -> None:
    first = canonical_json_bytes({"b": 2, "a": 1})
    second = canonical_json_bytes({"a": 1, "b": 2})

    assert first == second


def test_sensitive_principal_fields_are_excluded_from_serialization() -> None:
    principal = HumanPrincipal(
        issuer="https://issuer.example/realms/acps-leader",
        subject="raw-subject",
        principal_key=build_principal_key(issuer="https://issuer.example/realms/acps-leader", subject="raw-subject"),
        principal_id="principal-id",
        audiences=("leader-api",),
        roles=("ADMIN",),
        scopes=("session:write",),
        raw_claims={"sub": "raw-subject", "token": "secret"},
    )

    data = principal.model_dump()

    assert "subject" not in data
    assert "principal_key" not in data
    assert "raw_claims" not in data


def test_audit_actor_from_principal_does_not_leak_sensitive_fields() -> None:
    principal = HumanPrincipal(
        issuer="https://issuer.example/realms/acps-leader",
        subject="raw-subject",
        principal_key="https://issuer.example/realms/acps-leader#raw-subject",
        principal_id="principal-id",
        audiences=("leader-api",),
        username="alice",
        roles=("ADMIN", "USER"),
        raw_claims={"sub": "raw-subject"},
    )

    actor = audit_actor_from_principal(principal)
    actor_payload = actor.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert actor.id == "principal-id"
    assert actor.name == "alice"
    assert actor.role == "ADMIN,USER"
    assert "subject" not in actor_payload
    assert "principal_key" not in actor_payload
    assert "raw_claims" not in actor_payload
