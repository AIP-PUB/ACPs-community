"""Representative monitor API authentication tests."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.oidc.errors import InvalidAccessTokenError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import oidc as oidc_module
from app.core.amp_api_schema import AMPResponseMeta
from app.core.config import settings
from app.system.api import router as system_router


class RejectingValidator:
    async def validate_access_token(self, token: str):  # type: ignore[no-untyped-def]
        errors = {
            "wrong-azp": "Unexpected azp",
            "id-token": "Audience does not include monitor-api",
            "cross-realm": "Unexpected issuer",
        }
        raise InvalidAccessTokenError(errors[token])


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(system_router, prefix="/acps-amp-v1")
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


def test_system_query_requires_bearer_token_when_oidc_enabled() -> None:
    _enable_oidc()
    oidc_module._validator = MagicMock()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    response = client.post(
        "/acps-amp-v1/system/events/query",
        json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("token", ["wrong-azp", "id-token", "cross-realm"])
def test_system_query_rejects_non_api_project_tokens(token: str) -> None:
    _enable_oidc()
    cast("Any", oidc_module)._validator = RejectingValidator()
    client = TestClient(_make_app(), raise_server_exceptions=False)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.system.api.service.query_events", AsyncMock(return_value=([], AMPResponseMeta())))
        response = client.post(
            "/acps-amp-v1/system/events/query",
            json={"timeRange": {"startAt": "2026-06-24T00:00:00Z", "endAt": "2026-06-24T01:00:00Z"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
