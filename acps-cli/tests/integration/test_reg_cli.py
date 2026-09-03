"""集成测试：Registry 用户命令。

覆盖范围：
  - login（自动注册 + 登录）
  - logout（退出并清理登录态）
  - change-password（提示与会话保持）
  - whoami（需要有效 token）
  - list（列出 Agent）
  - upsert（创建/更新 Agent）
  - submit（提交 Agent 审核）
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from acps_cli.main import main
from tests._registry_mtls import RegistryMtlsSettings

pytestmark = pytest.mark.integration


def _login_user(runner: CliRunner, reg_conf: Path, username: str, password: str) -> None:
    result = runner.invoke(
        main,
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
    assert result.exit_code == 0, f"login 失败，输出: {result.output}"


def _login_admin(runner: CliRunner, reg_conf: Path, username: str, password: str) -> None:
    result = runner.invoke(
        main,
        [
            "--config",
            str(reg_conf),
            "admin",
            "auth",
            "login",
            "--username",
            username,
            "--password",
            password,
        ],
    )
    assert result.exit_code == 0, f"admin login 失败，输出: {result.output}"


def _create_submitted_ontology_agent(runner: CliRunner, reg_conf: Path, acs_path: Path) -> str:
    create_result = runner.invoke(
        main,
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
    assert create_result.exit_code == 0, f"ontology upsert 失败，输出: {create_result.output}"
    agent_id = json.loads(create_result.output)["agent_id"]

    submit_result = runner.invoke(
        main,
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
    assert submit_result.exit_code == 0, f"submit 失败，输出: {submit_result.output}"
    assert str(json.loads(submit_result.output).get("approval_status", "")).upper() == "PENDING"
    return agent_id


def _approve_agent(runner: CliRunner, reg_conf: Path, agent_id: str) -> str:
    approve_result = runner.invoke(
        main,
        [
            "--config",
            str(reg_conf),
            "admin",
            "registry",
            "review",
            "approve",
            "--agent-id",
            agent_id,
            "--json",
        ],
    )
    assert approve_result.exit_code == 0, f"approve 失败，输出: {approve_result.output}"
    approve_data = json.loads(approve_result.output)
    assert str(approve_data.get("approval_status", "")).upper() == "APPROVED"
    aic = approve_data.get("aic")
    assert isinstance(aic, str) and aic
    return aic


def _install_ontology_mtls_materials(work_dir: Path, ontology_aic: str, cert_path: Path, key_path: Path) -> None:
    target_dir = work_dir / ".registry-client" / "ontology-mtls" / ontology_aic.strip().upper()
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cert_path, target_dir / "certificate.pem")
    shutil.copyfile(key_path, target_dir / "private-key.pem")


def _write_entity_payload_file(work_dir: Path, label: str) -> Path:
    payload_path = work_dir / f"entity-{label}.json"
    payload_path.write_text(
        json.dumps(
            {
                "entityUserId": f"user-{label}",
                "entityMeta": {"scenario": "integration", "label": label},
                "endPoints": [
                    {
                        "url": f"https://entity-{label}.example.com/callback",
                        "transport": "JSONRPC",
                        "security": [],
                    }
                ],
                "certificate": {
                    "altNames": {
                        "dns": [f"entity-{label}.example.com"],
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


class TestRegCliLogin:
    """auth login 命令集成测试。"""

    def test_login_auto_register_new_user(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """新用户调用 login 时，自动注册并返回成功结果。"""
        username, password = user_credentials
        runner = CliRunner()

        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"login 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["username"] == username
        assert data["status"] in ("registered", "logged-in")
        assert data["has_refresh_token"] is True

    def test_login_existing_user(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """已注册用户二次调用 login 时，返回 logged-in 状态。"""
        username, password = user_credentials
        runner = CliRunner()

        # 先注册
        runner.invoke(
            main,
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

        # 再次登录
        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"二次登录失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "logged-in"

    def test_login_wrong_password_fails(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """已注册用户使用错误密码登录时，命令应以非零退出码退出。"""
        username, password = user_credentials
        runner = CliRunner()

        # 先注册
        runner.invoke(
            main,
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

        # 错误密码登录
        result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "auth",
                "login",
                "--username",
                username,
                "--password",
                "wrong-pass",
            ],
        )

        assert result.exit_code != 0


class TestRegCliWhoami:
    """auth whoami 命令集成测试。"""

    def test_whoami_returns_current_user(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """登录后调用 whoami 应返回当前用户信息。"""
        username, password = user_credentials
        runner = CliRunner()

        # 先登录
        runner.invoke(
            main,
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

        # 查询当前用户
        result = runner.invoke(main, ["--config", str(reg_conf), "auth", "whoami", "--json"])

        assert result.exit_code == 0, f"whoami 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data.get("username") == username

    def test_whoami_without_login_fails(
        self,
        work_dir: Path,
        reg_conf: Path,
    ) -> None:
        """未登录时调用 whoami 应以非零退出码退出。"""
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(reg_conf), "auth", "whoami"])
        assert result.exit_code != 0


class TestRegCliAuthLifecycle:
    """auth logout / change-password 命令集成测试。"""

    def test_logout_clears_local_token_and_invalidates_server_session(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
        token_file: Path,
    ) -> None:
        """logout 后本地 token 应删除，恢复旧 token 也不能再通过 whoami。"""
        username, password = user_credentials
        runner = CliRunner()

        _login_user(runner, reg_conf, username, password)
        assert token_file.exists(), f"token 文件未生成：{token_file}"
        saved_token = json.loads(token_file.read_text(encoding="utf-8"))

        result = runner.invoke(
            main,
            ["--config", str(reg_conf), "auth", "logout", "--json"],
        )

        assert result.exit_code == 0, f"logout 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["message"] == "Successfully logged out"
        assert data["local_token_cleared"] is True
        assert not token_file.exists(), "logout 后本地 token 文件仍存在"

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(saved_token, ensure_ascii=True, indent=2), encoding="utf-8")

        whoami_result = runner.invoke(main, ["--config", str(reg_conf), "auth", "whoami", "--json"])
        assert whoami_result.exit_code != 0, "恢复旧 token 后 whoami 不应继续成功"

    def test_change_password_keeps_current_session_and_requires_new_password_for_future_login(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
        token_file: Path,
    ) -> None:
        """change-password 应提示重新登录，但不主动清当前 token。"""
        username, old_password = user_credentials
        new_password = "Reset@Test67890"
        runner = CliRunner()

        _login_user(runner, reg_conf, username, old_password)
        assert token_file.exists(), f"token 文件未生成：{token_file}"

        change_result = runner.invoke(
            main,
            ["--config", str(reg_conf), "auth", "change-password", "--json"],
            input=f"{old_password}\n{new_password}\n{new_password}\n",
        )

        assert change_result.exit_code == 0, f"change-password 失败，输出: {change_result.output}"
        data = json.loads(change_result.output[change_result.output.find("{") :])
        assert data["success"] is True
        assert data["message"] == "Password updated successfully"
        assert data["notice"] == "密码已修改，建议重新登录"
        assert token_file.exists(), "change-password 不应主动清理当前 token 文件"

        whoami_result = runner.invoke(main, ["--config", str(reg_conf), "auth", "whoami", "--json"])
        assert whoami_result.exit_code == 0, f"改密后当前会话不应失效，输出: {whoami_result.output}"
        whoami_data = json.loads(whoami_result.output)
        assert whoami_data.get("username") == username

        old_login_result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "auth",
                "login",
                "--username",
                username,
                "--password",
                old_password,
            ],
        )
        assert old_login_result.exit_code != 0, "旧密码改后不应还能登录"

        new_login_result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "auth",
                "login",
                "--username",
                username,
                "--password",
                new_password,
                "--json",
            ],
        )
        assert new_login_result.exit_code == 0, f"新密码登录失败，输出: {new_login_result.output}"
        new_login_data = json.loads(new_login_result.output)
        assert new_login_data["username"] == username


class TestRegCliList:
    """agent list 命令集成测试。"""

    def test_list_returns_empty_for_new_user(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """新注册用户的 Agent 列表应为空。"""
        username, password = user_credentials
        runner = CliRunner()

        # 先登录
        runner.invoke(
            main,
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

        # 查询列表
        result = runner.invoke(main, ["--config", str(reg_conf), "agent", "list", "--json"])

        assert result.exit_code == 0, f"list 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_list_with_page_params(
        self,
        work_dir: Path,
        reg_conf: Path,
        user_credentials: tuple[str, str],
    ) -> None:
        """list 命令应支持 --page 和 --page-size 参数。"""
        username, password = user_credentials
        runner = CliRunner()

        runner.invoke(
            main,
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

        result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "agent",
                "list",
                "--page",
                "1",
                "--page-size",
                "5",
                "--json",
            ],
        )

        assert result.exit_code == 0, f"list 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data.get("page_size") == 5


class TestRegCliUpsert:
    """agent save 命令集成测试。"""

    def test_upsert_creates_new_agent(
        self,
        work_dir: Path,
        reg_conf: Path,
        acs_file: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """upsert 应成功创建新 Agent 并返回 created 动作。"""
        username, password = user_credentials
        acs_path, name, version = acs_file
        runner = CliRunner()

        # 先登录
        runner.invoke(
            main,
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

        # 创建 Agent
        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"agent save 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["action"] == "created"
        assert data["name"] == name
        assert data["version"] == version
        assert data.get("agent_id") is not None

    def test_upsert_updates_existing_agent(
        self,
        work_dir: Path,
        reg_conf: Path,
        acs_file: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """对同名同版本的 Agent 再次调用 upsert，应返回 updated 动作。"""
        username, password = user_credentials
        acs_path, _name, _version = acs_file
        runner = CliRunner()

        runner.invoke(
            main,
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
        # 第一次创建
        runner.invoke(
            main,
            ["--config", str(reg_conf), "agent", "save", "--acs-file", str(acs_path)],
        )

        # 第二次更新
        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"agent save 更新失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["action"] == "updated"


class TestRegCliSubmit:
    """agent submit 命令集成测试。"""

    def test_submit_puts_agent_in_pending(
        self,
        work_dir: Path,
        reg_conf: Path,
        acs_file: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """submit 应将 Agent 状态变为 PENDING。"""
        username, password = user_credentials
        acs_path, _, _ = acs_file
        runner = CliRunner()

        runner.invoke(
            main,
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

        # 创建 Agent
        create_result = runner.invoke(
            main,
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
        assert create_result.exit_code == 0
        agent_data = json.loads(create_result.output)
        agent_id = agent_data["agent_id"]

        # 提交审核
        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"submit 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert str(data.get("approval_status", "")).upper() == "PENDING"


class TestRegCliAtr:
    """Registry ATR 命令集成测试。"""

    def test_get_eab_returns_credentials_for_approved_ontology(
        self,
        work_dir: Path,
        reg_conf: Path,
        acs_file: tuple[Path, str, str],
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        username, password = user_credentials
        admin_username, admin_password = admin_credentials
        acs_path, _, _ = acs_file
        runner = CliRunner()

        _login_user(runner, reg_conf, username, password)
        agent_id = _create_submitted_ontology_agent(runner, reg_conf, acs_path)
        _login_admin(runner, reg_conf, admin_username, admin_password)
        aic = _approve_agent(runner, reg_conf, agent_id)

        output_path = work_dir / "eab.json"
        result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "cert",
                "eab",
                "fetch",
                "--aic",
                aic,
                "--output",
                str(output_path),
                "--json",
            ],
        )

        assert result.exit_code == 0, f"cert eab fetch 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["aic"] == aic
        assert output_path.exists()
        stored = json.loads(output_path.read_text(encoding="utf-8"))
        assert stored["aic"] == aic
        assert isinstance(stored.get("keyId"), str)
        assert isinstance(stored.get("macKey"), str)

    def test_register_entity_with_issued_ontology_certificate(
        self,
        work_dir: Path,
        reg_conf: Path,
        ca_conf: Path,
        acs_file: tuple[Path, str, str],
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
        registry_mtls_settings: RegistryMtlsSettings,
    ) -> None:
        if not registry_mtls_settings.is_available:
            pytest.skip(registry_mtls_settings.unavailable_reason)

        username, password = user_credentials
        admin_username, admin_password = admin_credentials
        acs_path, _, _ = acs_file
        runner = CliRunner()

        _login_user(runner, reg_conf, username, password)
        agent_id = _create_submitted_ontology_agent(runner, reg_conf, acs_path)
        _login_admin(runner, reg_conf, admin_username, admin_password)
        aic = _approve_agent(runner, reg_conf, agent_id)

        eab_path = work_dir / "ontology-eab.json"
        eab_result = runner.invoke(
            main,
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
            ],
        )
        assert eab_result.exit_code == 0, f"cert eab fetch 失败，输出: {eab_result.output}"

        cert_result = runner.invoke(
            main,
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
        assert cert_result.exit_code == 0, f"cert issue 失败，输出: {cert_result.output}"

        keyfiles_dir = work_dir / "keyfiles"
        cert_path = keyfiles_dir / "certs" / f"{aic}.pem"
        key_path = keyfiles_dir / "private" / f"{aic}.key"
        assert cert_path.exists(), f"证书文件未生成：{cert_path}"
        assert key_path.exists(), f"私钥文件未生成：{key_path}"

        _install_ontology_mtls_materials(work_dir, aic, cert_path, key_path)
        payload_path = _write_entity_payload_file(work_dir, aic.split(".")[-1])

        register_result = runner.invoke(
            main,
            [
                "--config",
                str(reg_conf),
                "entity",
                "derive",
                "--ontology-aic",
                aic,
                "--payload-file",
                str(payload_path),
                "--mtls-cert-file",
                str(cert_path),
                "--mtls-key-file",
                str(key_path),
                "--mtls-server-ca-file",
                str(registry_mtls_settings.ca_file),
                "--json",
            ],
        )

        assert register_result.exit_code == 0, f"register-entity 失败，输出: {register_result.output}"
        data = json.loads(register_result.output)
        assert data["approval_status"] == "APPROVED"
        assert isinstance(data.get("aic"), str) and data["aic"]
        assert data["entity"]["ontologyAic"] == aic
        assert data["entity"]["entityMeta"]["scenario"] == "integration"
        assert data["entity"]["certificate"]["altNames"]["dns"] == [f"entity-{aic.split('.')[-1]}.example.com"]
