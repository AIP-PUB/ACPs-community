from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from click import ClickException

from acps_cli.shared.session_store import SessionPrincipalSnapshot, SessionStore, StoredSessionRecord


def _record(index: int = 1) -> StoredSessionRecord:
    now = datetime.now(tz=timezone.utc)
    return StoredSessionRecord(
        schema_version=2,
        service="monitor",
        account_kind="user",
        auth_mode="oidc",
        issuer="https://issuer.example/realms/acps-monitor",
        client_id="monitor-cli",
        token_type="Bearer",
        access_token=f"access-token-{index}",
        refresh_token=f"refresh-token-{index}",
        scope="openid profile email",
        expires_at=now + timedelta(minutes=5),
        refresh_expires_at=now + timedelta(hours=1),
        principal=SessionPrincipalSnapshot(
            sub_hash="sha256:123",
            preferred_username="monitor-viewer",
            roles=("viewer",),
            scopes=("openid", "profile", "email"),
            allowed_aics=("AIC-DEMO-001",),
        ),
        created_at=now,
        updated_at=now,
    )


def test_session_store_round_trips_schema_v2(tmp_path) -> None:
    store = SessionStore(tmp_path / "monitor-user.json")
    record = _record()

    store.save(record)
    loaded = store.load()

    assert loaded is not None
    assert loaded.service == "monitor"
    assert loaded.account_kind == "user"
    assert loaded.client_id == "monitor-cli"
    assert loaded.token_type == "Bearer"
    assert loaded.principal is not None
    assert loaded.principal.preferred_username == "monitor-viewer"


def test_session_store_loads_legacy_local_token_file(tmp_path) -> None:
    token_file = tmp_path / "registry-user.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "legacy-access-token",
                "refresh_token": "legacy-refresh-token",
                "token_type": "bearer",
            }
        ),
        encoding="utf-8",
    )

    loaded = SessionStore(token_file).load()

    assert loaded is not None
    assert loaded.schema_version is None
    assert loaded.auth_mode is None
    assert loaded.access_token == "legacy-access-token"
    assert loaded.refresh_token == "legacy-refresh-token"
    assert loaded.token_type == "Bearer"


def test_session_store_rejects_non_bearer_token_type(tmp_path) -> None:
    token_file = tmp_path / "monitor-user.json"
    token_file.write_text(json.dumps({"access_token": "token", "token_type": "Basic"}), encoding="utf-8")

    with pytest.raises(ClickException, match="Token type must be Bearer"):
        SessionStore(token_file).load()


def test_session_store_writes_restricted_permissions(tmp_path) -> None:
    token_file = tmp_path / "monitor-user.json"
    SessionStore(token_file).save(_record())

    file_mode = stat.S_IMODE(token_file.stat().st_mode)

    assert file_mode == 0o600


def test_session_store_concurrent_writes_keep_valid_json(tmp_path) -> None:
    token_file = tmp_path / "monitor-user.json"
    store = SessionStore(token_file)

    def write(index: int) -> None:
        store.save(_record(index))

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(write, range(1, 31)))

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token.startswith("access-token-")
    assert loaded.refresh_token is not None
