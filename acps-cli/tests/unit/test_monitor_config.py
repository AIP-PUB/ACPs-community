from __future__ import annotations

from pathlib import Path

import pytest
from click import ClickException

from acps_cli.shared.runtime import RootCliRuntime
from acps_cli.shared.unified_config import build_monitor_auth_config, build_monitor_runtime_context


def _runtime(tmp_path: Path, toml_data: dict[str, object]) -> RootCliRuntime:
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text("# 测试生成\n", encoding="utf-8")
    return RootCliRuntime(
        config_path=str(config_path),
        verbose=False,
        toml_data=toml_data,
        resolved_config_path=config_path,
        config_dir=tmp_path,
    )


def test_monitor_runtime_context_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONITOR_BASE_URL", raising=False)
    monkeypatch.delenv("MONITOR_API_PREFIX", raising=False)
    monkeypatch.delenv("MONITOR_TIMEOUT_SECONDS", raising=False)

    context = build_monitor_runtime_context(_runtime(tmp_path, {}), cli_base_url=None, cli_api_prefix=None)

    assert context["server_base_url"] == "http://localhost:9009"
    assert context["api_prefix"] == "/acps-amp-v1"
    assert context["timeout_seconds"] == 15.0


def test_monitor_runtime_context_prefers_cli_over_env_and_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONITOR_BASE_URL", "http://env.example.test:9009")
    monkeypatch.setenv("MONITOR_API_PREFIX", "/env-prefix")
    monkeypatch.setenv("MONITOR_TIMEOUT_SECONDS", "18")

    context = build_monitor_runtime_context(
        _runtime(
            tmp_path,
            {"monitor": {"base_url": "http://toml.example.test:9009", "api_prefix": "/toml", "timeout_seconds": 21}},
        ),
        cli_base_url="http://cli.example.test:9009/",
        cli_api_prefix="/cli-prefix/",
    )

    assert context["server_base_url"] == "http://cli.example.test:9009"
    assert context["api_prefix"] == "/cli-prefix"
    assert context["timeout_seconds"] == 18.0


def test_monitor_runtime_context_uses_env_when_cli_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONITOR_BASE_URL", "http://env.example.test:9009/")
    monkeypatch.setenv("MONITOR_API_PREFIX", "/env-prefix/")
    monkeypatch.setenv("MONITOR_TIMEOUT_SECONDS", "19.5")

    context = build_monitor_runtime_context(
        _runtime(
            tmp_path,
            {"monitor": {"base_url": "http://toml.example.test:9009", "api_prefix": "/toml", "timeout_seconds": 21}},
        ),
        cli_base_url=None,
        cli_api_prefix=None,
    )

    assert context["server_base_url"] == "http://env.example.test:9009"
    assert context["api_prefix"] == "/env-prefix"
    assert context["timeout_seconds"] == 19.5


@pytest.mark.parametrize(
    ("cli_base_url", "toml_data", "env_value"),
    [
        ("localhost:9009", {}, None),
        ("", {}, None),
        (None, {"monitor": {"base_url": "localhost:9009"}}, None),
        (None, {"monitor": {"base_url": ""}}, None),
        (None, {}, "localhost:9009"),
        (None, {}, ""),
    ],
)
def test_monitor_runtime_context_rejects_invalid_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_base_url: str | None,
    toml_data: dict[str, object],
    env_value: str | None,
) -> None:
    monkeypatch.delenv("MONITOR_BASE_URL", raising=False)
    if env_value is not None:
        monkeypatch.setenv("MONITOR_BASE_URL", env_value)

    with pytest.raises(ClickException, match="Invalid MONITOR_BASE_URL"):
        build_monitor_runtime_context(_runtime(tmp_path, toml_data), cli_base_url=cli_base_url, cli_api_prefix=None)


@pytest.mark.parametrize(
    ("cli_api_prefix", "toml_data", "env_value"),
    [
        ("invalid", {}, None),
        ("", {}, None),
        (None, {"monitor": {"api_prefix": "invalid"}}, None),
        (None, {"monitor": {"api_prefix": ""}}, None),
        (None, {}, "invalid"),
        (None, {}, ""),
        ("/", {}, None),
    ],
)
def test_monitor_runtime_context_rejects_invalid_api_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_api_prefix: str | None,
    toml_data: dict[str, object],
    env_value: str | None,
) -> None:
    monkeypatch.delenv("MONITOR_API_PREFIX", raising=False)
    if env_value is not None:
        monkeypatch.setenv("MONITOR_API_PREFIX", env_value)

    with pytest.raises(ClickException, match="Invalid MONITOR_API_PREFIX"):
        build_monitor_runtime_context(_runtime(tmp_path, toml_data), cli_base_url=None, cli_api_prefix=cli_api_prefix)


@pytest.mark.parametrize(
    ("toml_data", "env_value"),
    [
        ({"monitor": {"timeout_seconds": 0}}, None),
        ({"monitor": {"timeout_seconds": -1}}, None),
        ({"monitor": {"timeout_seconds": "nope"}}, None),
        ({"monitor": {"timeout_seconds": ""}}, None),
        ({}, "0"),
        ({}, "-1"),
        ({}, "nope"),
        ({}, ""),
    ],
)
def test_monitor_runtime_context_rejects_invalid_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    toml_data: dict[str, object],
    env_value: str | None,
) -> None:
    monkeypatch.delenv("MONITOR_TIMEOUT_SECONDS", raising=False)
    if env_value is not None:
        monkeypatch.setenv("MONITOR_TIMEOUT_SECONDS", env_value)

    with pytest.raises(ClickException, match="Invalid MONITOR_TIMEOUT_SECONDS"):
        build_monitor_runtime_context(_runtime(tmp_path, toml_data), cli_base_url=None, cli_api_prefix=None)


def test_monitor_auth_config_defaults_to_none_mode(tmp_path: Path) -> None:
    config = build_monitor_auth_config(_runtime(tmp_path, {}))

    assert config.mode == "none"
    assert config.token_file.endswith("monitor-user.json")
    assert config.oidc is None


def test_monitor_auth_config_reads_oidc_settings_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPS_CLI_MONITOR_AUTH_MODE", "oidc")
    monkeypatch.setenv("ACPS_CLI_MONITOR_OIDC_ISSUER", "http://127.0.0.1:9080/realms/acps-monitor")
    monkeypatch.setenv("ACPS_CLI_MONITOR_OIDC_CLIENT_ID", "monitor-cli")
    monkeypatch.setenv("ACPS_CLI_MONITOR_OIDC_SCOPES", "openid,profile email")
    monkeypatch.setenv("ACPS_CLI_MONITOR_OIDC_REQUIRE_HTTPS", "false")
    monkeypatch.setenv("AUTH_MONITOR_TOKEN_FILE", "tokens/monitor.json")

    config = build_monitor_auth_config(_runtime(tmp_path, {}))

    assert config.mode == "oidc"
    assert config.token_file.endswith("tokens/monitor.json")
    assert config.oidc is not None
    assert config.oidc.issuer == "http://127.0.0.1:9080/realms/acps-monitor"
    assert config.oidc.client_id == "monitor-cli"
    assert config.oidc.scopes == ("openid", "profile", "email")
    assert config.oidc.require_https is False


@pytest.mark.parametrize(
    "toml_auth",
    [
        {"mode": "oidc", "client_id": "monitor-cli"},
        {"mode": "oidc", "issuer": "https://issuer.example/realms/acps-monitor"},
    ],
)
def test_monitor_auth_config_requires_issuer_and_client_id(tmp_path: Path, toml_auth: dict[str, object]) -> None:
    with pytest.raises(ClickException, match="required when mode=oidc"):
        build_monitor_auth_config(_runtime(tmp_path, {"monitor": {"auth": toml_auth}}))
