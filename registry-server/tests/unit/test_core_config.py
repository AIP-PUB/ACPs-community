from pathlib import Path

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://registry_test:registry_test@localhost:5432/registry_test",
    )
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SM4_ENCRYPTION_KEY", "00112233445566778899aabbccddeeff")
    monkeypatch.setenv("AIC_CRC_SALT", "0x12345678")
    monkeypatch.setenv("APP_ENV", "development")


def test_dsp_retention_settings_default_to_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("REGISTRY_SERVER_DSP_RETENTION_WINDOW_HOURS", raising=False)
    monkeypatch.delenv("REGISTRY_SERVER_DSP_RETENTION_MAX_RECORDS", raising=False)

    settings = Settings()

    assert settings.dsp_retention_window_hours == 168
    assert settings.dsp_retention_max_records == 100000


def test_dsp_retention_settings_can_be_overridden_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGISTRY_SERVER_DSP_RETENTION_WINDOW_HOURS", "0")
    monkeypatch.setenv("REGISTRY_SERVER_DSP_RETENTION_MAX_RECORDS", "1")

    settings = Settings()

    assert settings.dsp_retention_window_hours == 0
    assert settings.dsp_retention_max_records == 1


def test_smtp_settings_can_be_overridden_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("EMAIL_ADDRESS", "noreply@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "smtp-secret")

    settings = Settings()

    assert settings.smtp_server == "smtp.example.com"
    assert settings.smtp_port == "587"
    assert settings.email_address == "noreply@example.com"
    assert settings.email_password == "smtp-secret"


def _write_toml(config_dir: Path, name: str, body: str) -> None:
    (config_dir / name).write_text(body, encoding="utf-8")


def test_settings_prefer_cwd_config_for_packaged_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("REGISTRY_SERVER_ENABLE_MTLS_LISTENER", raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(
        config_dir,
        "default.toml",
        '[server]\nenable_mtls_listener = true\n\n[aic]\narsp_code = "1"\n',
    )
    _write_toml(config_dir, "production.toml", "[server]\nenable_mtls_listener = false\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.enable_mtls_listener is False
    assert settings.aic_arsp_code == "1"


def test_aic_arsp_code_defaults_to_spec_example(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = Settings()

    assert settings.aic_arsp_code == "1"


@pytest.mark.parametrize(("raw", "expected"), [("0001", "0001"), ("34c2", "34C2"), ("9Z", "9Z")])
def test_aic_arsp_code_normalizes_toml_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str,
    expected: str,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", f'[aic]\narsp_code = "{raw}"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_arsp_code == expected


@pytest.mark.parametrize("raw", ["0", "", "1234567", "34-C2"])
def test_aic_arsp_code_rejects_illegal_toml_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", f'[aic]\narsp_code = "{raw}"\n')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        Settings()


def test_aic_arsp_code_rejects_missing_section(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", "[server]\nenable_mtls_listener = true\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        Settings()


def test_aic_arsp_code_is_not_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGISTRY_AIC_ARSP_CODE", "9Z")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_arsp_code == "1"


def test_aic_protocol_version_defaults_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = Settings()

    assert settings.aic_protocol_version == "1"


def test_aic_protocol_version_reads_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\nprotocol_version = "a"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_protocol_version == "A"


def test_aic_protocol_version_accepts_unquoted_integer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\nprotocol_version = 2\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_protocol_version == "2"


def test_aic_protocol_version_missing_key_defaults_to_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_protocol_version == "1"


@pytest.mark.parametrize("raw", ['"0"', '""', '"12"', "true", "10"])
def test_aic_protocol_version_rejects_illegal_toml_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw: str,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", f'[aic]\narsp_code = "1"\nprotocol_version = {raw}\n')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        Settings()


def test_aic_protocol_version_is_not_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGISTRY_AIC_PROTOCOL_VERSION", "Z")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\nprotocol_version = "1"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_protocol_version == "1"


def test_aic_serial_len_defaults_to_six(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = Settings()

    assert settings.aic_ontology_serial_len == 6
    assert settings.aic_instance_serial_len == 6


def test_aic_serial_len_reads_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(
        config_dir,
        "default.toml",
        '[aic]\narsp_code = "1"\nontology_serial_len = 9\ninstance_serial_len = 4\n',
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_ontology_serial_len == 9
    assert settings.aic_instance_serial_len == 4


def test_aic_serial_len_missing_keys_default_to_six(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", '[aic]\narsp_code = "1"\n')
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_ontology_serial_len == 6
    assert settings.aic_instance_serial_len == 6


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("ontology_serial_len", "0"),
        ("ontology_serial_len", "10"),
        ("instance_serial_len", "-1"),
        ("instance_serial_len", '"6"'),
        ("ontology_serial_len", "true"),
    ],
)
def test_aic_serial_len_rejects_illegal_toml_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    raw: str,
) -> None:
    _set_required_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(config_dir, "default.toml", f'[aic]\narsp_code = "1"\n{key} = {raw}\n')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        Settings()


def test_aic_serial_len_is_not_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REGISTRY_AIC_ONTOLOGY_SERIAL_LEN", "9")
    monkeypatch.setenv("REGISTRY_AIC_INSTANCE_SERIAL_LEN", "9")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(
        config_dir,
        "default.toml",
        '[aic]\narsp_code = "1"\nontology_serial_len = 6\ninstance_serial_len = 6\n',
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.aic_ontology_serial_len == 6
    assert settings.aic_instance_serial_len == 6
