import json
from types import SimpleNamespace

from click.testing import CliRunner

from acps_cli.main import main
from acps_cli.registry.exceptions import RegistryClientError

REGISTRY_OIDC_CONFIG = (
    '[registry]\nbase_url = "http://localhost:9001"\n\n'
    '[registry.auth]\nmode = "oidc"\n'
    'issuer = "https://issuer.example/realms/acps-registry"\n'
    'client_id = "registry-cli"\n'
)


class StubAdminClient:
    def __init__(self):
        self.login_calls: list[tuple[str, str]] = []
        self.password_change_calls: list[tuple[str, str]] = []
        self.logout_calls = 0
        self.clear_token_calls = 0
        self.logout_error: RegistryClientError | None = None
        self.disable_calls: list[tuple[str, str]] = []
        self.enable_calls: list[str] = []
        self.reset_user_password_calls: list[tuple[str, str]] = []

    def login(self, username: str, password: str):
        self.login_calls.append((username, password))
        return {
            "access_token": "token",
            "token_type": "bearer",
            "refresh_token": "refresh",
        }

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

    def list_review_agents(self, page_num: int, page_size: int, statuses: list[str]):
        return {"items": [{"id": "1", "approval_status": "PENDING"}], "total": 1}

    def process_review(self, agent_id: str, approve: bool, comments: str | None):
        return {
            "id": agent_id,
            "approval_status": "APPROVED" if approve else "REJECTED",
            "aic": "AIC-001",
            "approved": approve,
            "comments": comments,
        }

    def disable_agent(self, agent_id: str, reason: str):
        self.disable_calls.append((agent_id, reason))
        return {
            "id": agent_id,
            "aic": "AIC-001",
            "is_disabled": True,
            "disabled_reason": reason,
        }

    def enable_agent(self, agent_id: str):
        self.enable_calls.append(agent_id)
        return {
            "id": agent_id,
            "aic": "AIC-001",
            "is_disabled": False,
        }

    def reset_user_password(self, user_id: str, new_password: str):
        self.reset_user_password_calls.append((user_id, new_password))
        return {"message": "Password reset successfully"}


class StubOidcSession:
    def __init__(self):
        self.login_calls = 0

    def login(self, *, on_prompt, sleep_func=None):
        self.login_calls += 1
        on_prompt(
            SimpleNamespace(
                verification_uri="https://issuer.example/device",
                verification_uri_complete="https://issuer.example/device?code=ADMIN-CODE",
                user_code="ADMIN-CODE",
            )
        )
        return

    def whoami(self):
        return {
            "service": "registry",
            "account_kind": "admin",
            "auth_mode": "oidc",
            "preferred_username": "oidc-admin",
            "roles": ["admin"],
            "has_refresh_token": True,
        }

    def status(self):
        return {"authenticated": True, **self.whoami()}

    def refresh(self):
        return None

    def logout(self):
        return {"local_session_cleared": True, "revocation_attempted": True, "revoked": True}


def test_review_list_default_status(monkeypatch, empty_conf):
    runner = CliRunner()
    monkeypatch.setattr(
        "acps_cli.registry.unified.RegistryApiClient",
        lambda config: StubAdminClient(),
    )

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "registry", "review", "list", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total"] == 1


def test_review_reject(monkeypatch, empty_conf):
    runner = CliRunner()
    monkeypatch.setattr(
        "acps_cli.registry.unified.RegistryApiClient",
        lambda config: StubAdminClient(),
    )

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "admin",
            "registry",
            "review",
            "reject",
            "--agent-id",
            "agent-1",
            "--comments",
            "invalid acs",
        ],
    )

    assert result.exit_code == 0
    assert "Rejected" in result.output


def test_review_approve_json_contains_flat_fields(monkeypatch, empty_conf):
    runner = CliRunner()
    monkeypatch.setattr(
        "acps_cli.registry.unified.RegistryApiClient",
        lambda config: StubAdminClient(),
    )

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "admin",
            "registry",
            "review",
            "approve",
            "--agent-id",
            "agent-1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["agent_id"] == "agent-1"


def test_admin_login_uses_env_credentials(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    monkeypatch.setenv("REGISTRY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REGISTRY_ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "auth", "login", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["username"] == "admin"
    assert client.login_calls == [("admin", "admin123")]


def test_admin_login_prompts_for_credentials(monkeypatch, tmp_path, empty_conf):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("acps_cli.shared.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("REGISTRY_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("REGISTRY_ADMIN_PASSWORD", raising=False)
    client = StubAdminClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "auth", "login", "--json"],
        input="prompt-admin\nprompt-secret\n",
    )

    assert result.exit_code == 0
    data = json.loads(result.output[result.output.find("{") :])
    assert data["username"] == "prompt-admin"
    assert client.login_calls == [("prompt-admin", "prompt-secret")]


def test_disable_agent_json_contains_flat_fields(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "admin",
            "registry",
            "agent",
            "disable",
            "--agent-id",
            "agent-1",
            "--reason",
            "manual review",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["agent_id"] == "agent-1"
    assert data["is_disabled"] is True
    assert data["disabled_reason"] == "manual review"
    assert client.disable_calls == [("agent-1", "manual review")]


def test_enable_agent_json_contains_flat_fields(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "admin",
            "registry",
            "agent",
            "enable",
            "--agent-id",
            "agent-1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["agent_id"] == "agent-1"
    assert data["is_disabled"] is False
    assert client.enable_calls == ["agent-1"]


def test_admin_logout_clears_local_token_after_server_logout(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "auth", "logout", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["message"] == "Successfully logged out"
    assert data["local_token_cleared"] is True
    assert client.logout_calls == 1
    assert client.clear_token_calls == 1


def test_admin_logout_clears_local_token_even_when_server_logout_fails(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    client.logout_error = RegistryClientError("network down")
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        ["--config", str(empty_conf), "admin", "auth", "logout", "--json"],
    )

    assert result.exit_code != 0
    assert "local token cleared" in result.output
    assert client.logout_calls == 0
    assert client.clear_token_calls == 1


def test_admin_reset_user_password_prompts_for_new_password(monkeypatch, empty_conf):
    runner = CliRunner()
    client = StubAdminClient()
    monkeypatch.setattr("acps_cli.registry.unified.RegistryApiClient", lambda config: client)

    result = runner.invoke(
        main,
        [
            "--config",
            str(empty_conf),
            "admin",
            "registry",
            "user",
            "reset-password",
            "--user-id",
            "user-123",
            "--json",
        ],
        input="ResetPass123!\nResetPass123!\n",
    )

    assert result.exit_code == 0
    data = json.loads(result.output[result.output.find("{") :])
    assert data["message"] == "Password reset successfully"
    assert data["user_id"] == "user-123"
    assert client.reset_user_password_calls == [("user-123", "ResetPass123!")]


def test_admin_oidc_login_rejects_username_password_options(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: StubOidcSession())

    result = runner.invoke(
        main,
        ["--config", str(config_path), "admin", "auth", "login", "--username", "admin", "--json"],
    )

    assert result.exit_code != 0
    assert "--username" in result.output


def test_admin_oidc_login_uses_device_flow_summary(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    session = StubOidcSession()
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: session)

    result = runner.invoke(main, ["--config", str(config_path), "admin", "auth", "login", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["preferred_username"] == "oidc-admin"
    assert data["authenticated"] is True
    assert session.login_calls == 1
    assert "ADMIN-CODE" in result.stderr


def test_admin_oidc_reset_password_is_rejected(monkeypatch, tmp_path):
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    monkeypatch.setattr("acps_cli.registry.unified.OidcAuthSessionManager", lambda auth_config: StubOidcSession())

    result = runner.invoke(
        main,
        [
            "--config",
            str(config_path),
            "admin",
            "registry",
            "user",
            "reset-password",
            "--user-id",
            "user-123",
        ],
    )

    assert result.exit_code != 0
    assert "identity provider" in result.output
