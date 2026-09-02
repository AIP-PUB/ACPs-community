"""Authorization scope-filter unit tests for monitor-server."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from types import SimpleNamespace

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from fastapi import HTTPException

from app.audit.schema import AuditRecordQueryRequest
from app.core.amp_api_schema import AMPFilter, AMPFilterCondition, AMPTimeRange
from app.core.authz import (
    apply_request_scope,
    ensure_any_aic_allowed,
    ensure_path_aic_allowed,
    ensure_trace_view_allowed,
    principal_scope_filter,
)
from app.core.config import settings
from app.system.planner import inject_scope_filter


def _principal(
    *,
    roles: tuple[str, ...] = ("viewer",),
    scopes: tuple[str, ...] = ("system:read",),
    tenant_id: str | None = None,
    allowed_aics: tuple[str, ...] = (),
) -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-monitor"
    subject = "scope-user-001"
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


@pytest.fixture(autouse=True)
def _restore_settings() -> Generator[None]:
    original_toml = deepcopy(settings._toml)
    try:
        yield
    finally:
        settings._toml = original_toml


def _enable_oidc() -> None:
    settings._toml.setdefault("oidc", {})["enabled"] = True


def test_principal_scope_filter_returns_admin_scope_when_oidc_disabled() -> None:
    scope = principal_scope_filter(None)
    assert scope.is_admin is True
    assert scope.tenant_id is None
    assert scope.allowed_aics == ()


def test_principal_scope_filter_allows_admin_without_explicit_resource_scope() -> None:
    _enable_oidc()
    principal = _principal(roles=("admin",))

    scope = principal_scope_filter(principal)

    assert scope.is_admin is True
    assert scope.allowed_aics == ()


def test_principal_scope_filter_rejects_unscoped_non_admin() -> None:
    _enable_oidc()
    principal = _principal()

    with pytest.raises(HTTPException) as exc_info:
        principal_scope_filter(principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Principal has no tenant_id or allowed_aics scope"


def test_apply_request_scope_injects_existing_filter_tenant_and_aics() -> None:
    _enable_oidc()
    principal = _principal(tenant_id="tenant-001", allowed_aics=("aic-001", "aic-002"))
    request = AuditRecordQueryRequest(
        timeRange=AMPTimeRange(start_at="2026-06-24T00:00:00Z", end_at="2026-06-24T01:00:00Z"),
        filter=AMPFilter(
            conditions=[AMPFilterCondition(field="traceId", op="eq", value="trace-001")],
            logic="and",
        ),
    )

    scoped_request = apply_request_scope(request, principal, aic_field="aic", tenant_field="tenantId")

    assert scoped_request is not request
    assert scoped_request.filter is not None
    assert scoped_request.filter.conditions is not None
    conditions = scoped_request.filter.conditions
    assert conditions[0].field == "traceId"
    assert conditions[1].field == "tenantId"
    assert conditions[1].value == "tenant-001"
    assert conditions[2].field == "aic"
    assert conditions[2].op == "in"
    assert conditions[2].value == ["aic-001", "aic-002"]


def test_inject_scope_filter_maps_scope_to_opensearch_clauses() -> None:
    _enable_oidc()
    principal = _principal(tenant_id="tenant-001", allowed_aics=("aic-001", "aic-002"))

    clauses = inject_scope_filter(principal=principal)

    assert clauses == [
        {"term": {"tenant_id": "tenant-001"}},
        {"terms": {"aic": ["aic-001", "aic-002"]}},
    ]


def test_ensure_path_aic_allowed_rejects_tenant_only_principal_without_aic_scope() -> None:
    _enable_oidc()
    principal = _principal(tenant_id="tenant-001")

    with pytest.raises(HTTPException) as exc_info:
        ensure_path_aic_allowed("aic-001", principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Request scope cannot be derived for this principal"


def test_ensure_any_aic_allowed_rejects_tenant_only_principal_without_aic_scope() -> None:
    _enable_oidc()
    principal = _principal(tenant_id="tenant-001")

    with pytest.raises(HTTPException) as exc_info:
        ensure_any_aic_allowed(["aic-001"], principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Request scope cannot be derived for this principal"


def test_ensure_trace_view_allowed_rejects_tenant_only_principal_without_aic_scope() -> None:
    _enable_oidc()
    principal = _principal(tenant_id="tenant-001")
    view = SimpleNamespace(
        summary=SimpleNamespace(root_aic="aic-001"),
        spans=[SimpleNamespace(aic="aic-001")],
    )

    with pytest.raises(HTTPException) as exc_info:
        ensure_trace_view_allowed(view, principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Request scope cannot be derived for this principal"
