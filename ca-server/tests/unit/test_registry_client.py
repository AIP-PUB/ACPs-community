"""app.acme.registry_client 的单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.acme import registry_client as rc
from app.acme.registry_client import AgentInfo, RegistryClient, get_registry_client


class DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def settings_stub() -> SimpleNamespace:
    return SimpleNamespace(
        registry_server_url="http://registry.local/acps-atr-v2",
        registry_server_internal_url="",
        registry_server_timeout=1,
        registry_server_internal_api_token="service-token",
        external_service_max_retries=2,
        external_service_retry_delays_list=[0, 0, 0],
        registry_server_mock=False,
        build_agent_common_name=lambda aic: f"{aic}.agents.example.com",
    )


@pytest.fixture
def patch_settings(monkeypatch: pytest.MonkeyPatch, settings_stub: SimpleNamespace) -> None:
    monkeypatch.setattr(rc, "get_settings", lambda: settings_stub)


def _valid_agent_payload(aic: str = "AIC-001") -> dict[str, Any]:
    return {
        "aic": aic,
        "active": True,
        "name": "Agent Name",
        "version": "1.0.0",
        "provider": {"organization": "ACME", "department": "AI", "countryCode": "CN"},
        "endPoints": [],
        "capabilities": {},
        "skills": [],
        "certificate": {"altNames": {"dns": ["a.example.com"], "ip": ["127.0.0.1"]}, "requestedValidity": 365},
    }


def test_agent_info_methods_cover_subject_and_san() -> None:
    info = AgentInfo(_valid_agent_payload("AIC-100"))

    assert info.is_valid() is True
    assert info.get_certificate_subject_components()["CN"]
    assert info.get_certificate_dns_names() == ["a.example.com"]
    assert info.get_certificate_ip_addresses() == ["127.0.0.1"]
    assert info.get_certificate_validity_days(max_days=300) == 300


def test_registry_client_resolve_internal_base_url(patch_settings: None) -> None:
    client = RegistryClient()

    assert client.internal_base_url == "http://registry.local"


def test_registry_client_internal_base_url_from_explicit_setting(
    monkeypatch: pytest.MonkeyPatch, settings_stub: SimpleNamespace
) -> None:
    settings_stub.registry_server_internal_url = "http://internal.registry.local/"
    monkeypatch.setattr(rc, "get_settings", lambda: settings_stub)

    client = RegistryClient()

    assert client.internal_base_url == "http://internal.registry.local"


def test_get_auth_headers_with_and_without_token(patch_settings: None) -> None:
    client = RegistryClient()
    assert client._get_auth_headers()["Authorization"] == "Bearer service-token"

    client.service_token = ""
    assert "Authorization" not in client._get_auth_headers()


@pytest.mark.asyncio
async def test_make_request_with_retry_success_after_retries(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    calls = {"count": 0}

    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: object | None,
        ) -> None:
            _ = exc_type, exc, tb
            return

        async def request(self, method: str, url: str, **kwargs: Any) -> DummyResponse:
            _ = method, kwargs
            calls["count"] += 1
            if calls["count"] < 3:
                raise httpx.RequestError("network", request=httpx.Request("GET", url))
            return DummyResponse(200, {"ok": True})

    async def _noop_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    client = RegistryClient()
    response = await client._make_request_with_retry("GET", "http://registry.local/acs/AIC-001")

    assert response is not None
    assert response.status_code == 200
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_make_request_with_retry_returns_none_on_non_request_error(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    class FakeAsyncClient:
        def __init__(self, timeout: int):
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: object | None,
        ) -> None:
            _ = exc_type, exc, tb
            return

        async def request(self, method: str, url: str, **kwargs: Any) -> DummyResponse:
            _ = method, url, kwargs
            raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = RegistryClient()
    response = await client._make_request_with_retry("GET", "http://registry.local/acs/AIC-001")

    assert response is None


@pytest.mark.asyncio
async def test_consume_eab_credential_success(monkeypatch: pytest.MonkeyPatch, patch_settings: None) -> None:
    client = RegistryClient()

    async def _fake_request(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(200, {"macKey": "mac-key", "aic": "AIC-001"})

    monkeypatch.setattr(client, "_make_request_with_retry", _fake_request)

    result = await client.consume_eab_credential("kid-1")

    assert result == ("mac-key", "AIC-001")


@pytest.mark.asyncio
async def test_consume_eab_credential_returns_none_for_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    client = RegistryClient()

    async def _fake_request(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(200, {"macKey": 1, "aic": None})

    monkeypatch.setattr(client, "_make_request_with_retry", _fake_request)

    assert await client.consume_eab_credential("kid-1") is None


@pytest.mark.asyncio
async def test_validate_aic_returns_none_on_error_statuses(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    client = RegistryClient()

    async def _resp_404(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(404, {})

    async def _resp_403(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(403, {})

    async def _resp_500(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(500, {})

    monkeypatch.setattr(client, "_make_request_with_retry", _resp_404)
    assert await client.validate_aic_and_get_info("AIC-001") is None

    monkeypatch.setattr(client, "_make_request_with_retry", _resp_403)
    assert await client.validate_aic_and_get_info("AIC-001") is None

    monkeypatch.setattr(client, "_make_request_with_retry", _resp_500)
    assert await client.validate_aic_and_get_info("AIC-001") is None


@pytest.mark.asyncio
async def test_validate_aic_success_and_data_checks(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    client = RegistryClient()

    async def _ok(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(200, _valid_agent_payload("AIC-001"))

    monkeypatch.setattr(client, "_make_request_with_retry", _ok)

    info = await client.validate_aic_and_get_info("AIC-001")

    assert info is not None
    assert info.aic == "AIC-001"
    assert info.organization == "ACME"


@pytest.mark.asyncio
async def test_validate_aic_rejects_mismatch_inactive_and_missing_org(
    monkeypatch: pytest.MonkeyPatch,
    patch_settings: None,
) -> None:
    client = RegistryClient()

    async def _mismatch(*_args: Any, **_kwargs: Any) -> DummyResponse:
        return DummyResponse(200, _valid_agent_payload("AIC-OTHER"))

    async def _inactive(*_args: Any, **_kwargs: Any) -> DummyResponse:
        payload = _valid_agent_payload("AIC-001")
        payload["active"] = False
        return DummyResponse(200, payload)

    async def _missing_org(*_args: Any, **_kwargs: Any) -> DummyResponse:
        payload = _valid_agent_payload("AIC-001")
        payload["provider"]["organization"] = ""
        return DummyResponse(200, payload)

    monkeypatch.setattr(client, "_make_request_with_retry", _mismatch)
    assert await client.validate_aic_and_get_info("AIC-001") is None

    monkeypatch.setattr(client, "_make_request_with_retry", _inactive)
    assert await client.validate_aic_and_get_info("AIC-001") is None

    # organization 可选：个人身份认证 Agent 可能没有组织字段
    monkeypatch.setattr(client, "_make_request_with_retry", _missing_org)
    missing_org_info = await client.validate_aic_and_get_info("AIC-001")
    assert missing_org_info is not None
    assert missing_org_info.organization == ""


@pytest.mark.asyncio
async def test_mock_mode_paths(monkeypatch: pytest.MonkeyPatch, settings_stub: SimpleNamespace) -> None:
    settings_stub.registry_server_mock = True
    monkeypatch.setattr(rc, "get_settings", lambda: settings_stub)

    client = RegistryClient()

    info = await client.validate_aic_and_get_info("AIC-001")
    assert info is not None
    assert await client.consume_eab_credential("kid-1") is None
    assert await client.register_certificate_request("AIC-001", "ord-1") is True
    assert await client.notify_certificate_issued("AIC-001", "ord-1", "cert-1") is True
    assert await client.verify_agent_ownership("AIC-001", {}) is True


@pytest.mark.asyncio
async def test_non_mock_auxiliary_methods_return_true_by_default(patch_settings: None) -> None:
    client = RegistryClient()

    assert await client.register_certificate_request("AIC-001", "ord-1") is True
    assert await client.notify_certificate_issued("AIC-001", "ord-1", "cert-1") is True
    assert await client.verify_agent_ownership("AIC-001", {"sub": "x"}) is True


def test_get_registry_client_singleton(monkeypatch: pytest.MonkeyPatch, settings_stub: SimpleNamespace) -> None:
    monkeypatch.setattr(rc, "get_settings", lambda: settings_stub)
    monkeypatch.setattr(rc, "_registry_client", None)

    c1 = get_registry_client()
    c2 = get_registry_client()

    assert c1 is c2
