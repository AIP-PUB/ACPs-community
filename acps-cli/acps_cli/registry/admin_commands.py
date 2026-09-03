"""面向管理员/审核员的 Registry 管理命令实现。"""

from __future__ import annotations

from typing import cast

import click

from acps_cli.shared.cli_logging import setup_cli_logging
from acps_cli.shared.config import load_toml_config
from acps_cli.shared.flexible_group import FlexibleGroup

from .client import RegistryApiClient
from .config import CliOverrides, Config, ConfigError
from .exceptions import RegistryClientError
from .output import print_result

RELOGIN_RECOMMENDED_NOTICE = "密码已修改，建议重新登录"


def _setup_logging(verbose: bool) -> None:
    setup_cli_logging(verbose)


def _resolve_credentials(
    config: Config,
    username: str | None,
    password: str | None,
) -> tuple[str, str]:
    resolved_username = (username or config.username or "").strip()
    resolved_password = password or config.password

    if not resolved_username:
        resolved_username = click.prompt("Username", type=str).strip()
    if not resolved_password:
        resolved_password = click.prompt("Password", hide_input=True)

    if not resolved_username or not resolved_password:
        raise click.ClickException("Username and password are required")
    return resolved_username, resolved_password


def _prompt_password_change() -> tuple[str, str]:
    current_password = click.prompt("Current password", hide_input=True)
    new_password = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    if not current_password or not new_password:
        raise click.ClickException("Current password and new password are required")
    return current_password, new_password


def _prompt_new_password(new_password: str | None) -> str:
    if new_password:
        return new_password
    return cast("str", click.prompt("New password", hide_input=True, confirmation_prompt=True))


def _build_password_change_output(result: dict[str, object]) -> dict[str, object]:
    output = dict(result)
    output["notice"] = RELOGIN_RECOMMENDED_NOTICE
    return output


def _logout_with_local_cleanup(client: RegistryApiClient) -> dict[str, object]:
    try:
        result = client.logout()
    except RegistryClientError as exc:
        try:
            client.clear_token()
        except OSError as clear_exc:
            raise click.ClickException(f"{exc}; failed to clear local token: {clear_exc}") from clear_exc
        raise click.ClickException(f"{exc}; local token cleared") from exc

    try:
        client.clear_token()
    except OSError as exc:
        raise click.ClickException(f"Server logout succeeded but failed to clear local token: {exc}") from exc

    output = dict(result)
    output["local_token_cleared"] = True
    return output


@click.group(cls=FlexibleGroup)
@click.option("--config", "config_path", default=None, help="Path to acps-cli.toml config file")
@click.option("--server-url", default=None, help="Override registry server base URL")
@click.option("--timeout", type=int, default=None, help="Override request timeout seconds")
@click.option("--verbose", "verbose", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(
    ctx: click.Context,
    config_path: str | None,
    server_url: str | None,
    timeout: int | None,
    verbose: bool,
) -> None:
    """Registry 管理侧命令入口。"""
    _setup_logging(verbose)
    overrides = CliOverrides(
        server_base_url=server_url,
        timeout_seconds=timeout,
    )
    toml_data, resolved_path = load_toml_config(config_path)
    try:
        config = Config(
            toml_section=toml_data.get("registry", {}),
            overrides=overrides,
            credential_env_prefix="REGISTRY_ADMIN",
            default_token_name="registry-admin.json",  # noqa: S106
            config_file_dir=resolved_path.parent if resolved_path else None,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["client"] = RegistryApiClient(config)


@main.command("login")
@click.option("--username", default=None, help="Username")
@click.option("--password", default=None, help="Password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def login(ctx: click.Context, username: str | None, password: str | None, as_json: bool) -> None:
    """登录并将管理员访问令牌保存到本地。"""
    client: RegistryApiClient = ctx.obj["client"]
    config: Config = ctx.obj["config"]
    resolved_username, resolved_password = _resolve_credentials(config, username, password)
    try:
        result = client.login(username=resolved_username, password=resolved_password)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    output = {
        "message": "Login successful",
        "username": resolved_username,
        "token_type": result.get("token_type", "bearer"),
        "has_refresh_token": bool(result.get("refresh_token")),
    }
    print_result(output, as_json=as_json)


@main.command("change-password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def change_password(ctx: click.Context, as_json: bool) -> None:
    """交互式修改当前登录管理员密码。"""
    client: RegistryApiClient = ctx.obj["client"]
    current_password, new_password = _prompt_password_change()
    try:
        result = client.update_current_user_password(current_password, new_password)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(_build_password_change_output(result), as_json=as_json)


@main.command("logout")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def logout(ctx: click.Context, as_json: bool) -> None:
    """注销当前登录管理员，并清理本地 token。"""
    client: RegistryApiClient = ctx.obj["client"]
    print_result(_logout_with_local_cleanup(client), as_json=as_json)


@main.command("list")
@click.option("--page", "page_num", default=1, type=int, show_default=True)
@click.option("--page-size", default=20, type=int, show_default=True)
@click.option("--status", "statuses", multiple=True, help="Filter statuses, can repeat")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def list_reviews(
    ctx: click.Context,
    page_num: int,
    page_size: int,
    statuses: tuple[str, ...],
    as_json: bool,
) -> None:
    """查询待审核 Agent 列表。"""
    client: RegistryApiClient = ctx.obj["client"]
    status_list = list(statuses) if statuses else ["PENDING"]
    try:
        result = client.list_review_agents(page_num=page_num, page_size=page_size, statuses=status_list)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(result, as_json=as_json)


@main.command("approve")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--comments", default=None, help="Optional review comments")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def approve_review(ctx: click.Context, agent_id: str, comments: str | None, as_json: bool) -> None:
    """通过 Agent 审核请求。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.process_review(agent_id=agent_id, approve=True, comments=comments)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    output = {
        "message": "Approved",
        "agent_id": result.get("id"),
        "approval_status": result.get("approval_status"),
        "aic": result.get("aic"),
        "agent": result,
    }
    print_result(output, as_json=as_json)


@main.command("reject")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--comments", required=True, help="Reject reason")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def reject_review(ctx: click.Context, agent_id: str, comments: str, as_json: bool) -> None:
    """驳回 Agent 审核请求。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.process_review(agent_id=agent_id, approve=False, comments=comments)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    output = {
        "message": "Rejected",
        "agent_id": result.get("id"),
        "approval_status": result.get("approval_status"),
        "aic": result.get("aic"),
        "agent": result,
    }
    print_result(output, as_json=as_json)


@main.command("disable")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--reason", default="Staff disable", show_default=True, help="Disable reason")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def disable_agent(ctx: click.Context, agent_id: str, reason: str, as_json: bool) -> None:
    """禁用指定 Agent。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.disable_agent(agent_id=agent_id, reason=reason)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    output = {
        "message": "Disabled",
        "agent_id": result.get("id"),
        "aic": result.get("aic"),
        "is_disabled": result.get("is_disabled"),
        "disabled_reason": result.get("disabled_reason"),
        "agent": result,
    }
    print_result(output, as_json=as_json)


@main.command("enable")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def enable_agent(ctx: click.Context, agent_id: str, as_json: bool) -> None:
    """启用指定 Agent。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.enable_agent(agent_id=agent_id)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    output = {
        "message": "Enabled",
        "agent_id": result.get("id"),
        "aic": result.get("aic"),
        "is_disabled": result.get("is_disabled"),
        "agent": result,
    }
    print_result(output, as_json=as_json)


@main.command("reset-user-password")
@click.option("--user-id", required=True, help="Target user UUID")
@click.option("--new-password", default=None, help="New password for the target user")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def reset_user_password(ctx: click.Context, user_id: str, new_password: str | None, as_json: bool) -> None:
    """为指定用户重置密码。"""
    client: RegistryApiClient = ctx.obj["client"]
    resolved_password = _prompt_new_password(new_password)
    try:
        result = client.reset_user_password(user_id=user_id, new_password=resolved_password)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    output = dict(result)
    output["user_id"] = user_id
    print_result(output, as_json=as_json)


if __name__ == "__main__":
    main()
