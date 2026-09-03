"""端到端测试：ATR-EAB 全链路工作流。

完整流程（对应 acps-infra/scripts/tests/run-atr-eab-e2e.sh 的 Python 版本）：

    1. auth login                    — 注册测试用户并获取 token
    2. agent save                    — 按 ACS 创建 Agent（DRAFT 状态）
    3. agent submit                  — 提交审核（→ PENDING）
    4. admin auth login              — 管理员登录
    5. admin registry review approve — 审核通过（→ APPROVED，分配 AIC）
    6. cert eab fetch                — 获取 EAB 凭证
    7. cert issue                    — 通过 ACME 协议申请 Agent 证书
  8. 验证证书内容（AIC、EKU、有效期）

每个测试用例均使用唯一用户名和 Agent 名称，测试之间相互隔离。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography import x509

from acps_cli.main import main as cli_main
from tests._registry_mtls import RegistryMtlsSettings
from tests.e2e.conftest import make_acs_file

pytestmark = pytest.mark.e2e

user_main = cli_main
admin_main = cli_main
ca_main = cli_main


def _complete_ontology_certificate_flow(
    work_dir: Path,
    reg_conf: Path,
    admin_conf: Path,
    ca_conf: Path,
    user_credentials: tuple[str, str],
    admin_credentials: tuple[str, str],
) -> dict[str, Any]:
    username, password = user_credentials
    admin_username, admin_password = admin_credentials
    runner = CliRunner()

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
    assert login_result.exit_code == 0, f"Step 1 login 失败: {login_result.output}"
    login_data = json.loads(login_result.output)
    assert login_data["status"] in ("registered", "logged-in")

    acs_path, _agent_name, _agent_version = make_acs_file(work_dir)
    upsert_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "save",
            "--acs-file",
            str(acs_path),
            "--ontology",
            "--json",
        ],
    )
    assert upsert_result.exit_code == 0, f"Step 2 upsert 失败: {upsert_result.output}"
    upsert_data = json.loads(upsert_result.output)
    assert upsert_data["action"] == "created"
    agent_id = upsert_data["agent_id"]
    assert agent_id

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
    assert submit_result.exit_code == 0, f"Step 3 submit 失败: {submit_result.output}"
    submit_data = json.loads(submit_result.output)
    assert str(submit_data.get("approval_status", "")).upper() == "PENDING"

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
    assert admin_login_result.exit_code == 0, f"Step 4 admin login 失败: {admin_login_result.output}"

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
    assert approve_result.exit_code == 0, f"Step 5 approve 失败: {approve_result.output}"
    approve_data = json.loads(approve_result.output)
    assert str(approve_data.get("approval_status", "")).upper() == "APPROVED"
    aic = approve_data.get("aic")
    assert aic, "审核通过后未分配 AIC"

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
    assert eab_result.exit_code == 0, f"Step 6 cert eab fetch 失败: {eab_result.output}"
    assert eab_path.exists(), "EAB 文件未生成"
    eab_data = json.loads(eab_path.read_text(encoding="utf-8"))
    assert isinstance(eab_data.get("keyId"), str)
    assert isinstance(eab_data.get("macKey"), str)
    assert eab_data.get("aic") == aic

    keyfiles_dir = ca_conf.parent / "keyfiles"
    cert_path = keyfiles_dir / "certs" / f"{aic}.pem"
    cert_result = runner.invoke(
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
    assert cert_result.exit_code == 0, f"Step 7 cert issue 失败: {cert_result.output}"

    account_key_path = keyfiles_dir / "accounts" / f"{aic}.account.key"
    private_key_path = keyfiles_dir / "private" / f"{aic}.key"
    csr_path = keyfiles_dir / "csr" / f"{aic}.csr"
    assert cert_path.exists(), f"证书文件未以 AIC 命名生成：{cert_path}"
    assert account_key_path.exists(), f"ACME account key 未以 AIC 命名生成：{account_key_path}"
    assert private_key_path.exists(), f"Agent private key 未以 AIC 命名生成：{private_key_path}"
    assert csr_path.exists(), f"CSR 文件未以 AIC 命名生成：{csr_path}"

    return {
        "aic": aic,
        "eab_path": eab_path,
        "cert_path": cert_path,
        "account_key_path": account_key_path,
        "private_key_path": private_key_path,
        "csr_path": csr_path,
    }


def _install_ontology_mtls_materials(work_dir: Path, ontology_aic: str, cert_path: Path, key_path: Path) -> None:
    target_dir = work_dir / ".registry-client" / "ontology-mtls" / ontology_aic.strip().upper()
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cert_path, target_dir / "certificate.pem")
    shutil.copyfile(key_path, target_dir / "private-key.pem")


def _write_entity_payload_file(work_dir: Path, ontology_aic: str) -> Path:
    payload_path = work_dir / "entity.json"
    payload_path.write_text(
        json.dumps(
            {
                "entityUserId": f"entity-{ontology_aic.split('.')[-1]}",
                "entityMeta": {"scenario": "e2e", "sourceOntology": ontology_aic},
                "endPoints": [
                    {
                        "url": f"https://entity-{ontology_aic.split('.')[-1]}.example.com/callback",
                        "transport": "JSONRPC",
                        "security": [],
                    }
                ],
                "certificate": {
                    "altNames": {
                        "dns": [f"entity-{ontology_aic.split('.')[-1]}.example.com"],
                    },
                    "requestedValidity": 365,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return payload_path


class TestFullAtrEabWorkflow:
    """完整 ATR-EAB 证书申请端到端流程。"""

    def test_register_approve_get_eab_and_cert(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        ca_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        """
        从用户注册到获取 Agent 证书的完整流程验证：
          注册 → 创建 Agent → 提交审核 → 管理员审批 → 获取 EAB → 申请证书
        """
        flow = _complete_ontology_certificate_flow(
            work_dir=work_dir,
            reg_conf=reg_conf,
            admin_conf=admin_conf,
            ca_conf=ca_conf,
            user_credentials=user_credentials,
            admin_credentials=admin_credentials,
        )
        aic = str(flow["aic"])
        cert_path = Path(flow["cert_path"])

        # ── 步骤 8：验证证书内容 ──────────────────────────────────────────────
        cert_pem = cert_path.read_bytes()
        # 解析 PEM 链中的第一张证书
        pem_header = b"-----BEGIN CERTIFICATE-----"
        first_cert_end = cert_pem.find(pem_header, len(pem_header))
        first_cert_pem = cert_pem[:first_cert_end] if first_cert_end > 0 else cert_pem

        cert = x509.load_pem_x509_certificate(first_cert_pem)

        # 验证 CN 中包含 AIC
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cn, "证书缺少 CN"
        assert aic in cn[0].value, f"证书 CN '{cn[0].value}' 不含 AIC '{aic}'"

        # 验证 EKU 包含 clientAuth
        try:
            eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
            eku_oids = [oid.dotted_string for oid in eku_ext.value]
            assert "1.3.6.1.5.5.7.3.2" in eku_oids, f"证书 EKU 不含 clientAuth: {eku_oids}"
        except x509.ExtensionNotFound:
            pytest.fail("证书缺少 ExtendedKeyUsage 扩展")

    def test_register_entity_after_issuing_ontology_certificate(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        ca_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
        registry_mtls_settings: RegistryMtlsSettings,
    ) -> None:
        if not registry_mtls_settings.is_available:
            pytest.skip(registry_mtls_settings.unavailable_reason)

        flow = _complete_ontology_certificate_flow(
            work_dir=work_dir,
            reg_conf=reg_conf,
            admin_conf=admin_conf,
            ca_conf=ca_conf,
            user_credentials=user_credentials,
            admin_credentials=admin_credentials,
        )
        ontology_aic = str(flow["aic"])
        cert_path = Path(flow["cert_path"])
        key_path = Path(flow["private_key_path"])

        _install_ontology_mtls_materials(work_dir, ontology_aic, cert_path, key_path)
        payload_path = _write_entity_payload_file(work_dir, ontology_aic)

        runner = CliRunner()
        register_result = runner.invoke(
            user_main,
            [
                "--config",
                str(reg_conf),
                "entity",
                "derive",
                "--ontology-aic",
                ontology_aic,
                "--payload-file",
                str(payload_path),
                "--json",
            ],
        )

        assert register_result.exit_code == 0, f"register-entity 失败: {register_result.output}"
        register_data = json.loads(register_result.output)
        assert register_data["approval_status"] == "APPROVED"
        assert isinstance(register_data.get("aic"), str) and register_data["aic"]
        assert register_data["entity"]["ontologyAic"] == ontology_aic
        assert register_data["entity"]["entityMeta"]["scenario"] == "e2e"
        assert register_data["entity"]["certificate"]["altNames"]["dns"] == [
            f"entity-{ontology_aic.split('.')[-1]}.example.com"
        ]


class TestRegistrationCheckWorkflow:
    """agent check 命令端到端测试。"""

    def test_check_shows_missing_for_new_acs(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """未提交的 ACS 检查应返回 missing 状态。"""
        username, password = user_credentials
        runner = CliRunner()

        runner.invoke(
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
            ],
        )

        acs_path, _, _ = make_acs_file(work_dir)
        result = runner.invoke(
            user_main,
            [
                "--config",
                str(reg_conf),
                "agent",
                "check",
                "--acs-file",
                str(acs_path),
                "--json",
            ],
        )

        assert result.exit_code == 0, f"check 失败: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "missing"

    def test_check_shows_draft_after_upsert(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """创建 Agent 后 check 应返回 draft 状态。"""
        username, password = user_credentials
        runner = CliRunner()

        runner.invoke(
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
            ],
        )

        acs_path, _, _ = make_acs_file(work_dir)
        runner.invoke(
            user_main,
            ["--config", str(reg_conf), "agent", "save", "--acs-file", str(acs_path)],
        )

        result = runner.invoke(
            user_main,
            [
                "--config",
                str(reg_conf),
                "agent",
                "check",
                "--acs-file",
                str(acs_path),
                "--json",
            ],
        )

        assert result.exit_code == 0, f"check 失败: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "draft"

    def test_check_shows_pending_after_submit(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """提交审核后 check 应返回 pending 状态。"""
        username, password = user_credentials
        runner = CliRunner()

        runner.invoke(
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
            ],
        )

        acs_path, _, _ = make_acs_file(work_dir)
        create_result = runner.invoke(
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
        agent_id = json.loads(create_result.output)["agent_id"]

        runner.invoke(
            user_main,
            ["--config", str(reg_conf), "agent", "submit", "--agent-id", agent_id],
        )

        result = runner.invoke(
            user_main,
            [
                "--config",
                str(reg_conf),
                "agent",
                "check",
                "--acs-file",
                str(acs_path),
                "--json",
            ],
        )

        assert result.exit_code == 0, f"check 失败: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "pending"
