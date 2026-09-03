"""Listener-aware app factory tests."""

from __future__ import annotations

from app.api.group_problem import GroupApiError
from tests.conftest import VALID_LEADER_AIC, build_test_client


def test_group_listener_exposes_group_openapi_and_group_exception_handler() -> None:
    client, _, _, _ = build_test_client(
        listener_port=9007,
        caller_aic=VALID_LEADER_AIC,
    )
    app = client.app

    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "MQ Auth Group API"
    assert schema["info"]["version"] == "2.2.0"
    assert "/groups/{leader_aic}/{group_id}/members/{member_aic}" in schema["paths"]
    assert "/auth/user" not in schema["paths"]
    assert "/health" in schema["paths"]
    assert "/ready" in schema["paths"]
    assert GroupApiError in app.exception_handlers
    assert any(getattr(route, "path", None) == "/metrics" for route in app.routes)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True


def test_auth_listener_exposes_auth_openapi_without_group_exception_handler() -> None:
    client, _, _, _ = build_test_client(listener_port=9008)
    app = client.app

    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "MQ Auth Auth API"
    assert schema["info"]["version"] == "2.2.0"
    assert "/auth/user" in schema["paths"]
    assert "/groups/{leader_aic}/{group_id}/members/{member_aic}" not in schema["paths"]
    assert "/health" in schema["paths"]
    assert "/ready" in schema["paths"]
    assert GroupApiError not in app.exception_handlers
    assert any(getattr(route, "path", None) == "/metrics" for route in app.routes)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True
