from pathlib import Path

from acps_cli.registry.config import CliOverrides, Config


def test_config_prefers_env_over_toml(monkeypatch):
    monkeypatch.setenv("REGISTRY_SERVER_BASE_URL", "http://from-env:9001/api/v1")
    monkeypatch.delenv("REGISTRY_API_BASE_URL", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://from-toml:9001/api/v1"})

    assert cfg.server_base_url == "http://from-env:9001/api/v1"


def test_config_prefers_cli_over_env(monkeypatch):
    monkeypatch.setenv("REGISTRY_SERVER_BASE_URL", "http://from-env:9001/api/v1")
    monkeypatch.delenv("REGISTRY_API_BASE_URL", raising=False)

    cfg = Config(
        toml_section={},
        overrides=CliOverrides(server_base_url="http://from-cli:9001/api/v1"),
    )

    assert cfg.server_base_url == "http://from-cli:9001/api/v1"


def test_config_reads_toml_when_no_env(monkeypatch):
    monkeypatch.delenv("REGISTRY_SERVER_BASE_URL", raising=False)
    monkeypatch.delenv("REGISTRY_API_BASE_URL", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://from-toml:9001/api/v1"})

    assert cfg.server_base_url == "http://from-toml:9001/api/v1"


def test_token_file_default_path(monkeypatch):
    monkeypatch.delenv("REGISTRY_TOKEN_FILE", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://localhost:9001/api/v1"})

    assert cfg.token_file == Path.cwd() / ".registry-client" / "registry-user.json"


def test_token_file_default_with_config_file_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("REGISTRY_TOKEN_FILE", raising=False)

    cfg = Config(
        toml_section={"server_base_url": "http://localhost:9001/api/v1"},
        config_file_dir=tmp_path,
    )

    assert cfg.token_file == tmp_path / ".registry-client" / "registry-user.json"


def test_atr_base_url_default_path(monkeypatch):
    monkeypatch.delenv("REGISTRY_ATR_BASE_URL", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://localhost:9001/api/v1"})

    assert cfg.atr_base_url == "http://localhost:9001/acps-atr-v2"


def test_atr_base_url_supports_legacy_api_path(monkeypatch):
    monkeypatch.delenv("REGISTRY_ATR_BASE_URL", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://localhost:9000/registry/api"})

    assert cfg.atr_base_url == "http://localhost:9000/registry/acps-atr-v2"


def test_atr_base_url_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("REGISTRY_ATR_BASE_URL", "http://override-host:9000/registry/acps-atr-v2")

    cfg = Config(toml_section={})

    assert cfg.atr_base_url == "http://override-host:9000/registry/acps-atr-v2"


def test_mtls_base_url_default_path(monkeypatch):
    monkeypatch.delenv("REGISTRY_MTLS_BASE_URL", raising=False)

    cfg = Config(toml_section={"server_base_url": "http://localhost:9001/api/v1"})

    assert cfg.mtls_base_url == "http://localhost:9002"


def test_mtls_base_url_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("REGISTRY_MTLS_BASE_URL", "https://registry.example.com:9443")

    cfg = Config(toml_section={"server_base_url": "http://localhost:9001/api/v1"})

    assert cfg.mtls_base_url == "https://registry.example.com:9443"


def test_ontology_mtls_materials_dir_resolves_relative_to_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("REGISTRY_ONTOLOGY_MTLS_MATERIALS_DIR", raising=False)

    cfg = Config(
        toml_section={"server_base_url": "http://localhost:9001/api/v1"},
        config_file_dir=tmp_path,
    )

    assert cfg.ontology_mtls_materials_dir == tmp_path / ".registry-client" / "ontology-mtls"


def test_mtls_server_ca_file_supports_relative_path(tmp_path, monkeypatch):
    monkeypatch.delenv("REGISTRY_MTLS_SERVER_CA_FILE", raising=False)

    cfg = Config(
        toml_section={
            "server_base_url": "http://localhost:9001/api/v1",
            "mtls_server_ca_file": "./certs/registry-ca.pem",
        },
        config_file_dir=tmp_path,
    )

    assert cfg.mtls_server_ca_file == tmp_path / "certs" / "registry-ca.pem"


def test_config_reads_user_credentials(monkeypatch):
    monkeypatch.setenv("REGISTRY_USER_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_USER_PASSWORD", "demo123")

    cfg = Config(toml_section={})

    assert cfg.username == "demo-client"
    assert cfg.password == "demo123"


def test_config_reads_admin_credentials(monkeypatch):
    monkeypatch.setenv("REGISTRY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REGISTRY_ADMIN_PASSWORD", "admin123")

    cfg = Config(toml_section={}, credential_env_prefix="REGISTRY_ADMIN")

    assert cfg.username == "admin"
    assert cfg.password == "admin123"


def test_config_reads_aliases_from_env(monkeypatch):
    """REGISTRY_API_BASE_URL 和 REGISTRY_CLIENT_* 别名仍可从环境变量读取。"""
    monkeypatch.setenv("REGISTRY_API_BASE_URL", "http://host.docker.internal:9001/api/v1")
    monkeypatch.delenv("REGISTRY_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("REGISTRY_CLIENT_USERNAME", "demo-client")
    monkeypatch.setenv("REGISTRY_CLIENT_PASSWORD", "demo123")
    monkeypatch.setenv("REGISTRY_CLIENT_NAME", "Demo Client")
    monkeypatch.setenv("REGISTRY_CLIENT_ORG", "Demo Organization")

    cfg = Config(toml_section={})

    assert cfg.server_base_url == "http://host.docker.internal:9001/api/v1"
    assert cfg.username == "demo-client"
    assert cfg.password == "demo123"
    assert cfg.display_name == "Demo Client"
    assert cfg.org_name == "Demo Organization"


def test_config_reads_admin_aliases_from_env(monkeypatch):
    """REGISTRY_ADMIN_* 凭证可从环境变量读取。"""
    monkeypatch.setenv("REGISTRY_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REGISTRY_ADMIN_PASSWORD", "admin123")

    cfg = Config(toml_section={}, credential_env_prefix="REGISTRY_ADMIN")

    assert cfg.username == "admin"
    assert cfg.password == "admin123"


def test_config_reads_registration_profile_from_toml():
    cfg = Config(
        toml_section={
            "display_name": "Demo Client",
            "org_name": "ACPs Demo",
        }
    )

    assert cfg.display_name == "Demo Client"
    assert cfg.org_name == "ACPs Demo"
