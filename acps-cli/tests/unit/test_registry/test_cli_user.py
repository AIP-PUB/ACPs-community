import json
from types import SimpleNamespace

from click.testing import CliRunner

from acps_cli.ca import unified as ca_unified
from acps_cli.main import main
from acps_cli.registry.exceptions import RegistryClientError

REGISTRY_OIDC_CONFIG = (
    '[registry]\nbase_url = "http://localhost:9001"\n\n'
    '[registry.auth]\nmode = "oidc"\n'
    'issuer = "https://issuer.example/realms/acps-registry"\n'
    'client_id = "registry-cli"\n'
)


class StubClient:
    def __init__(self):
        self.submit_calls: list[str] = []
        self.login_or_register_calls: list[tuple[str, str, str | None, str | None]] = []
        self.password_change_calls: list[tuple[str, str]] = []
        self.logout_calls = 0
        self.clear_token_calls = 0
        self.logout_error: RegistryClientError | None = None
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.atr_calls: list[tuple[str, dict[str, object] | None]] = []
        self.get_eab_calls: list[str] = []
        self.find_by_aic_calls: list[str] = []
        self.find_by_name_version_calls: list[tuple[str, str]] = []
        self.get_my_agent_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.find_by_aic_result: dict | None = None
        self.find_by_name_version_result: dict | None = None
        self.get_my_agent_result: dict | None = None

    def login_or_register_user(self, username: str, password: str, name: str | None, org_name: str | None):
        self.login_or_register_calls.append((username, password, name, org_name))
        return {
            "status": "registered",
            "username": username,
            "token": {
                "access_token": "token",
                "token_type": "bearer",
                "refresh_token": "refresh",
            },
        }

    def whoami(self):
        return {"username": "alice", "roles": ["CLIENT"]}

    def update_current_user_password(self, old_password: str, new_password: str):
        self.password_change_calls.append((old_password, new_password))
        return {"success": True, "message": "Password updated successfully"}

    def logout(self):
        if self.logout_error is not None:
            raise self.logout_error
        self.logout_calls += 1
        return {"success": True, "message": "Successfully logged out"}

    def clear_token(self):
        self.clear_token_calls += 1

    def list_my_agents(self, page_num: int, page_size: int, statuses: list[str]):
        return {"items": [], "total": 0, "page_num": page_num, "page_size": page_size}

    def find_my_agent_by_name_version(self, name: str, version: str):
        self.find_by_name_version_calls.append((name, version))
        return self.find_by_name_version_result

    def find_my_agent_by_aic(self, aic: str):
        self.find_by_aic_calls.append(aic)
        return self.find_by_aic_result

    def get_my_agent(self, agent_id: str):
        self.get_my_agent_calls.append(agent_id)
        return self.get_my_agent_result or {
            "id": agent_id,
            "name": "demo-agent",
            "version": "1.0.0",
        }

    def create_agent(self, payload):
        self.create_calls.append(payload)
        return {"id": "agent-1", **payload}

    def update_agent(self, agent_id: str, payload):
        self.update_calls.append((agent_id, payload))
        return {"id": agent_id, **payload}

    def submit_agent(self, agent_id: str):
        self.submit_calls.append(agent_id)
        return {"id": agent_id, "approval_status": "PENDING"}

    def register_entity_via_atr(
        self,
        ontology_aic: str,
        entity_payload: dict[str, object] | None = None,
        **kwargs,
    ):
        self.atr_calls.append(
            (
                ontology_aic,
                (entity_payload | kwargs if entity_payload is not None else kwargs or None),
            )
        )
        return {
            "ontologyAic": ontology_aic,
            "entityAic": "1.2.3.4.5.6.7.8.9.10",
            "entityMeta": entity_payload.get("entityMeta") if entity_payload else None,
        }

    def get_eab_credential(self, aic: str):
        self.get_eab_calls.append(aic)
        return {
            "keyId": "kid-1",
            "macKey": "secret-mac",
            "aic": aic,
            "expiresAt": "2026-04-11T12:00:00+08:00",
        }

    def delete_agent(self, agent_id: str):
        self.delete_calls.append(agent_id)
        return {}


class StubOidcSession:
    def __init__(self):
        self.login_calls = 0
        self.refresh_calls = 0

    def login(self, *, on_prompt, sleep_func=None):
        self.login_calls += 1
        on_prompt(
            SimpleNamespace(
                verification_uri="https://issuer.example/device",
                verification_uri_complete="https://issuer.example/device?code=USER-CODE",
                user_code="USER-CODE",
            )
        )
        return

    def whoami(self):
        return {
            "service": "registry",
            "account_kind": "user",
            "auth_mode": "oidc",
            "preferred_username": "oidc-user",
            "roles": ["user"],
            "has_refresh_token": True,
        }

    def status(self):
        return {"authenticated": True, **self.whoami()}

    def refresh(self):
        self.refresh_calls += 1
        return

    def logout(self):
        return {"local_session_cleared": True, "revocation_attempted": True, "revoked": True}


def test_whoami_json_output(monkeypatch, empty_conf):
    runner = CliRunner()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: StubClient())

    result = runner.invoke(main, ["--config", str(empty_conf), "auth", "whoami", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["username"] == "alice"


def test_agent_submit(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()

    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "agent", "submit", "--agent-id", "abc"],
    )

    assert result.exit_code == 0
    assert client.submit_calls == ["abc"]


def test_login_uses_env_credentials(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setenv("REGISTRY_USER_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_USER_PASSWORD", "demo123")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "auth", "login", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "registered"
    assert client.login_or_register_calls == [("demo-client", "demo123", None, None)]


def test_login_reads_unified_base_url_env(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setenv("REGISTRY_BASE_URL", "http://localhost:9001")
    monkeypatch.setenv("REGISTRY_CLIENT_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_CLIENT_PASSWORD", "demo123")
    monkeypatch.setenv("REGISTRY_CLIENT_NAME", "Demo Client")
    monkeypatch.setenv("REGISTRY_CLIENT_ORG", "Demo Organization")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(main, ["--config", str(empty_conf), "auth", "login", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["username"] == "demo-client"
    assert data["name"] == "Demo Client"
    assert data["org_name"] == "Demo Organization"
    assert client.login_or_register_calls == [("demo-client", "demo123", "Demo Client", "Demo Organization")]


def test_login_passes_optional_registration_profile(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setenv("REGISTRY_USER_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_USER_PASSWORD", "demo123")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "auth",
            "login",
            "--name",
            "Demo Client",
            "--org-name",
            "ACPs Demo",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert client.login_or_register_calls == [("demo-client", "demo123", "Demo Client", "ACPs Demo")]


def test_login_prompts_for_credentials(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("acps_cli.shared.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("REGISTRY_USER_USERNAME", raising=False)
    monkeypatch.delenv("REGISTRY_USER_PASSWORD", raising=False)
    client = StubClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "auth", "login", "--json"],
        input="prompt-user\nprompt-pass\n",
    )

    assert result.exit_code == 0
    data = json.loads(result.output[result.output.find("{") :])
    assert data["username"] == "prompt-user"
    assert client.login_or_register_calls == [("prompt-user", "prompt-pass", None, None)]


def test_user_change_password_prompts_interactively(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "auth", "change-password", "--json"],
        input="OldPass123!\nNewPass456!\nNewPass456!\n",
    )

    assert result.exit_code == 0
    data = json.loads(result.output[result.output.find("{") :])
    assert data["success"] is True
    assert data["message"] == "Password updated successfully"
    assert data["notice"] == "密码已修改，建议重新登录"
    assert client.password_change_calls == [("OldPass123!", "NewPass456!")]


def test_admin_change_password_prompts_interactively(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "auth", "change-password", "--json"],
        input="AdminOld123!\nAdminNew456!\nAdminNew456!\n",
    )

    assert result.exit_code == 0
    data = json.loads(result.output[result.output.find("{") :])
    assert data["success"] is True
    assert data["message"] == "Password updated successfully"
    assert data["notice"] == "密码已修改，建议重新登录"
    assert client.password_change_calls == [("AdminOld123!", "AdminNew456!")]


def test_user_logout_clears_local_token_after_server_logout(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "auth", "logout", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["message"] == "Successfully logged out"
    assert data["local_token_cleared"] is True
    assert client.logout_calls == 1
    assert client.clear_token_calls == 1


def test_user_logout_clears_local_token_even_when_server_logout_fails(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    client.logout_error = RegistryClientError("network down")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "auth", "logout", "--json"],
    )

    assert result.exit_code != 0
    assert "local token cleared" in result.output
    assert client.logout_calls == 0
    assert client.clear_token_calls == 1


def test_login_reads_registration_profile_from_toml(monkeypatch, tmp_path):
    runner = CliRunner()
    client = StubClient()
    conf = tmp_path / "acps-cli.toml"
    conf.write_text(
        "[registry]\n"
        'base_url = "http://localhost:9001"\n'
        'mtls_base_url = "http://localhost:9002"\n'
        'display_name = "Demo Client"\n'
        'org_name = "ACPs Demo"\n\n'
        "[auth]\n"
        'user_token_file = "./.acps-cli/tokens/user.json"\n'
        'admin_token_file = "./.acps-cli/tokens/admin.json"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REGISTRY_USER_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_USER_PASSWORD", "demo123")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(main, ["--config", str(conf), "auth", "login", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["name"] == "Demo Client"
    assert data["org_name"] == "ACPs Demo"
    assert client.login_or_register_calls == [("demo-client", "demo123", "Demo Client", "ACPs Demo")]


def test_agent_upsert_uses_metadata_from_acs_file(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(
        json.dumps(
            {
                "name": "demo-agent",
                "version": "1.0.0",
                "description": "Demo agent",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "save",
            "--acs-file",
            str(acs_path),
            "--logo-url",
            "https://example.com/logo.png",
            "--json",
        ],
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(client.create_calls) == 1
    payload = client.create_calls[0]
    assert output["agent_id"] == "agent-1"
    assert output["action"] == "created"
    assert payload["name"] == "demo-agent"
    assert payload["version"] == "1.0.0"
    assert payload["description"] == "Demo agent"
    assert payload["logo_url"] == "https://example.com/logo.png"


def test_agent_upsert_requires_name_and_version_in_acs(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(json.dumps({"description": "Demo agent"}), encoding="utf-8")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "save",
            "--acs-file",
            str(acs_path),
        ],
    )

    assert result.exit_code != 0
    assert "ACS JSON must contain a non-empty string field: name" in result.output


def test_register_entity_invokes_atr_registration(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    payload_path = tmp_path / "entity.json"
    payload_path.write_text(
        json.dumps(
            {
                "entityMeta": {"region": "beijing"},
                "entityUserId": "user-001",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "entity",
            "derive",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
            "--payload-file",
            str(payload_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["approval_status"] == "APPROVED"
    assert data["aic"] == "1.2.3.4.5.6.7.8.9.10"
    assert client.atr_calls == [
        (
            "1.2.3.4.5.6.7.000000.9.10",
            {
                "entityMeta": {"region": "beijing"},
                "entityUserId": "user-001",
                "mtls_cert_file": None,
                "mtls_key_file": None,
                "mtls_server_ca_file": None,
            },
        )
    ]


def test_register_entity_forwards_certificate_payload(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    payload_path = tmp_path / "entity.json"
    payload_path.write_text(
        json.dumps(
            {
                "endPoints": [
                    {
                        "url": "https://entity.example.com/callback",
                        "transport": "JSONRPC",
                        "security": [],
                    }
                ],
                "certificate": {
                    "altNames": {"dns": ["entity.example.com"]},
                    "requestedValidity": 365,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "entity",
            "derive",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
            "--payload-file",
            str(payload_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert client.atr_calls == [
        (
            "1.2.3.4.5.6.7.000000.9.10",
            {
                "endPoints": [
                    {
                        "url": "https://entity.example.com/callback",
                        "transport": "JSONRPC",
                        "security": [],
                    }
                ],
                "certificate": {
                    "altNames": {"dns": ["entity.example.com"]},
                    "requestedValidity": 365,
                },
                "mtls_cert_file": None,
                "mtls_key_file": None,
                "mtls_server_ca_file": None,
            },
        )
    ]


def test_register_entity_rejects_unsupported_payload_fields(tmp_path, empty_conf):
    runner = CliRunner()
    payload_path = tmp_path / "entity.json"
    payload_path.write_text(json.dumps({"skills": []}), encoding="utf-8")

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "entity",
            "derive",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
            "--payload-file",
            str(payload_path),
        ],
    )

    assert result.exit_code != 0
    assert "unsupported fields: skills" in result.output


def test_get_eab_writes_secret_file(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    output_path = tmp_path / "eab.json"

    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "cert",
            "eab",
            "fetch",
            "--aic",
            "1.2.3",
            "--output",
            str(output_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["key_id"] == "kid-1"
    assert data["output"] == str(output_path)
    assert client.get_eab_calls == ["1.2.3"]

    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stored["macKey"] == "secret-mac"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_register_entity_accepts_mtls_url_after_derive(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    captured: dict[str, str] = {}

    def factory(config, *args, **kwargs):
        captured["mtls"] = config.mtls_base_url
        return client

    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", factory)

    result = runner.invoke(
        main,
        [
            "entity",
            "derive",
            "--config",
            str(empty_conf),
            "--mtls-url",
            "https://custom.example.com:8443",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["mtls"] == "https://custom.example.com:8443"
    assert client.atr_calls[0][0] == "1.2.3.4.5.6.7.000000.9.10"


def test_register_entity_accepts_mixed_entity_and_derive_option_order(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubClient()
    captured: dict[str, str] = {}

    def factory(config, *args, **kwargs):
        captured["mtls"] = config.mtls_base_url
        return client

    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", factory)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "entity",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
            "derive",
            "--mtls-url",
            "https://custom.example.com:8443",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["mtls"] == "https://custom.example.com:8443"


def test_cert_eab_fetch_server_url_after_subcommand_stays_on_registry(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    captured: dict[str, object] = {}
    output_path = tmp_path / "eab.json"

    def factory(config, *args, **kwargs):
        captured["registry"] = config.server_base_url
        return client

    def spy_ca(ctx, server_url):
        captured["ca"] = server_url
        original_build_ca_context(ctx, server_url)

    original_build_ca_context = ca_unified._build_ca_context
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", factory)
    monkeypatch.setattr(ca_unified, "_build_ca_context", spy_ca)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "cert",
            "eab",
            "fetch",
            "--server-url",
            "http://registry.override.test:9001",
            "--aic",
            "1.2.3",
            "--output",
            str(output_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["ca"] is None
    assert captured["registry"] == "http://registry.override.test:9001/api/v1"
    assert client.get_eab_calls == ["1.2.3"]


def test_submit_rejects_removed_ontology_mode(monkeypatch, empty_conf):
    runner = CliRunner()
    monkeypatch.setattr("acps_cli.registry.commands.RegistryApiClient", lambda config: StubClient())

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "submit",
            "--ontology-aic",
            "1.2.3.4.5.6.7.000000.9.10",
        ],
    )

    assert result.exit_code != 0
    assert "No such option: --ontology-aic" in result.output


def test_check_returns_missing_when_agent_not_found(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(
        json.dumps({"name": "demo-agent", "version": "1.0.0", "aic": ""}),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "check",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "missing"
    assert data["name"] == "demo-agent"
    assert client.find_by_name_version_calls == [("demo-agent", "1.0.0")]


def test_check_prefers_local_aic_lookup(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    client.find_by_aic_result = {
        "id": "agent-1",
        "name": "demo-agent",
        "version": "1.0.0",
        "approval_status": "APPROVED",
        "aic": "1.2.3",
        "is_deleted": False,
        "is_disabled": False,
    }
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(
        json.dumps({"name": "demo-agent", "version": "1.0.0", "aic": "1.2.3"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "check",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "approved"
    assert data["source"] == "aic"
    assert client.find_by_aic_calls == ["1.2.3"]


def test_delete_uses_located_agent_id(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    client.find_by_name_version_result = {
        "id": "agent-1",
        "name": "demo-agent",
        "version": "1.0.0",
        "approval_status": "DRAFT",
        "aic": None,
        "is_deleted": False,
        "is_disabled": False,
    }
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(
        json.dumps({"name": "demo-agent", "version": "1.0.0"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "delete",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "deleted"
    assert data["agent_id"] == "agent-1"
    assert client.delete_calls == ["agent-1"]


def test_sync_acs_updates_local_metadata(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    client = StubClient()
    client.find_by_name_version_result = {
        "id": "agent-1",
        "name": "demo-agent",
        "version": "1.0.0",
        "approval_status": "APPROVED",
        "aic": "1.2.3",
    }
    client.get_my_agent_result = {
        "id": "agent-1",
        "name": "demo-agent",
        "version": "1.0.0",
        "approval_status": "APPROVED",
        "aic": "1.2.3",
        "acs": {
            "active": True,
            "lastModifiedTime": "2026-04-01T00:00:00+08:00",
            "aic": "1.2.3",
        },
    }
    acs_path = tmp_path / "agent.json"
    acs_path.write_text(
        json.dumps({"name": "demo-agent", "version": "1.0.0", "aic": ""}),
        encoding="utf-8",
    )
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "agent",
            "sync",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "synced"
    updated = json.loads(acs_path.read_text(encoding="utf-8"))
    assert updated["aic"] == "1.2.3"
    assert updated["active"] is True
    assert updated["lastModifiedTime"] == "2026-04-01T00:00:00+08:00"


def test_oidc_login_rejects_username_password_options(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: StubOidcSession())

    result = runner.invoke(
        main,
        ["--config", str(config_path), "auth", "login", "--username", "alice", "--json"],
    )

    assert result.exit_code != 0
    assert "--username" in result.output


def test_oidc_login_uses_device_flow_summary(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    session = StubOidcSession()
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: session)

    result = runner.invoke(main, ["--config", str(config_path), "auth", "login", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["preferred_username"] == "oidc-user"
    assert data["authenticated"] is True
    assert session.login_calls == 1
    assert "USER-CODE" in result.stderr
    assert "access_token" not in result.stdout


def test_oidc_change_password_is_rejected(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: StubOidcSession())

    result = runner.invoke(main, ["--config", str(config_path), "auth", "change-password"])

    assert result.exit_code != 0
    assert "identity provider" in result.output


def test_oidc_refresh_uses_session_manager(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    session = StubOidcSession()
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: session)

    result = runner.invoke(main, ["--config", str(config_path), "auth", "refresh", "--json"])

    assert result.exit_code == 0
    assert session.refresh_calls == 1
