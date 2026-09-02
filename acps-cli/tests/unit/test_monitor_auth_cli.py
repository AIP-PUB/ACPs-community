from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from acps_cli.main import main

MONITOR_OIDC_CONFIG = (
    '[monitor]\nbase_url = "http://localhost:9009"\n\n'
    '[monitor.auth]\nmode = "oidc"\n'
    'issuer = "https://issuer.example/realms/acps-monitor"\n'
    'client_id = "monitor-cli"\n'
)


class StubMonitorOidcSession:
    def __init__(self) -> None:
        self.login_calls = 0
        self.refresh_calls = 0

    def login(self, *, on_prompt, sleep_func=None):
        self.login_calls += 1
        on_prompt(
            SimpleNamespace(
                verification_uri="https://issuer.example/device",
                verification_uri_complete="https://issuer.example/device?code=MONITOR-CODE",
                user_code="MONITOR-CODE",
            )
        )
        return

    def whoami(self):
        return {
            "service": "monitor",
            "account_kind": "user",
            "auth_mode": "oidc",
            "preferred_username": "monitor-viewer",
            "roles": ["viewer"],
            "has_refresh_token": True,
        }

    def status(self):
        return {"authenticated": True, **self.whoami()}

    def refresh(self):
        self.refresh_calls += 1
        return

    def logout(self):
        return {"local_session_cleared": True, "revocation_attempted": True, "revoked": True}


def test_monitor_auth_login_rejected_when_mode_none(tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text('[monitor]\nbase_url = "http://localhost:9009"\n', encoding="utf-8")

    result = runner.invoke(main, ["--config", str(config_path), "monitor", "auth", "login"])

    assert result.exit_code != 0
    assert "not enabled" in result.output


def test_monitor_auth_login_uses_oidc_session(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(MONITOR_OIDC_CONFIG, encoding="utf-8")
    session = StubMonitorOidcSession()
    monkeypatch.setattr("acps_cli.monitor.commands.OidcAuthSessionManager", lambda auth_config: session)

    result = runner.invoke(main, ["--config", str(config_path), "monitor", "auth", "login", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["preferred_username"] == "monitor-viewer"
    assert data["authenticated"] is True
    assert session.login_calls == 1
    assert "MONITOR-CODE" in result.stderr


def test_monitor_auth_refresh_uses_oidc_session(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(MONITOR_OIDC_CONFIG, encoding="utf-8")
    session = StubMonitorOidcSession()
    monkeypatch.setattr("acps_cli.monitor.commands.OidcAuthSessionManager", lambda auth_config: session)

    result = runner.invoke(main, ["--config", str(config_path), "monitor", "auth", "refresh", "--json"])

    assert result.exit_code == 0
    assert session.refresh_calls == 1
