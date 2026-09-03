"""端到端测试：cert 生命周期工作流。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from acps_cli.ca.keys import generate_private_key, load_private_key, save_private_key
from acps_cli.main import main as cli_main
from tests.e2e.conftest import make_acs_file

pytestmark = pytest.mark.e2e

user_main = cli_main
admin_main = cli_main
ca_main = cli_main


@dataclass(frozen=True)
class IssuedCertificateArtifacts:
    """记录生命周期测试需要复用的证书产物。"""

    acs_path: Path
    aic: str
    eab_path: Path
    cert_path: Path


def _issue_approved_agent_certificate(
    runner: CliRunner,
    *,
    work_dir: Path,
    reg_conf: Path,
    admin_conf: Path,
    ca_conf: Path,
    user_credentials: tuple[str, str],
    admin_credentials: tuple[str, str],
) -> IssuedCertificateArtifacts:
    """完成从注册到首次签发证书的最小闭环。"""
    username, password = user_credentials
    admin_username, admin_password = admin_credentials

    login_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "auth",
            "login",
            "--username",
            username,
            "--password",
            password,
            "--json",
        ],
    )
    assert login_result.exit_code == 0, f"login 失败: {login_result.output}"

    acs_path, _, _ = make_acs_file(work_dir)
    upsert_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "save",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )
    assert upsert_result.exit_code == 0, f"upsert 失败: {upsert_result.output}"
    agent_id = json.loads(upsert_result.output)["agent_id"]

    submit_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "submit",
            "--agent-id",
            agent_id,
            "--json",
        ],
    )
    assert submit_result.exit_code == 0, f"submit 失败: {submit_result.output}"

    admin_login_result = runner.invoke(
        admin_main,
        [
            "--config",
            str(admin_conf),
            "admin",
            "auth",
            "login",
            "--username",
            admin_username,
            "--password",
            admin_password,
            "--json",
        ],
    )
    assert admin_login_result.exit_code == 0, f"admin login 失败: {admin_login_result.output}"

    approve_result = runner.invoke(
        admin_main,
        [
            "--config",
            str(admin_conf),
            "admin",
            "registry",
            "review",
            "approve",
            "--agent-id",
            agent_id,
            "--json",
        ],
    )
    assert approve_result.exit_code == 0, f"approve 失败: {approve_result.output}"
    approve_payload = json.loads(approve_result.output)
    aic = str(approve_payload.get("aic") or "")
    assert aic, "审批后未获得 AIC"

    eab_path = work_dir / "eab.json"
    eab_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "cert",
            "eab",
            "fetch",
            "--aic",
            aic,
            "--output",
            str(eab_path),
            "--json",
        ],
    )
    assert eab_result.exit_code == 0, f"cert eab fetch 失败: {eab_result.output}"

    new_cert_result = runner.invoke(
        ca_main,
        [
            "--config",
            str(ca_conf),
            "cert",
            "issue",
            "--aic",
            aic,
            "--eab-file",
            str(eab_path),
            "--usage",
            "clientAuth",
        ],
    )
    assert new_cert_result.exit_code == 0, f"cert issue 失败: {new_cert_result.output}"

    cert_path = work_dir / "keyfiles" / "certs" / f"{aic}.pem"
    assert cert_path.exists(), f"证书文件未生成: {cert_path}"

    return IssuedCertificateArtifacts(
        acs_path=acs_path,
        aic=aic,
        eab_path=eab_path,
        cert_path=cert_path,
    )


def _read_first_certificate_serial(cert_path: Path) -> int:
    """读取证书链中的第一张证书序列号。"""
    cert_pem = cert_path.read_bytes()
    begin_marker = b"-----BEGIN CERTIFICATE-----"
    next_begin = cert_pem.find(begin_marker, len(begin_marker))
    first_cert_pem = cert_pem[:next_begin] if next_begin > 0 else cert_pem
    return x509.load_pem_x509_certificate(first_cert_pem).serial_number


def _wait_for_certificate_status(
    runner: CliRunner,
    *,
    ca_conf: Path,
    aic: str,
    expected_status: str,
) -> dict[str, object]:
    """轮询 OCSP 状态直到命中目标状态。"""
    for _ in range(15):
        ocsp_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "ocsp",
                "cert-status",
                "--aic",
                aic,
            ],
        )
        assert ocsp_result.exit_code == 0, f"OCSP 检查失败: {ocsp_result.output}"
        payload = json.loads(ocsp_result.output)
        if str(payload.get("certificateStatus") or "").upper() == expected_status:
            return payload
        time.sleep(2)

    pytest.fail(f"OCSP 未在预期时间内返回 {expected_status}")


def _refresh_crl(runner: CliRunner, *, ca_conf: Path) -> dict[str, object]:
    """刷新 CRL；管理员认证由测试配置统一准备。"""
    refresh_result = runner.invoke(
        ca_main,
        [
            "--config",
            str(ca_conf),
            "admin",
            "ca",
            "crl",
            "refresh",
        ],
    )
    if refresh_result.exit_code == 0:
        payload = json.loads(refresh_result.output)
        assert isinstance(payload, dict)
        return payload

    pytest.fail(f"refresh-crl 失败: {refresh_result.output}")


class TestCaLifecycleWorkflow:
    """验证 cert 生命周期主命令可组成完整闭环。"""

    def test_account_key_rollover_defaults_to_ed25519_and_keeps_account_usable(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        ca_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        runner = CliRunner()
        legacy_account_key_path = work_dir / "keyfiles" / "accounts" / "account.key"
        save_private_key(generate_private_key("ec"), str(legacy_account_key_path))

        issued = _issue_approved_agent_certificate(
            runner,
            work_dir=work_dir,
            reg_conf=reg_conf,
            admin_conf=admin_conf,
            ca_conf=ca_conf,
            user_credentials=user_credentials,
            admin_credentials=admin_credentials,
        )

        account_key_path = work_dir / "keyfiles" / "accounts" / f"{issued.aic}.account.key"
        assert account_key_path.exists(), f"canonical account key 未生成: {account_key_path}"

        initial_key = load_private_key(str(account_key_path))
        assert isinstance(
            initial_key,
            ec.EllipticCurvePrivateKey,
        ), "预置 legacy account key 未被用于首次 ACME account 创建"
        initial_key_pem = account_key_path.read_bytes()
        initial_serial = _read_first_certificate_serial(issued.cert_path)

        rollover_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "account-key",
                "rollover",
                "--aic",
                issued.aic,
                "--no-backup",
            ],
        )
        assert rollover_result.exit_code == 0, f"account-key rollover 失败: {rollover_result.output}"

        rolled_key = load_private_key(str(account_key_path))
        assert isinstance(rolled_key, ed25519.Ed25519PrivateKey), "未显式指定 --key-type 时 rollover 应默认生成 Ed25519"
        assert account_key_path.read_bytes() != initial_key_pem, "rollover 后 account key 文件内容未变化"
        assert list(account_key_path.parent.glob(f"{account_key_path.name}.bak-*")) == [], (
            "--no-backup 不应产生备份文件"
        )

        renew_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "renew",
                "--aic",
                issued.aic,
                "--eab-file",
                str(issued.eab_path),
                "--usage",
                "clientAuth",
                "--force",
            ],
        )
        assert renew_result.exit_code == 0, f"rollover 后使用新 account key 续期失败: {renew_result.output}"

        renewed_serial = _read_first_certificate_serial(issued.cert_path)
        assert renewed_serial != initial_serial, "rollover 后续期未签发新证书"

    def test_renew_and_download_crl_assets(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        ca_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        runner = CliRunner()
        issued = _issue_approved_agent_certificate(
            runner,
            work_dir=work_dir,
            reg_conf=reg_conf,
            admin_conf=admin_conf,
            ca_conf=ca_conf,
            user_credentials=user_credentials,
            admin_credentials=admin_credentials,
        )

        initial_serial = _read_first_certificate_serial(issued.cert_path)

        trust_bundle_path = work_dir / "manual-trust-bundle.pem"
        trust_bundle_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "trust-bundle",
                "update",
                "--output",
                str(trust_bundle_path),
            ],
        )
        assert trust_bundle_result.exit_code == 0, f"cert trust-bundle update 失败: {trust_bundle_result.output}"
        assert trust_bundle_path.exists(), "trust bundle 文件未生成"
        assert trust_bundle_path.stat().st_size > 0, "trust bundle 文件为空"

        renewed_trust_bundle_path = work_dir / "renewed-trust-bundle.pem"
        renew_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "renew",
                "--aic",
                issued.aic,
                "--eab-file",
                str(issued.eab_path),
                "--usage",
                "clientAuth",
                "--force",
                "--trust-bundle-path",
                str(renewed_trust_bundle_path),
            ],
        )
        assert renew_result.exit_code == 0, f"cert renew 失败: {renew_result.output}"

        renewed_serial = _read_first_certificate_serial(issued.cert_path)
        assert renewed_serial != initial_serial, "续期后证书序列号未变化"
        assert renewed_trust_bundle_path.exists(), "续期后的 trust bundle 未生成"
        assert renewed_trust_bundle_path.stat().st_size > 0, "续期后的 trust bundle 为空"

        crl_der_path = work_dir / "current.crl"
        download_der_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "crl",
                "download",
                "--output",
                str(crl_der_path),
                "--format",
                "der",
            ],
        )
        assert download_der_result.exit_code == 0, f"download-crl der 失败: {download_der_result.output}"
        assert crl_der_path.exists(), "DER CRL 文件未生成"
        assert crl_der_path.stat().st_size > 0, "DER CRL 文件为空"

        crl_pem_path = work_dir / "current-crl.pem"
        download_pem_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "crl",
                "download",
                "--output",
                str(crl_pem_path),
                "--format",
                "pem",
            ],
        )
        assert download_pem_result.exit_code == 0, f"download-crl pem 失败: {download_pem_result.output}"
        assert crl_pem_path.exists(), "PEM CRL 文件未生成"
        assert crl_pem_path.stat().st_size > 0, "PEM CRL 文件为空"
        assert b"BEGIN X509 CRL" in crl_pem_path.read_bytes(), "PEM CRL 文件格式不正确"

    def test_revoke_cert_updates_ocsp_when_admin_refresh_is_available(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        ca_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        runner = CliRunner()
        issued = _issue_approved_agent_certificate(
            runner,
            work_dir=work_dir,
            reg_conf=reg_conf,
            admin_conf=admin_conf,
            ca_conf=ca_conf,
            user_credentials=user_credentials,
            admin_credentials=admin_credentials,
        )

        revoke_result = runner.invoke(
            ca_main,
            [
                "--config",
                str(ca_conf),
                "cert",
                "revoke",
                "--aic",
                issued.aic,
                "--reason",
                "superseded",
            ],
        )
        assert revoke_result.exit_code == 0, f"revoke-cert 失败: {revoke_result.output}"

        _refresh_crl(runner, ca_conf=ca_conf)

        revoked_payload = _wait_for_certificate_status(
            runner,
            ca_conf=ca_conf,
            aic=issued.aic,
            expected_status="REVOKED",
        )
        assert revoked_payload.get("revocationTime"), "REVOKED 响应缺少 revocationTime"

    def test_refresh_crl_generates_new_crl_when_admin_auth_is_ready(
        self,
        ca_conf: Path,
    ) -> None:
        runner = CliRunner()
        refresh_payload = _refresh_crl(runner, ca_conf=ca_conf)
        assert isinstance(refresh_payload, dict)
