import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from acps_cli.registry.client import RegistryApiClient
from acps_cli.registry.config import Config
from acps_cli.registry.exceptions import RegistryClientError


def _build_client(tmp_path) -> RegistryApiClient:
    return RegistryApiClient(
        Config(
            toml_section={
                "server_base_url": "http://localhost:9001/api/v1",
                "ontology_mtls_materials_dir": str(tmp_path / "ontology-mtls"),
                "mtls_server_ca_file": str(tmp_path / "registry-ca.pem"),
            },
            config_file_dir=tmp_path,
        )
    )


def _write_test_certificate(cert_path: Path, common_name: str) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _write_test_key(key_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_matching_test_cert_and_key(cert_path: Path, key_path: Path, common_name: str) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_test_mtls_materials(client: RegistryApiClient, ontology_aic: str) -> tuple[Path, Path, Path]:
    cert_file = client.config.resolve_ontology_mtls_cert_file(ontology_aic)
    key_file = client.config.resolve_ontology_mtls_key_file(ontology_aic)
    ca_file = client.config.mtls_server_ca_file
    assert ca_file is not None
    _write_test_certificate(cert_file, ontology_aic)
    _write_test_key(key_file)
    _write_test_certificate(ca_file, "Registry Test CA")
    return cert_file, key_file, ca_file


def test_login_or_register_user_returns_login_result(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    register_calls: list[tuple[str, str, str | None, str | None]] = []

    monkeypatch.setattr(
        client,
        "login",
        lambda username, password: {
            "access_token": "token",
            "token_type": "bearer",
            "refresh_token": "refresh",
        },
    )
    monkeypatch.setattr(
        client,
        "register_user",
        lambda username, password, name=None, org_name=None: register_calls.append(
            (username, password, name, org_name)
        ),
    )

    result = client.login_or_register_user("demo-client", "demo123")

    assert result["status"] == "logged-in"
    assert result["username"] == "demo-client"
    assert register_calls == []


def test_login_or_register_user_registers_when_user_not_found(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    def fake_login(username: str, password: str):
        raise RegistryClientError(
            "API request failed: POST /auth/login",
            status_code=404,
            payload={"error_name": "USER_NOT_FOUND"},
        )

    monkeypatch.setattr(client, "login", fake_login)
    monkeypatch.setattr(
        client,
        "register_user",
        lambda username, password, name=None, org_name=None: {
            "access_token": "token",
            "token_type": "bearer",
            "refresh_token": "refresh",
        },
    )

    result = client.login_or_register_user(
        "demo-client",
        "demo123",
        name="Demo Client",
        org_name="ACPs Demo",
    )

    assert result["status"] == "registered"
    assert result["username"] == "demo-client"


def test_login_or_register_user_rejects_invalid_credentials(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    register_calls: list[tuple[str, str, str | None, str | None]] = []

    def fake_login(username: str, password: str):
        raise RegistryClientError(
            "API request failed: POST /auth/login",
            status_code=401,
            payload={"error_name": "invalid_credentials"},
        )

    monkeypatch.setattr(client, "login", fake_login)
    monkeypatch.setattr(
        client,
        "register_user",
        lambda username, password, name=None, org_name=None: register_calls.append(
            (username, password, name, org_name)
        ),
    )

    try:
        client.login_or_register_user("demo-client", "wrong-password")
    except RegistryClientError as exc:
        assert exc.error_name == "invalid_credentials"
    else:
        raise AssertionError("Expected RegistryClientError")

    assert register_calls == []


def test_logout_uses_auth_when_local_access_token_exists(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    client.token_store.save({"access_token": "token", "token_type": "bearer"})
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["auth_required"] = kwargs["auth_required"]
        return {"success": True, "message": "Successfully logged out"}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.logout()

    assert result["success"] is True
    assert captured == {
        "method": "POST",
        "path": "/auth/logout",
        "auth_required": True,
    }


def test_reset_user_password_uses_admin_endpoint(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["auth_required"] = kwargs["auth_required"]
        captured["json_body"] = kwargs["json_body"]
        return {"message": "Password reset successfully"}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.reset_user_password("user-123", "ResetPass123!")

    assert result["message"] == "Password reset successfully"
    assert captured == {
        "method": "PUT",
        "path": "/account/user/user-123/password",
        "auth_required": True,
        "json_body": {"new_password": "ResetPass123!"},
    }


def test_register_entity_via_atr_returns_result(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    ontology_aic = "1.2.3.4.5.6.7.000000.9.10"
    cert_file, key_file, ca_file = _write_test_mtls_materials(client, ontology_aic)

    def fake_request(method: str, path: str, **kwargs):
        assert method == "POST"
        assert path == "/acps-atr-v2/entity"
        assert kwargs["auth_required"] is True
        assert kwargs["base_url"] == client.base_mtls_url
        assert kwargs["cert"] == (str(cert_file), str(key_file))
        assert kwargs["verify"] == str(ca_file)
        assert kwargs["json_body"] == {
            "ontologyAic": ontology_aic,
            "entityMeta": {"region": "beijing"},
            "certificate": {
                "altNames": {"dns": ["entity.example.com"]},
                "requestedValidity": 365,
            },
        }
        return {
            "status": "ok",
            "result": {"entityAic": "1.2.3.4.5.6.7.8.9.10"},
        }

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.register_entity_via_atr(
        ontology_aic,
        entity_payload={
            "entityMeta": {"region": "beijing"},
            "certificate": {
                "altNames": {"dns": ["entity.example.com"]},
                "requestedValidity": 365,
            },
        },
    )

    assert result == {"entityAic": "1.2.3.4.5.6.7.8.9.10"}


def test_register_entity_via_atr_rejects_error_payload(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    ontology_aic = "1.2.3.4.5.6.7.000000.9.10"
    _write_test_mtls_materials(client, ontology_aic)

    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: {
            "status": "error",
            "error": {"message": "Ontology is not approved"},
        },
    )

    try:
        client.register_entity_via_atr(ontology_aic)
    except RegistryClientError as exc:
        assert str(exc) == "Ontology is not approved"
    else:
        raise AssertionError("Expected RegistryClientError")


def test_register_entity_via_atr_requires_matching_certificate(tmp_path):
    client = _build_client(tmp_path)
    ontology_aic = "1.2.3.4.5.6.7.000000.9.10"
    cert_file = client.config.resolve_ontology_mtls_cert_file(ontology_aic)
    key_file = client.config.resolve_ontology_mtls_key_file(ontology_aic)
    ca_file = client.config.mtls_server_ca_file
    assert ca_file is not None
    _write_test_certificate(cert_file, "WRONG-AIC")
    _write_test_key(key_file)
    _write_test_certificate(ca_file, "Registry Test CA")

    try:
        client.register_entity_via_atr(ontology_aic)
    except RegistryClientError as exc:
        assert "does not match ontology AIC" in str(exc)
    else:
        raise AssertionError("Expected RegistryClientError")


def test_request_with_client_cert_uses_ssl_context(tmp_path, monkeypatch):
    client = _build_client(tmp_path)
    ontology_aic = "1.2.3.4.5.6.7.000000.9.10"
    cert_file = client.config.resolve_ontology_mtls_cert_file(ontology_aic)
    key_file = client.config.resolve_ontology_mtls_key_file(ontology_aic)
    ca_file = client.config.mtls_server_ca_file
    assert ca_file is not None
    _write_matching_test_cert_and_key(cert_file, key_file, ontology_aic)
    _write_test_certificate(ca_file, "Registry Test CA")
    captured: dict[str, object] = {}

    class DummyResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json() -> dict[str, object]:
            return {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def request(self, method: str, url: str, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return DummyResponse()

    monkeypatch.setattr("acps_cli.registry.client.httpx.Client", DummyClient)

    result = client._request(
        "POST",
        "/acps-atr-v2/entity",
        auth_required=False,
        base_url=client.base_mtls_url,
        json_body={"ontologyAic": ontology_aic},
        cert=(str(cert_file), str(key_file)),
        verify=str(ca_file),
    )

    assert result == {}
    assert captured["method"] == "POST"
    assert captured["url"] == f"{client.base_mtls_url}/acps-atr-v2/entity"
    assert isinstance(captured["verify"], ssl.SSLContext)
    assert "cert" not in captured


def test_request_surfaces_nested_error_message(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    class DummyResponse:
        status_code = 409
        content = b'{"status":"error"}'
        text = '{"status":"error"}'

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "status": "error",
                "error": {
                    "code": 40901,
                    "message": "Service endpoint URL conflicts with existing entity",
                },
            }

    monkeypatch.setattr(
        "acps_cli.registry.client.httpx.request",
        lambda method, url, verify, **kwargs: DummyResponse(),
    )

    try:
        client._request("POST", "/acps-atr-v2/entity", auth_required=False)
    except RegistryClientError as exc:
        assert str(exc) == "Service endpoint URL conflicts with existing entity (status=409)"
    else:
        raise AssertionError("Expected RegistryClientError")


def test_find_my_agent_by_aic_passes_filter(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    def fake_request(method: str, path: str, **kwargs):
        assert method == "GET"
        assert path == "/agent/client"
        assert kwargs["params"]["page_num"] == 1
        assert kwargs["params"]["page_size"] == 100
        assert kwargs["params"]["aic"] == "1.2.3"
        assert kwargs["params"]["is_deleted"] == "false"
        return {"items": [{"id": "agent-1", "aic": "1.2.3"}]}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.find_my_agent_by_aic("1.2.3")

    assert result == {"id": "agent-1", "aic": "1.2.3"}


def test_delete_agent_uses_cleanup_reason(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    def fake_request(method: str, path: str, **kwargs):
        assert method == "DELETE"
        assert path == "/agent/client/agent-1"
        assert kwargs["json_body"] == "setup-agents cleanup"
        return {}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.delete_agent("agent-1")

    assert result == {}


def test_update_current_user_password_uses_account_endpoint(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    def fake_request(method: str, path: str, **kwargs):
        assert method == "PUT"
        assert path == "/account/me/password"
        assert kwargs["auth_required"] is True
        assert kwargs["json_body"] == {
            "old_password": "OldPass123!",
            "new_password": "NewPass456!",
        }
        return {"success": True, "message": "Password updated successfully"}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.update_current_user_password("OldPass123!", "NewPass456!")

    assert result == {"success": True, "message": "Password updated successfully"}


def test_get_eab_credential_uses_atr_endpoint(tmp_path, monkeypatch):
    client = _build_client(tmp_path)

    def fake_request(method: str, path: str, **kwargs):
        assert method == "POST"
        assert path == "/eab/1.2.3"
        assert kwargs["auth_required"] is True
        assert kwargs["base_url"] == client.base_atr_url
        return {
            "keyId": "kid-1",
            "macKey": "secret",
            "aic": "1.2.3",
            "expiresAt": "2026-04-11T12:00:00+08:00",
        }

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.get_eab_credential("1.2.3")

    assert result["keyId"] == "kid-1"
