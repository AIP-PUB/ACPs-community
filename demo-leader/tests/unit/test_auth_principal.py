"""Leader OIDC principal helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key


def _principal(*, principal_id: str | None = None, roles: tuple[str, ...] = ("user",)) -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-leader"
    subject = principal_id or "user-subject-001"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=principal_id or build_principal_id(issuer=issuer, subject=subject),
        audiences=("leader-api",),
        username="alice",
        name="Alice",
        email="alice@example.com",
        roles=roles,
        scopes=("leader:submit",),
        raw_claims={},
    )


def _session():
    from assistant.models import ExecutionMode, ScenarioRuntime, Session, UserResult, UserResultType
    from assistant.models.base import now_iso

    now = now_iso()
    return Session(
        session_id="sess-auth-test",
        mode=ExecutionMode.DIRECT_RPC,
        created_at=now,
        updated_at=now,
        touched_at=now,
        ttl_seconds=3600,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        base_scenario=ScenarioRuntime(id="base", kind="base", version="1.0.0", loaded_at=now),
        event_log=[],
        user_result=UserResult(type=UserResultType.PENDING, data_items=[], updated_at=now),
    )


def test_bind_session_principal_persists_safe_fields() -> None:
    from assistant.security import bind_session_principal

    principal = _principal()
    session = _session()

    bind_session_principal(session, principal)

    assert session.user_id == principal.principal_id
    assert session.principal_issuer == principal.issuer
    assert session.principal_subject == principal.subject
    assert session.principal_username == principal.username
    assert session.principal_email == principal.email
    assert session.user_context["principal"]["principalId"] == principal.principal_id
    assert "subject" not in session.user_context["principal"]


def test_ensure_session_owner_rejects_non_owner() -> None:
    from assistant.security import ensure_session_owner
    from fastapi import HTTPException

    owner = _principal(principal_id="owner-principal-id")
    other = _principal(principal_id="other-principal-id")
    session = _session()
    session.user_id = owner.principal_id

    try:
        ensure_session_owner(session, other)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Expected non-owner access to be rejected")


def test_security_helpers_allow_owner_or_operator_and_replace_invalid_context(monkeypatch) -> None:
    from assistant import security

    principal = _principal()
    session = _session()
    session.user_id = principal.principal_id
    security.ensure_session_owner(session, principal)

    session.user_id = "another-user"
    monkeypatch.setattr(security, "is_operator", lambda _principal: True)
    security.ensure_session_owner(session, principal, allow_operator=True)
    assert security.can_manage_group(principal) is True

    session.user_context = None
    security.bind_session_principal(session, principal)
    assert session.user_context["principal"]["principalId"] == principal.principal_id


def test_stream_token_store_binds_token_to_session() -> None:
    from assistant.auth import StreamTokenStore

    store = StreamTokenStore()
    token, _ = store.issue(session_id="sess-001", principal_id="principal-001", ttl_seconds=60)

    record = store.validate(session_id="sess-001", token=token)

    assert record is not None
    assert record.session_id == "sess-001"
    assert record.principal_id == "principal-001"
    assert store.validate(session_id="sess-002", token=token) is None
