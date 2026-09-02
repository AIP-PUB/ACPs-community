"""单元测试 - cert 账户密钥的一 AIC 一把模型。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from acps_cli.ca.acme import AcmeError
from acps_cli.ca.commands import (
    _load_eab_file,
    _resolve_account_key_path,
)
from acps_cli.ca.config import Config
from acps_cli.ca.keys import generate_private_key, save_private_key
from acps_cli.main import main as ca_main


def _write_ca_config(tmp_path: Path) -> Path:
    keyfiles = tmp_path / "keyfiles"
    (keyfiles / "certs").mkdir(parents=True, exist_ok=True)
    (keyfiles / "accounts").mkdir(parents=True, exist_ok=True)
    (keyfiles / "private").mkdir(parents=True, exist_ok=True)
    (keyfiles / "csr").mkdir(parents=True, exist_ok=True)

    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(
        "[ca]\n"
        'base_url = "http://localhost:9003"\n'
        f'account_keys_dir = "{keyfiles / "accounts"}"\n'
        f'private_keys_dir = "{keyfiles / "private"}"\n'
        f'certs_dir = "{keyfiles / "certs"}"\n'
        f'csr_dir = "{keyfiles / "csr"}"\n'
        f'trust_bundle_path = "{keyfiles / "trust-bundle.pem"}"\n',
        encoding="utf-8",
    )
    return config_path


def _write_eab_file(tmp_path: Path, aic: str) -> Path:
    eab_file = tmp_path / "eab.json"
    eab_file.write_text(
        json.dumps(
            {
                "keyId": "kid-1",
                "macKey": "bWFja2V5",
                "aic": aic,
            }
        ),
        encoding="utf-8",
    )
    return eab_file


@pytest.mark.unit
class TestAccountKeyModel:
    def test_relative_account_keys_dir_resolves_from_config_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "configs" / "acps-cli.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
            },
            config_file_path=str(config_path),
        )

        assert cfg.account_keys_dir == str((config_path.parent / "keyfiles" / "accounts").resolve())

    def test_relative_env_path_overrides_anchor_to_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "configs" / "acps-cli.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CA_ACCOUNT_KEYS_DIR", "./env-accounts")

        cfg = Config(
            {"server_base_url": "http://localhost:9003"},
            config_file_path=str(config_path),
        )

        assert cfg.account_keys_dir == str((config_path.parent / "env-accounts").resolve())

    def test_legacy_single_account_key_is_materialized_to_aic_scoped_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "acps-cli.toml"
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
            },
            config_file_path=str(config_path),
        )
        legacy_path = Path(cfg.account_keys_dir) / "account.key"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("legacy-account-key", encoding="utf-8")

        resolution = _resolve_account_key_path(cfg, "AIC-001", allow_legacy_materialization=True)

        assert resolution.exists is True
        assert resolution.source == "legacy"
        assert resolution.canonical_path == cfg.account_key_path_for("AIC-001")
        assert resolution.load_path == str(legacy_path)
        assert not Path(resolution.canonical_path).exists()
        assert legacy_path.exists()

    def test_legacy_single_account_key_requires_bound_issue_flow(self, tmp_path: Path) -> None:
        config_path = tmp_path / "acps-cli.toml"
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
            },
            config_file_path=str(config_path),
        )
        legacy_path = Path(cfg.account_keys_dir) / "account.key"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("legacy-account-key", encoding="utf-8")

        with pytest.raises(
            click.ClickException,
            match=r"Legacy shared account\.key can only be materialized during cert issue",
        ):
            _resolve_account_key_path(cfg, "AIC-001")

    def test_legacy_single_account_key_rejects_ambiguous_multi_aic_auto_migration(self, tmp_path: Path) -> None:
        config_path = tmp_path / "acps-cli.toml"
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
            },
            config_file_path=str(config_path),
        )
        legacy_path = Path(cfg.account_keys_dir) / "account.key"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("shared-legacy-account-key", encoding="utf-8")
        Path(cfg.account_key_path_for("AIC-001")).write_text("canonical-aic-001-key", encoding="utf-8")

        with pytest.raises(
            click.ClickException,
            match=r"Legacy shared account\.key cannot be auto-migrated",
        ):
            _resolve_account_key_path(cfg, "AIC-002", allow_legacy_materialization=True)

    def test_legacy_single_account_key_rejects_workspace_with_other_aic_artifacts(self, tmp_path: Path) -> None:
        config_path = tmp_path / "acps-cli.toml"
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
                "private_keys_dir": "./keyfiles/private",
            },
            config_file_path=str(config_path),
        )
        legacy_path = Path(cfg.account_keys_dir) / "account.key"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("shared-legacy-account-key", encoding="utf-8")

        other_private_key = Path(cfg.private_keys_dir) / "AIC-OTHER.key"
        other_private_key.parent.mkdir(parents=True, exist_ok=True)
        other_private_key.write_text("other-aic-private-key", encoding="utf-8")

        with pytest.raises(click.ClickException, match="workspace contains artifacts for another AIC"):
            _resolve_account_key_path(cfg, "AIC-001", allow_legacy_materialization=True)

    def test_legacy_single_account_key_allows_configured_trust_bundle_in_certs_dir(self, tmp_path: Path) -> None:
        config_path = tmp_path / "acps-cli.toml"
        cfg = Config(
            {
                "server_base_url": "http://localhost:9003",
                "account_keys_dir": "./keyfiles/accounts",
                "certs_dir": "./keyfiles/certs",
                "trust_bundle_path": "./keyfiles/certs/custom-bundle.pem",
            },
            config_file_path=str(config_path),
        )
        legacy_path = Path(cfg.account_keys_dir) / "account.key"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("shared-legacy-account-key", encoding="utf-8")

        trust_bundle_path = Path(cfg.trust_bundle_path)
        trust_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        trust_bundle_path.write_text("trust-bundle", encoding="utf-8")

        resolution = _resolve_account_key_path(cfg, "AIC-001", allow_legacy_materialization=True)

        assert resolution.exists is True
        assert resolution.source == "legacy"

    def test_eab_aic_mismatch_is_rejected(self, tmp_path: Path) -> None:
        eab_file = tmp_path / "eab.json"
        eab_file.write_text(
            json.dumps(
                {
                    "keyId": "kid-1",
                    "macKey": "bWFja2V5",
                    "aic": "AIC-OTHER",
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(click.ClickException, match="EAB AIC mismatch"):
            _load_eab_file(str(eab_file), expected_aic="AIC-EXPECTED")

    def test_issue_rejects_bound_legacy_account_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config_path = _write_ca_config(tmp_path)
        eab_file = _write_eab_file(tmp_path, "AIC-001")
        legacy_path = tmp_path / "keyfiles" / "accounts" / "account.key"
        save_private_key(generate_private_key("ec"), str(legacy_path))
        calls: list[bool] = []

        def _fake_new_account(
            self,
            contact=None,
            terms_of_service_agreed=True,
            only_return_existing=False,
            eab_credential=None,
        ):
            calls.append(only_return_existing)
            if only_return_existing:
                return {"status": "valid"}
            raise AssertionError("cert issue should stop before creating a new account")

        monkeypatch.setattr("acps_cli.ca.commands.AcmeClient.new_account", _fake_new_account)

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "issue",
                "--aic",
                "AIC-001",
                "--eab-file",
                str(eab_file),
                "--usage",
                "clientAuth",
            ],
        )

        assert result.exit_code != 0
        assert "Legacy shared account.key is already bound to an existing ACME account" in result.output
        assert calls == [True]
        assert not (tmp_path / "keyfiles" / "accounts" / "AIC-001.account.key").exists()

    def test_issue_materializes_legacy_account_key_only_after_unbound_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = _write_ca_config(tmp_path)
        eab_file = _write_eab_file(tmp_path, "AIC-001")
        legacy_path = tmp_path / "keyfiles" / "accounts" / "account.key"
        save_private_key(generate_private_key("ec"), str(legacy_path))
        calls: list[dict[str, bool]] = []

        def _fake_new_account(
            self,
            contact=None,
            terms_of_service_agreed=True,
            only_return_existing=False,
            eab_credential=None,
        ):
            calls.append(
                {
                    "only_return_existing": only_return_existing,
                    "has_eab": eab_credential is not None,
                }
            )
            if only_return_existing:
                raise AcmeError("Account does not exist", 404)
            return {"status": "valid"}

        monkeypatch.setattr("acps_cli.ca.commands.AcmeClient.new_account", _fake_new_account)
        monkeypatch.setattr(
            "acps_cli.ca.commands.AcmeClient.new_order",
            lambda self, aic, usage="clientAuth": {"finalize": "https://ca.example/finalize/1"},
        )
        monkeypatch.setattr(
            "acps_cli.ca.commands.AcmeClient.finalize_order",
            lambda self, finalize_url, csr_pem: {
                "status": "valid",
                "certificate": "https://ca.example/cert/1",
                "url": "https://ca.example/order/1",
            },
        )
        monkeypatch.setattr(
            "acps_cli.ca.commands.AcmeClient.get_certificate",
            lambda self, cert_url: b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        )
        monkeypatch.setattr(
            "acps_cli.ca.commands.httpx.get",
            lambda url, timeout=10: SimpleNamespace(
                content=b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                raise_for_status=lambda: None,
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "issue",
                "--aic",
                "AIC-001",
                "--eab-file",
                str(eab_file),
                "--usage",
                "clientAuth",
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls == [
            {"only_return_existing": True, "has_eab": False},
            {"only_return_existing": False, "has_eab": True},
        ]

        canonical_path = tmp_path / "keyfiles" / "accounts" / "AIC-001.account.key"
        assert canonical_path.exists()
        assert canonical_path.read_bytes() == legacy_path.read_bytes()

    def test_key_rollover_without_new_key_does_not_create_timestamp_copy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = _write_ca_config(tmp_path)
        canonical_path = tmp_path / "keyfiles" / "accounts" / "AIC-001.account.key"
        save_private_key(generate_private_key("ec"), str(canonical_path))

        def _fake_new_account(
            self,
            contact=None,
            terms_of_service_agreed=True,
            only_return_existing=False,
            eab_credential=None,
        ):
            assert only_return_existing is True
            self.account_url = "https://ca.example/acct/1"
            return {"status": "valid"}

        monkeypatch.setattr("acps_cli.ca.commands.AcmeClient.new_account", _fake_new_account)
        monkeypatch.setattr("acps_cli.ca.commands.AcmeClient.key_change", lambda self, new_key: None)

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "account-key",
                "rollover",
                "--aic",
                "AIC-001",
                "--no-backup",
            ],
        )

        assert result.exit_code == 0, result.output
        account_key_files = [path.name for path in canonical_path.parent.iterdir() if path.is_file()]
        assert canonical_path.name in account_key_files
        assert not any(name.startswith("account-new-") for name in account_key_files)

    def test_renew_cert_does_not_materialize_legacy_shared_account_key(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = _write_ca_config(tmp_path)
        eab_file = _write_eab_file(tmp_path, "AIC-001")
        legacy_path = tmp_path / "keyfiles" / "accounts" / "account.key"
        save_private_key(generate_private_key("ec"), str(legacy_path))

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "renew",
                "--aic",
                "AIC-001",
                "--eab-file",
                str(eab_file),
                "--usage",
                "clientAuth",
                "--force",
            ],
        )

        assert result.exit_code != 0
        assert "Legacy shared account.key can only be materialized during cert issue" in result.output
        assert not (tmp_path / "keyfiles" / "accounts" / "AIC-001.account.key").exists()

    def test_renew_cert_infers_existing_agent_key_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = _write_ca_config(tmp_path)
        eab_file = _write_eab_file(tmp_path, "AIC-001")
        existing_agent_key = tmp_path / "keyfiles" / "private" / "AIC-001.key"
        save_private_key(generate_private_key("rsa"), str(existing_agent_key))
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "acps_cli.ca.commands._run_new_cert",
            lambda ctx, aic, eab_file, usage, key_type, reuse_key, key_path, cert_path, trust_bundle_path, **kwargs: (
                captured.update(
                    {
                        "aic": aic,
                        "key_type": key_type,
                        "reuse_key": reuse_key,
                        "key_path": key_path,
                    }
                )
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "renew",
                "--aic",
                "AIC-001",
                "--eab-file",
                str(eab_file),
                "--usage",
                "clientAuth",
                "--force",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["key_type"] == "rsa"
        assert captured["reuse_key"] is True

    def test_renew_cert_defaults_to_ed25519_when_agent_key_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        config_path = _write_ca_config(tmp_path)
        eab_file = _write_eab_file(tmp_path, "AIC-001")
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "acps_cli.ca.commands._run_new_cert",
            lambda ctx, aic, eab_file, usage, key_type, reuse_key, key_path, cert_path, trust_bundle_path, **kwargs: (
                captured.update(
                    {
                        "aic": aic,
                        "key_type": key_type,
                        "reuse_key": reuse_key,
                    }
                )
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            ca_main,
            [
                "--config",
                str(config_path),
                "cert",
                "renew",
                "--aic",
                "AIC-001",
                "--eab-file",
                str(eab_file),
                "--usage",
                "clientAuth",
                "--force",
            ],
        )

        assert result.exit_code == 0, result.output
        assert captured["key_type"] == "ed25519"
        assert captured["reuse_key"] is True
