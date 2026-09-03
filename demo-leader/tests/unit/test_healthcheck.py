from __future__ import annotations

from types import SimpleNamespace

import pytest

from leader import healthcheck


def test_resolve_host_maps_wildcard_to_loopback(monkeypatch) -> None:
    monkeypatch.setenv("LEADER_API_HOST", "0.0.0.0")
    assert healthcheck._resolve_host() == "127.0.0.1"


def test_use_mtls_healthcheck_requires_identity_binding_and_callback(monkeypatch) -> None:
    monkeypatch.setattr(
        healthcheck,
        "settings",
        {"app": {"identity_binding_enabled": True, "callback_base_url": "https://leader.example.com"}},
    )
    assert healthcheck._use_mtls_healthcheck() is True

    monkeypatch.setattr(
        healthcheck,
        "settings",
        {"app": {"identity_binding_enabled": False, "callback_base_url": "https://leader.example.com"}},
    )
    assert healthcheck._use_mtls_healthcheck() is False


def test_resolve_host_and_port_from_settings(monkeypatch) -> None:
    monkeypatch.delenv("LEADER_API_HOST", raising=False)
    monkeypatch.delenv("LEADER_API_PORT", raising=False)
    monkeypatch.setattr(healthcheck, "settings", {"uvicorn": {"host": "127.0.0.2", "port": 9443}})

    assert healthcheck._resolve_host() == "127.0.0.2"
    assert healthcheck._resolve_port() == 9443

    monkeypatch.setenv("LEADER_API_PORT", "9555")
    assert healthcheck._resolve_port() == 9555


def test_build_https_context_requires_client_context(monkeypatch) -> None:
    monkeypatch.setattr(healthcheck, "_build_client_ssl_context", lambda _settings: None)

    with pytest.raises(RuntimeError, match="mTLS callback healthcheck"):
        healthcheck._build_https_context()


def test_build_https_context_disables_hostname_check(monkeypatch) -> None:
    ctx = SimpleNamespace(check_hostname=True)
    monkeypatch.setattr(healthcheck, "_build_client_ssl_context", lambda _settings: ctx)

    assert healthcheck._build_https_context() is ctx
    assert ctx.check_hostname is False


def test_main_uses_http_connection_and_closes(monkeypatch) -> None:
    created = []

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.closed = False
            created.append(self)

        def request(self, method: str, path: str) -> None:
            self.requested = (method, path)

        def getresponse(self):
            return SimpleNamespace(status=204)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(healthcheck, "_resolve_host", lambda: "127.0.0.1")
    monkeypatch.setattr(healthcheck, "_resolve_port", lambda: 9031)
    monkeypatch.setattr(healthcheck, "_use_mtls_healthcheck", lambda: False)
    monkeypatch.setattr(healthcheck.http.client, "HTTPConnection", FakeConnection)

    assert healthcheck.main() == 0
    assert created[0].requested == ("GET", healthcheck.HEALTH_PATH)
    assert created[0].closed is True


def test_main_uses_https_and_reports_bad_status_or_exception(monkeypatch) -> None:
    class BadStatusConnection:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False

        def request(self, method: str, path: str) -> None:
            pass

        def getresponse(self):
            return SimpleNamespace(status=503)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(healthcheck, "_resolve_host", lambda: "leader.local")
    monkeypatch.setattr(healthcheck, "_resolve_port", lambda: 9443)
    monkeypatch.setattr(healthcheck, "_use_mtls_healthcheck", lambda: True)
    monkeypatch.setattr(healthcheck, "_build_https_context", lambda: object())
    monkeypatch.setattr(healthcheck.http.client, "HTTPSConnection", BadStatusConnection)

    assert healthcheck.main() == 1

    monkeypatch.setattr(
        healthcheck.http.client,
        "HTTPSConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down")),
    )
    assert healthcheck.main() == 1
