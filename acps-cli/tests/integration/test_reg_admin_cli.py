"""集成测试：Registry 管理员命令。

覆盖范围：
  - login（管理员登录）
  - logout（管理员退出）
  - list（待审核 Agent 列表）
  - approve（审核通过）
  - reject（审核驳回）
  - reset-password（管理员重置其它用户密码）
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from acps_cli.main import main

pytestmark = pytest.mark.integration


# ─── 辅助工具：用正式 CLI 注册并提交一个 Agent ────────────────────────────────


def _login_user(runner: CliRunner, reg_conf: Path, username: str, password: str) -> None:
    """使用普通用户身份登录。"""
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
    assert result.exit_code == 0, f"user login 失败，输出: {result.output}"


def _register_and_submit_agent(
    work_dir: Path,
    reg_conf: Path,
    username: str,
    password: str,
) -> tuple[str, str]:
    """注册新用户，创建并提交一个 Agent，返回 (agent_id, acs_name)。"""
    runner = CliRunner()

    # 注册 / 登录
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

    # 创建 ACS 文件（包含所有服务端必需字段）
    name = f"admin-test-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    acs = {
        "aic": "",
        "active": False,
        "lastModifiedTime": now,
        "protocolVersion": "02.02",
        "name": name,
        "version": "1.0.0",
        "description": "管理员集成测试用 Agent",
        "provider": {
            "organization": "Test Org",
            "url": "https://test.example.org",
            "license": "TEST-LICENSE",
        },
        "securitySchemes": {"mtls": {"type": "mutualTLS", "description": "Agent 间 mTLS 双向认证"}},
        "endPoints": [
            {
                "url": "https://localhost:9000/rpc",
                "transport": "JSONRPC",
                "security": [{"mtls": []}],
            }
        ],
        "capabilities": {"streaming": False, "notification": False, "messageQueue": []},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": f"{name}.skill",
                "name": "Test Skill",
                "description": "集成测试用技能",
                "version": "1.0.0",
                "tags": ["test"],
                "examples": ["test query"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        ],
    }
    acs_path = work_dir / f"acs_{name}.json"
    acs_path.write_text(json.dumps(acs, ensure_ascii=False, indent=2), encoding="utf-8")

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
    assert create_result.exit_code == 0, f"upsert 失败: {create_result.output}"
    agent_data = json.loads(create_result.output)
    agent_id = agent_data["agent_id"]

    # 提交审核
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
    assert submit_result.exit_code == 0, f"submit 失败: {submit_result.output}"

    return agent_id, name


# ─── 管理员配置文件 fixture ───────────────────────────────────────────────────


@pytest.fixture()
def admin_conf(work_dir: Path, registry_url: str) -> Path:
    """在工作目录写入管理员 acps-cli.toml。"""
    conf = work_dir / "acps-cli.toml"
    conf.write_text(
        "[registry]\n"
        f'base_url = "{registry_url}"\n\n'
        "[auth]\n"
        f'user_token_file = "{work_dir / ".acps-cli" / "tokens" / "registry-user.json"}"\n'
        f'admin_token_file = "{work_dir / ".acps-cli" / "tokens" / "registry-admin.json"}"\n',
        encoding="utf-8",
    )
    return conf


# ─── 管理员已登录 fixture ────────────────────────────────────────────────────


@pytest.fixture()
def logged_in_admin(
    work_dir: Path,
    admin_conf: Path,
    admin_credentials: tuple[str, str],
) -> tuple[Path, str, str]:
    """以管理员身份登录，返回 (admin_conf, username, password)。"""
    username, password = admin_credentials
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config",
            str(admin_conf),
            "admin",
            "auth",
            "login",
            "--username",
            username,
            "--password",
            password,
        ],
    )
    assert result.exit_code == 0, f"管理员登录失败: {result.output}"
    return admin_conf, username, password


class TestRegAdminCliLogin:
    """admin auth login 命令集成测试。"""

    def test_admin_login_success(
        self,
        work_dir: Path,
        admin_conf: Path,
        admin_credentials: tuple[str, str],
    ) -> None:
        """admin 登录应返回 Login successful。"""
        username, password = admin_credentials
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "auth",
                "login",
                "--username",
                username,
                "--password",
                password,
                "--json",
            ],
        )

        assert result.exit_code == 0, f"admin login 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["message"] == "Login successful"
        assert data["username"] == username

    def test_admin_login_wrong_password_fails(
        self,
        work_dir: Path,
        admin_conf: Path,
        admin_credentials: tuple[str, str],
    ) -> None:
        """管理员错误密码登录应以非零退出码退出。"""
        username, _ = admin_credentials
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "auth",
                "login",
                "--username",
                username,
                "--password",
                "wrong-pass",
            ],
        )

        assert result.exit_code != 0

    def test_admin_logout_clears_local_token_and_invalidates_server_session(
        self,
        work_dir: Path,
        logged_in_admin: tuple[Path, str, str],
        admin_token_file: Path,
    ) -> None:
        """admin logout 后本地 token 应删除，恢复旧 token 也不能再通过 whoami。"""
        admin_conf, _, _ = logged_in_admin
        runner = CliRunner()

        assert admin_token_file.exists(), f"管理员 token 文件未生成：{admin_token_file}"
        saved_token = json.loads(admin_token_file.read_text(encoding="utf-8"))

        result = runner.invoke(
            main,
            ["--config", str(admin_conf), "admin", "auth", "logout", "--json"],
        )

        assert result.exit_code == 0, f"admin logout 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["message"] == "Successfully logged out"
        assert data["local_token_cleared"] is True
        assert not admin_token_file.exists(), "admin logout 后本地 token 文件仍存在"

        admin_token_file.parent.mkdir(parents=True, exist_ok=True)
        admin_token_file.write_text(json.dumps(saved_token, ensure_ascii=True, indent=2), encoding="utf-8")

        whoami_result = runner.invoke(main, ["--config", str(admin_conf), "admin", "auth", "whoami", "--json"])
        assert whoami_result.exit_code != 0, "恢复旧管理员 token 后 whoami 不应继续成功"


class TestRegAdminCliList:
    """admin registry review list 命令集成测试。"""

    def test_list_returns_items(
        self,
        work_dir: Path,
        reg_conf: Path,
        logged_in_admin: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """list 命令应返回包含 items 和 total 字段的结果。"""
        admin_conf, _, _ = logged_in_admin
        username, password = user_credentials

        # 提交一个 Agent 使待审列表不为空
        _register_and_submit_agent(work_dir, reg_conf, username, password)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "registry",
                "review",
                "list",
                "--json",
            ],
        )

        assert result.exit_code == 0, f"list 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert "items" in data
        assert "total" in data

    def test_list_with_page_params(
        self,
        work_dir: Path,
        logged_in_admin: tuple[Path, str, str],
    ) -> None:
        """list 命令应支持 --page 和 --page-size 参数。"""
        admin_conf, _, _ = logged_in_admin
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "registry",
                "review",
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


class TestRegAdminCliApprove:
    """admin registry review approve 命令集成测试。"""

    def test_approve_sets_agent_approved(
        self,
        work_dir: Path,
        reg_conf: Path,
        logged_in_admin: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """审核通过后，Agent 状态应变为 APPROVED 并分配 AIC。"""
        admin_conf, _, _ = logged_in_admin
        username, password = user_credentials

        agent_id, _ = _register_and_submit_agent(work_dir, reg_conf, username, password)

        runner = CliRunner()
        result = runner.invoke(
            main,
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

        assert result.exit_code == 0, f"approve 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["message"] == "Approved"
        assert str(data.get("approval_status", "")).upper() == "APPROVED"
        assert data.get("aic") is not None  # 审核通过后应分配 AIC


class TestRegAdminCliReject:
    """admin registry review reject 命令集成测试。"""

    def test_reject_sets_agent_rejected(
        self,
        work_dir: Path,
        reg_conf: Path,
        logged_in_admin: tuple[Path, str, str],
        user_credentials: tuple[str, str],
    ) -> None:
        """审核驳回后，Agent 状态应变为 REJECTED。"""
        admin_conf, _, _ = logged_in_admin
        username, password = user_credentials

        agent_id, _ = _register_and_submit_agent(work_dir, reg_conf, username, password)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "registry",
                "review",
                "reject",
                "--agent-id",
                agent_id,
                "--comments",
                "集成测试驳回",
                "--json",
            ],
        )

        assert result.exit_code == 0, f"reject 失败，输出: {result.output}"
        data = json.loads(result.output)
        assert data["message"] == "Rejected"
        assert str(data.get("approval_status", "")).upper() == "REJECTED"


class TestRegAdminCliUserResetPassword:
    """admin registry user reset-password 命令集成测试。"""

    def test_reset_password_replaces_target_user_login_password(
        self,
        work_dir: Path,
        admin_conf: Path,
        logged_in_admin: tuple[Path, str, str],
        user_credentials: tuple[str, str],
        token_file: Path,
    ) -> None:
        """管理员重置指定用户密码后，旧会话与旧密码都应失效，新密码应生效。"""
        del logged_in_admin
        username, old_password = user_credentials
        new_password = "AdminReset@Test67890"
        runner = CliRunner()

        _login_user(runner, admin_conf, username, old_password)
        assert token_file.exists(), f"目标用户 token 文件未生成：{token_file}"

        whoami_result = runner.invoke(main, ["--config", str(admin_conf), "auth", "whoami", "--json"])
        assert whoami_result.exit_code == 0, f"获取目标用户信息失败，输出: {whoami_result.output}"
        user_id = json.loads(whoami_result.output)["id"]

        reset_result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "admin",
                "registry",
                "user",
                "reset-password",
                "--user-id",
                user_id,
                "--json",
            ],
            input=f"{new_password}\n{new_password}\n",
        )

        assert reset_result.exit_code == 0, f"reset-password 失败，输出: {reset_result.output}"
        data = json.loads(reset_result.output[reset_result.output.find("{") :])
        assert data["message"] == "Password reset successfully"
        assert data["user_id"] == user_id

        revoked_session_result = runner.invoke(main, ["--config", str(admin_conf), "auth", "whoami", "--json"])
        assert revoked_session_result.exit_code != 0, "管理员重置后目标用户旧会话不应继续有效"

        old_login_result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
                "auth",
                "login",
                "--username",
                username,
                "--password",
                old_password,
            ],
        )
        assert old_login_result.exit_code != 0, "管理员重置后旧密码不应还能登录"

        new_login_result = runner.invoke(
            main,
            [
                "--config",
                str(admin_conf),
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
