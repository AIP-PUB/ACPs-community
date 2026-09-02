from types import SimpleNamespace
from typing import Literal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app import main as app_main
from app.core.config import settings

pytestmark = pytest.mark.unit


class _ReadySession:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def __aenter__(self) -> _ReadySession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        return False

    async def execute(self, statement: object) -> SimpleNamespace:
        del statement
        if self._fail:
            raise OperationalError("SELECT 1", {}, Exception("db down"))
        return SimpleNamespace()


async def test_ready_returns_ready_when_database_is_available(
    main_app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_main, "AsyncSessionLocal", lambda: _ReadySession())

    response = await main_app_client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_ready_returns_503_when_database_is_unavailable(
    main_app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_main, "AsyncSessionLocal", lambda: _ReadySession(fail=True))

    response = await main_app_client.get("/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:acps:error:dependencies:dependency-unavailable",
        "status": 503,
        "title": "Dependency unavailable",
        "detail": "Database connectivity check failed",
        "error_name": "DEPENDENCY_UNAVAILABLE",
    }


async def test_build_app_returns_cors_headers_for_allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        settings._toml,
        "cors",
        {
            "enabled": True,
            "origins": ["http://localhost:9030"],
            "allow_methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type"],
            "expose_headers": ["X-Request-ID"],
            "allow_credentials": False,
            "max_age": 600,
        },
    )
    cors_app = app_main._build_app(title="cors-test", enable_atr_ip_restriction=False, enable_cors=True)

    async with AsyncClient(transport=ASGITransport(app=cors_app), base_url="http://test") as client:
        response = await client.options(
            "/health",
            headers={
                "Origin": "http://localhost:9030",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:9030"


async def test_build_app_does_not_set_cors_header_for_disallowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        settings._toml,
        "cors",
        {
            "enabled": True,
            "origins": ["http://localhost:9030"],
            "allow_methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type"],
            "allow_credentials": False,
        },
    )
    cors_app = app_main._build_app(title="cors-test", enable_atr_ip_restriction=False, enable_cors=True)

    async with AsyncClient(transport=ASGITransport(app=cors_app), base_url="http://test") as client:
        response = await client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
