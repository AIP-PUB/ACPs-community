from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.account.model import User
from app.account.oidc_user_link_migration import (
    OidcUserLinkMapping,
    load_link_mappings,
    migrate_local_users_to_oidc,
)


class DummySyncSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1


def _user(
    *,
    username: str,
    email: str | None = None,
    auth_provider: str = "local",
    external_issuer: str | None = None,
    external_subject: str | None = None,
    external_principal_id: str | None = None,
) -> User:
    user = User(
        username=username,
        email=email,
        auth_provider=auth_provider,
        external_issuer=external_issuer,
        external_subject=external_subject,
        external_principal_id=external_principal_id,
    )
    user.id = uuid.uuid4()
    return user


def _mapping(*, username: str = "alice", expected_email: str | None = None) -> OidcUserLinkMapping:
    return OidcUserLinkMapping(
        username=username,
        issuer="https://issuer.example/realms/acps-registry",
        subject="raw-subject-value",
        expected_email=expected_email,
    )


def _patch_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_user: User,
    principal_user: User | None = None,
) -> None:
    monkeypatch.setattr(
        "app.account.oidc_user_link_migration._get_user_by_id",
        lambda session, user_id: None,
    )
    monkeypatch.setattr(
        "app.account.oidc_user_link_migration._get_user_by_username",
        lambda session, username: target_user if username == target_user.username else None,
    )
    monkeypatch.setattr(
        "app.account.oidc_user_link_migration._get_user_by_external_principal_id",
        lambda session, principal_id: principal_user,
    )
    monkeypatch.setattr(
        "app.account.oidc_user_link_migration._get_user_by_external_identity",
        lambda session, issuer, subject: None,
    )


def _sync_session(session: DummySyncSession) -> Session:
    return cast("Session", session)


def test_load_link_mappings_rejects_email_only_locator(tmp_path: Path) -> None:
    mapping_file = tmp_path / "mappings.json"
    mapping_file.write_text(
        json.dumps(
            [
                {
                    "email": "alice@example.com",
                    "issuer": "https://issuer.example/realms/acps-registry",
                    "subject": "raw-subject-value",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="email-only matching is not supported"):
        load_link_mappings(mapping_file)


def test_migrate_local_users_to_oidc_dry_run_does_not_mutate_user(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySyncSession()
    user = _user(username="alice", email="alice@example.com")
    mapping = _mapping()
    _patch_resolution(monkeypatch, target_user=user)

    report = migrate_local_users_to_oidc(_sync_session(session), [mapping], dry_run=True)

    assert report.dry_run is True
    assert report.applied_count == 0
    assert report.blocking_count == 0
    assert report.results[0].status == "would_link"
    assert user.external_issuer is None
    assert user.external_subject is None
    assert user.external_principal_id is None
    assert session.flushed == 0

    rendered = json.dumps(report.to_json_dict(), ensure_ascii=False)
    assert "raw-subject-value" not in rendered
    assert report.results[0].subject_hash in rendered


def test_migrate_local_users_to_oidc_apply_links_existing_local_user(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySyncSession()
    user = _user(username="alice", email="alice@example.com")
    mapping = _mapping()
    _patch_resolution(monkeypatch, target_user=user)

    report = migrate_local_users_to_oidc(_sync_session(session), [mapping], dry_run=False)

    assert report.dry_run is False
    assert report.applied_count == 1
    assert report.blocking_count == 0
    assert report.results[0].status == "linked"
    assert user.external_issuer == mapping.issuer
    assert user.external_subject == "raw-subject-value"
    assert user.external_principal_id == mapping.principal_id
    assert user.auth_provider == "local"
    assert session.flushed == 1


def test_migrate_local_users_to_oidc_reports_principal_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySyncSession()
    target_user = _user(username="alice", email="alice@example.com")
    linked_user = _user(
        username="shadow-alice",
        auth_provider="oidc",
        external_issuer="https://issuer.example/realms/acps-registry",
        external_subject="raw-subject-value",
        external_principal_id=_mapping().principal_id,
    )
    mapping = _mapping()
    _patch_resolution(monkeypatch, target_user=target_user, principal_user=linked_user)

    report = migrate_local_users_to_oidc(_sync_session(session), [mapping], dry_run=False)

    assert report.applied_count == 0
    assert report.blocking_count == 1
    assert report.results[0].status == "principal_conflict"
    assert target_user.external_principal_id is None
    assert session.flushed == 0


def test_migrate_local_users_to_oidc_blocks_expected_email_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySyncSession()
    user = _user(username="alice", email="alice@example.com")
    mapping = _mapping(expected_email="mismatch@example.com")
    _patch_resolution(monkeypatch, target_user=user)

    report = migrate_local_users_to_oidc(_sync_session(session), [mapping], dry_run=False)

    assert report.applied_count == 0
    assert report.blocking_count == 1
    assert report.results[0].status == "email_mismatch"
    assert "email auto-merge is disabled" in report.results[0].message
    assert user.external_principal_id is None
