from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from acps_sdk.oidc import OidcTokenResponse

from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager
from acps_cli.shared.session_store import SessionPrincipalSnapshot, SessionStore, StoredSessionRecord
from acps_cli.shared.unified_config import OidcAuthConfig, ServiceAuthConfig


def _auth_config(tmp_path) -> ServiceAuthConfig:
    return ServiceAuthConfig(
        service="monitor",
        account_kind="user",
        mode="oidc",
        token_file=str(tmp_path / "monitor-user.json"),
        oidc=OidcAuthConfig(
            issuer="https://issuer.example/realms/acps-monitor",
            client_id="monitor-cli",
            scopes=("openid", "profile", "email"),
            require_https=True,
        ),
    )


def _jwt_token(*, exp_delta_seconds: int, roles: list[str] | None = None) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": "user-123",
        "preferred_username": "monitor-viewer",
        "name": "Monitor Viewer",
        "email": "monitor-viewer@example.com",
        "tenant_id": "tenant-demo",
        "allowed_aics": ["AIC-DEMO-001", "AIC-DEMO-002"],
        "realm_access": {"roles": roles or ["viewer"]},
        "exp": int((now + timedelta(seconds=exp_delta_seconds)).timestamp()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
    }
    header = {"alg": "none", "typ": "JWT"}

    def encode_part(value: dict[str, object]) -> str:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    return f"{encode_part(header)}.{encode_part(payload)}."


def _token_response(
    *,
    access_token: str,
    refresh_token: str | None = "refresh-token",
    expires_in: int | None = 300,
) -> OidcTokenResponse:
    payload = {
        "access_token": access_token,
        "token_type": "bearer",
    }
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    if expires_in is not None:
        payload["expires_in"] = expires_in
    return OidcTokenResponse.model_validate(payload)


def _stored_record(now: datetime) -> StoredSessionRecord:
    return StoredSessionRecord(
        schema_version=2,
        service="monitor",
        account_kind="user",
        auth_mode="oidc",
        issuer="https://issuer.example/realms/acps-monitor",
        client_id="monitor-cli",
        token_type="Bearer",
        access_token=_jwt_token(exp_delta_seconds=60),
        refresh_token="refresh-token",
        scope="openid profile email",
        expires_at=now + timedelta(seconds=60),
        refresh_expires_at=now + timedelta(hours=1),
        principal=SessionPrincipalSnapshot(
            sub_hash="sha256:abc",
            preferred_username="monitor-viewer",
            roles=("viewer",),
            scopes=("openid", "profile", "email"),
        ),
        created_at=now,
        updated_at=now,
    )


class FakeOidcClient:
    def __init__(self) -> None:
        self.poll_results: list[object] = []
        self.refresh_response: OidcTokenResponse | None = None
        self.revoke_called_with: str | None = None

    def start_device_authorization(self) -> SimpleNamespace:
        return SimpleNamespace(
            device_code=SimpleNamespace(get_secret_value=lambda: "device-code"),
            user_code="USER-CODE",
            verification_uri="https://issuer.example/device",
            verification_uri_complete="https://issuer.example/device?user_code=USER-CODE",
            interval=3,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        )

    def poll_device_token(self, device_code: str, interval: int):
        assert device_code == "device-code"
        assert interval >= 3
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh_token(self, refresh_token: str) -> OidcTokenResponse:
        assert refresh_token == "refresh-token"
        assert self.refresh_response is not None
        return self.refresh_response

    def revoke_token(self, token: str, *, token_type_hint: str | None = None):
        assert token_type_hint == "refresh_token"
        self.revoke_called_with = token
        return SimpleNamespace(revoked=True)


def test_oidc_login_persists_summary_and_token_file(monkeypatch, tmp_path) -> None:
    fake_client = FakeOidcClient()
    access_token = _jwt_token(exp_delta_seconds=300, roles=["viewer", "observer"])
    fake_client.poll_results = [
        SimpleNamespace(status="authorization_pending", interval=3, token_response=None),
        SimpleNamespace(status="success", interval=3, token_response=_token_response(access_token=access_token)),
    ]
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    monkeypatch.setattr(manager, "_with_device_client", lambda callback: callback(fake_client))
    prompts = []
    sleeps: list[float] = []

    record = manager.login(on_prompt=prompts.append, sleep_func=sleeps.append)

    assert len(prompts) == 1
    assert prompts[0].user_code == "USER-CODE"
    assert sleeps == [3, 3]
    assert record.principal is not None
    assert record.principal.preferred_username == "monitor-viewer"
    assert record.principal.roles == ("viewer", "observer")
    status = manager.status()
    assert status["authenticated"] is True
    assert status["preferred_username"] == "monitor-viewer"
    assert "access_token" not in status
    stored = SessionStore(tmp_path / "monitor-user.json").load()
    assert stored is not None
    assert stored.access_token == access_token


def test_refresh_updates_session_and_preserves_created_at(monkeypatch, tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(_stored_record(now))
    fake_client = FakeOidcClient()
    fake_client.refresh_response = _token_response(access_token=_jwt_token(exp_delta_seconds=600))
    monkeypatch.setattr(manager, "_with_device_client", lambda callback: callback(fake_client))

    refreshed = manager.refresh()

    assert refreshed.created_at == now
    assert refreshed.updated_at is not None
    assert refreshed.updated_at >= now
    assert refreshed.access_token != _stored_record(now).access_token


def test_get_access_token_auto_refreshes_when_expiring(monkeypatch, tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    record = replace(_stored_record(now), expires_at=now + timedelta(seconds=30))
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(record)
    fake_client = FakeOidcClient()
    fake_client.refresh_response = _token_response(access_token=_jwt_token(exp_delta_seconds=600))
    monkeypatch.setattr(manager, "_with_device_client", lambda callback: callback(fake_client))

    access_token = manager.get_access_token()

    assert access_token != record.access_token


def test_refresh_without_refresh_token_requires_login(tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(replace(_stored_record(now), refresh_token=None))

    with pytest.raises(AuthSessionError, match="does not include a refresh token"):
        manager.refresh()


def test_load_session_rejects_mismatched_issuer(tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(replace(_stored_record(now), issuer="https://other.example/realms/acps-monitor"))

    with pytest.raises(AuthSessionError, match="issuer/client_id"):
        manager.load_session()


def test_logout_revokes_refresh_token_and_clears_local_file(monkeypatch, tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(_stored_record(now))
    fake_client = FakeOidcClient()
    monkeypatch.setattr(manager, "_with_device_client", lambda callback: callback(fake_client))

    result = manager.logout()

    assert result["local_session_cleared"] is True
    assert result["revocation_attempted"] is True
    assert result["revoked"] is True
    assert fake_client.revoke_called_with == "refresh-token"
    assert manager.store.load() is None


def test_logout_skips_revocation_when_session_metadata_no_longer_matches(monkeypatch, tmp_path) -> None:
    now = datetime.now(tz=timezone.utc)
    manager = OidcAuthSessionManager(_auth_config(tmp_path))
    manager.store.save(
        replace(
            _stored_record(now),
            issuer="https://other.example/realms/acps-monitor",
        )
    )
    fake_client = FakeOidcClient()
    monkeypatch.setattr(manager, "_with_device_client", lambda callback: callback(fake_client))

    result = manager.logout()

    assert result["local_session_cleared"] is True
    assert result["revocation_attempted"] is False
    assert result["revoked"] is False
    assert "revocation_skipped_reason" in result
    assert fake_client.revoke_called_with is None
    assert manager.store.load() is None
