"""Representative monitor API authorization tests."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from acps_sdk.amp.heartbeat_sync import HeartbeatSyncInfo
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit.api import router as audit_router
from app.core import authz as authz_module
from app.core import oidc as oidc_module
from app.core.amp_api_schema import AMPResponseMeta
from app.core.config import settings
from app.heartbeat.api import router as heartbeat_router
from app.system.api import router as system_router


def _principal(
    *,
    roles: tuple[str, ...] = ("viewer",),
    scopes: tuple[str, ...] = (),
    tenant_id: str | None = None,
    allowed_aics: tuple[str, ...] = ("aic-001",),
) -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-monitor"
    subject = "monitor-role-user-001"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("monitor-api",),
        azp="monitor-web",
        roles=roles,
        scopes=scopes,
        tenant_id=tenant_id,
        allowed_aics=allowed_aics,
        raw_claims={},
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(system_router, prefix="/acps-amp-v1")
    app.include_router(audit_router, prefix="/acps-amp-v1")
    app.include_router(heartbeat_router, prefix="/acps-amp-v1")
    return app


@pytest.fixture(autouse=True)
def _restore_state() -> Generator[None]:
    original_toml = deepcopy(settings._toml)
    original_validator = oidc_module._validator
    try:
        yield
    finally:
        settings._toml = original_toml
        oidc_module._validator = original_validator


def _enable_oidc() -> None:
    settings._toml.setdefault("oidc", {})["enabled"] = True


def test_viewer_can_read_system_events_and_scope_is_injected() -> None:
    _enable_oidc()
    principal = _principal(roles=("viewer",), allowed_aics=("aic-001", "aic-002"))
    app = _make_app()
    app.dependency_overrides[oidc_module.get_request_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)
    query_events = AsyncMock(return_value=([], AMPResponseMeta()))

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.system.api.service.query_events", query_events)
        response = client.post(
            "/acps-amp-v1/system/events/query",
            json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
        )

    assert response.status_code == 200
    await_args = query_events.await_args
    assert await_args is not None
    scoped_request = await_args.args[1]
    assert scoped_request.filter is not None
    assert scoped_request.filter.conditions is not None
    assert scoped_request.filter.conditions[0].field == "aic"
    assert scoped_request.filter.conditions[0].op == "in"
    assert scoped_request.filter.conditions[0].value == ["aic-001", "aic-002"]
    assert await_args.kwargs["principal"] == principal


def test_viewer_cannot_submit_audit_export() -> None:
    _enable_oidc()
    principal = _principal(roles=("viewer",))
    app = _make_app()
    app.dependency_overrides[oidc_module.get_request_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/acps-amp-v1/audit/export",
        json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing audit:export scope or auditor role"


def test_auditor_can_submit_audit_export() -> None:
    _enable_oidc()
    principal = _principal(roles=("auditor",))
    app = _make_app()
    app.dependency_overrides[oidc_module.get_request_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.audit.api.service.submit_export", AsyncMock(return_value="export-task-001"))
        response = client.post(
            "/acps-amp-v1/audit/export",
            json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
        )

    assert response.status_code == 202
    assert response.json()["taskId"] == "export-task-001"


def test_auditor_can_submit_integrity_verify() -> None:
    _enable_oidc()
    principal = _principal(roles=("auditor",))
    app = _make_app()
    app.dependency_overrides[oidc_module.get_request_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)

    result = {
        "checkedAt": "2026-06-24T00:00:00Z",
        "summary": {"checkedCount": 1, "failedCount": 0, "anchoredUntil": None},
        "failures": [],
    }

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.audit.api.service.submit_integrity_verify", AsyncMock(return_value=result))
        response = client.post(
            "/acps-amp-v1/audit/integrity/verify",
            json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
        )

    assert response.status_code == 200
    assert response.json()["summary"]["checkedCount"] == 1


def test_operator_can_read_sync_info() -> None:
    _enable_oidc()
    principal = _principal(roles=("operator",))
    app = _make_app()
    app.dependency_overrides[authz_module.require_sync_access] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)

    sync_info = HeartbeatSyncInfo(
        type="amp-alive-delta",
        schema_version="1",
        snapshot_content_type="application/x-ndjson",
        kafka_topic="amp.heartbeat.alive-delta",
        shard_count=1,
        refresh_emit_interval_seconds=30,
        delta_retention_hours=168,
        current_published_seq_by_shard={"hb-000": "12"},
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.heartbeat.api.sync_service.ensure_sync_enabled", lambda: None)
        monkeypatch.setattr("app.heartbeat.api.sync_service.get_sync_info", AsyncMock(return_value=sync_info))
        monkeypatch.setattr("app.heartbeat.api.get_redis", lambda: AsyncMock())
        response = client.get("/acps-amp-v1/heartbeat/sync/info")

    assert response.status_code == 200
    assert response.json()["kafkaTopic"] == "amp.heartbeat.alive-delta"


def test_internal_sync_token_can_read_sync_info_when_oidc_enabled() -> None:
    """Keycloak/OIDC on: discovery-server uses shared internal Bearer for /sync/*."""
    _enable_oidc()
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    sync_info = HeartbeatSyncInfo(
        type="amp-alive-delta",
        schema_version="1",
        snapshot_content_type="application/x-ndjson",
        kafka_topic="amp.heartbeat.alive-delta",
        shard_count=1,
        refresh_emit_interval_seconds=30,
        delta_retention_hours=168,
        current_published_seq_by_shard={"hb-000": "12"},
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            settings,
            "heartbeat_sync_internal_token",
            "svc-sync-token",
            raising=False,
        )
        monkeypatch.setattr("app.heartbeat.api.sync_service.ensure_sync_enabled", lambda: None)
        monkeypatch.setattr("app.heartbeat.api.sync_service.get_sync_info", AsyncMock(return_value=sync_info))
        monkeypatch.setattr("app.heartbeat.api.get_redis", lambda: AsyncMock())
        response = client.get(
            "/acps-amp-v1/heartbeat/sync/info",
            headers={"Authorization": "Bearer svc-sync-token"},
        )

    assert response.status_code == 200
    assert response.json()["type"] == "amp-alive-delta"


def test_oidc_on_rejects_sync_info_without_token() -> None:
    _enable_oidc()
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "heartbeat_sync_internal_token", "", raising=False)
        monkeypatch.setattr("app.heartbeat.api.sync_service.ensure_sync_enabled", lambda: None)
        monkeypatch.setattr("app.heartbeat.api.get_redis", lambda: AsyncMock())

        # No validator initialized → 503 from resolve_bearer_principal path, or 401.
        # Force resolve to behave like missing bearer.
        async def _missing(_credentials: object) -> HumanPrincipal:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        monkeypatch.setattr(authz_module, "resolve_bearer_principal", _missing)
        response = client.get("/acps-amp-v1/heartbeat/sync/info")

    assert response.status_code == 401


def test_tenant_only_principal_cannot_read_heartbeat_summary() -> None:
    _enable_oidc()
    principal = _principal(roles=("viewer",), tenant_id="tenant-001", allowed_aics=())
    app = _make_app()
    app.dependency_overrides[oidc_module.get_request_principal] = lambda: principal
    client = TestClient(app, raise_server_exceptions=False)
    get_summary = AsyncMock(return_value=(None, None))

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.heartbeat.api.service.get_summary", get_summary)
        response = client.get("/acps-amp-v1/heartbeat/summary")

    assert response.status_code == 403
    assert response.json()["detail"] == "Request scope cannot be derived for this principal"
    get_summary.assert_not_awaited()
