"""面向普通用户的 Registry 命令实现。"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import click

from acps_cli.shared.cli_logging import setup_cli_logging
from acps_cli.shared.config import load_toml_config
from acps_cli.shared.flexible_group import FlexibleGroup

from .client import ENTITY_REGISTRATION_PAYLOAD_FIELDS, RegistryApiClient
from .config import CliOverrides, Config, ConfigError
from .exceptions import RegistryClientError
from .output import print_result

LOGGER = logging.getLogger(__name__)
ACS_FILE_REQUIRED_MESSAGE = "ACS file is required"
RELOGIN_RECOMMENDED_NOTICE = "密码已修改，建议重新登录"


def _setup_logging(verbose: bool) -> None:
    setup_cli_logging(verbose)


def _load_acs_file(file_path: str | None) -> dict[str, Any] | None:
    if file_path is None:
        return None
    path = Path(file_path)
    if not path.exists():
        raise click.ClickException(f"ACS file not found: {file_path}")
    with open(path, encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid ACS JSON file: {exc}") from exc
    if not isinstance(data, dict):
        raise click.ClickException("ACS JSON must be an object")
    return data


def _write_json_preserve_order(file_path: Path, payload: dict[str, Any]) -> None:
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=False)
        file.write("\n")


def _write_secret_json(file_path: Path, payload: dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_preserve_order(file_path, payload)
    file_path.chmod(0o600)


def _load_entity_payload_file(file_path: str | None) -> dict[str, Any] | None:
    if file_path is None:
        return None
    path = Path(file_path)
    if not path.exists():
        raise click.ClickException(f"Entity payload file not found: {file_path}")
    with open(path, encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid entity payload JSON file: {exc}") from exc
    if not isinstance(data, dict):
        raise click.ClickException("Entity payload JSON must be an object")
    unknown_fields = sorted(set(data) - set(ENTITY_REGISTRATION_PAYLOAD_FIELDS))
    if unknown_fields:
        joined = ", ".join(unknown_fields)
        raise click.ClickException(f"Entity payload JSON contains unsupported fields: {joined}")
    return data


def _get_required_acs_text(acs: dict[str, Any], field_name: str) -> str:
    value = acs.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise click.ClickException(f"ACS JSON must contain a non-empty string field: {field_name}")
    return value.strip()


def _get_optional_acs_text(acs: dict[str, Any], field_name: str) -> str | None:
    value = acs.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise click.ClickException(f"ACS JSON field must be a string when present: {field_name}")
    stripped = value.strip()
    return stripped or None


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


def _build_password_change_output(result: dict[str, Any]) -> dict[str, Any]:
    output = dict(result)
    output["notice"] = RELOGIN_RECOMMENDED_NOTICE
    return output


def _logout_with_local_cleanup(client: RegistryApiClient) -> dict[str, Any]:
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


def _extract_acs_identity(acs: dict[str, Any]) -> tuple[str | None, str, str]:
    local_aic = acs.get("aic")
    normalized_aic = None
    if isinstance(local_aic, str) and local_aic.strip():
        normalized_aic = local_aic.strip().upper()
    return (
        normalized_aic,
        _get_required_acs_text(acs, "name"),
        _get_required_acs_text(acs, "version"),
    )


def _resolve_agent_from_acs(client: RegistryApiClient, acs: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    local_aic, name, version = _extract_acs_identity(acs)
    if local_aic:
        agent = client.find_my_agent_by_aic(local_aic)
        if agent is not None:
            return agent, "aic"
    return (
        client.find_my_agent_by_name_version(name=name, version=version),
        "name_version",
    )


def _flatten_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": agent.get("id"),
        "name": agent.get("name"),
        "version": agent.get("version"),
        "approval_status": agent.get("approval_status"),
        "aic": agent.get("aic"),
        "is_deleted": agent.get("is_deleted"),
        "is_disabled": agent.get("is_disabled"),
    }


def _derive_agent_status(agent: dict[str, Any]) -> str:
    approval_status = str(agent.get("approval_status") or "").upper()
    if approval_status == "APPROVED" and agent.get("aic"):
        return "approved"
    if approval_status == "PENDING":
        return "pending"
    if approval_status == "DRAFT":
        return "draft"
    return approval_status.lower() if approval_status else "unknown"


def _parse_embedded_agent_acs(raw_acs: Any) -> dict[str, Any] | None:
    if isinstance(raw_acs, dict):
        return raw_acs
    if isinstance(raw_acs, str):
        parsed = json.loads(raw_acs)
        return parsed if isinstance(parsed, dict) else None
    return None


def _apply_embedded_agent_metadata(acs_payload: dict[str, Any], embedded_acs: dict[str, Any]) -> None:
    if embedded_acs.get("active") is not None:
        acs_payload["active"] = embedded_acs["active"]
    if embedded_acs.get("lastModifiedTime"):
        acs_payload["lastModifiedTime"] = embedded_acs["lastModifiedTime"]
    if embedded_acs.get("aic"):
        acs_payload["aic"] = embedded_acs["aic"]


def _sync_acs_payload_from_agent(acs_payload: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    updated_payload = copy.deepcopy(acs_payload)
    if agent.get("aic"):
        updated_payload["aic"] = agent["aic"]

    embedded_acs = _parse_embedded_agent_acs(agent.get("acs"))
    if embedded_acs:
        _apply_embedded_agent_metadata(updated_payload, embedded_acs)
    return updated_payload


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
    """Registry 用户侧命令入口。"""
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
            credential_env_prefix="REGISTRY_USER",
            default_token_name="registry-user.json",  # noqa: S106
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
@click.option("--name", default=None, help="Display name for auto registration")
@click.option("--org-name", default=None, help="Organization name for auto registration")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def login(
    ctx: click.Context,
    username: str | None,
    password: str | None,
    name: str | None,
    org_name: str | None,
    as_json: bool,
) -> None:
    """登录；账号不存在时自动注册并写入本地 token。"""
    client: RegistryApiClient = ctx.obj["client"]
    config: Config = ctx.obj["config"]
    resolved_username, resolved_password = _resolve_credentials(config, username, password)
    resolved_name = name or config.display_name
    resolved_org_name = org_name or config.org_name
    try:
        result = client.login_or_register_user(
            username=resolved_username,
            password=resolved_password,
            name=resolved_name,
            org_name=resolved_org_name,
        )
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    token = result.get("token", {}) if isinstance(result, dict) else {}
    status = str(result.get("status", "logged-in"))
    output = {
        "message": ("Account registered and logged in" if status == "registered" else "Login successful"),
        "status": status,
        "username": resolved_username,
        "name": resolved_name,
        "org_name": resolved_org_name,
        "token_type": token.get("token_type", "bearer"),
        "has_refresh_token": bool(token.get("refresh_token")),
    }
    print_result(output, as_json=as_json)


@main.command("whoami")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def whoami(ctx: click.Context, as_json: bool) -> None:
    """显示当前登录用户资料。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.whoami()
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(result, as_json=as_json)


@main.command("logout")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def logout(ctx: click.Context, as_json: bool) -> None:
    """注销当前登录用户，并清理本地 token。"""
    client: RegistryApiClient = ctx.obj["client"]
    print_result(_logout_with_local_cleanup(client), as_json=as_json)


@main.command("change-password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def change_password(ctx: click.Context, as_json: bool) -> None:
    """交互式修改当前登录用户密码。"""
    client: RegistryApiClient = ctx.obj["client"]
    current_password, new_password = _prompt_password_change()
    try:
        result = client.update_current_user_password(current_password, new_password)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(_build_password_change_output(result), as_json=as_json)


@main.command("fetch-eab")
@click.option("--aic", required=True, help="Agent AIC")
@click.option("--output", "output_path", required=True, help="Output JSON file path")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def get_eab(
    ctx: click.Context,
    aic: str,
    output_path: str,
    as_json: bool,
) -> None:
    """获取 EAB 凭证并保存到本地文件。"""
    client: RegistryApiClient = ctx.obj["client"]
    output_file = Path(output_path)

    try:
        credential = client.get_eab_credential(aic)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    if not isinstance(credential.get("keyId"), str) or not isinstance(credential.get("macKey"), str):
        raise click.ClickException("Registry server returned an invalid EAB payload")

    _write_secret_json(output_file, credential)
    print_result(
        {
            "message": "EAB credential saved",
            "aic": credential.get("aic"),
            "key_id": credential.get("keyId"),
            "expires_at": credential.get("expiresAt"),
            "output": str(output_file),
        },
        as_json=as_json,
    )


@main.command("list")
@click.option("--page", "page_num", default=1, type=int, show_default=True)
@click.option("--page-size", default=20, type=int, show_default=True)
@click.option("--status", "statuses", multiple=True, help="Filter statuses, can repeat")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def list_agents(
    ctx: click.Context,
    page_num: int,
    page_size: int,
    statuses: tuple[str, ...],
    as_json: bool,
) -> None:
    """查询当前用户的 Agent 列表。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.list_my_agents(page_num=page_num, page_size=page_size, statuses=list(statuses))
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(result, as_json=as_json)


@main.command("upsert")
@click.option("--logo-url", default=None, help="Agent logo URL")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--ontology/--no-ontology", "is_ontology", default=False, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def upsert_agent(
    ctx: click.Context,
    logo_url: str | None,
    acs_file: str,
    is_ontology: bool,
    as_json: bool,
) -> None:
    """按 ACS 中的 name+version 创建或更新 Agent。"""
    client: RegistryApiClient = ctx.obj["client"]
    acs = _load_acs_file(acs_file)
    if acs is None:
        raise click.ClickException(ACS_FILE_REQUIRED_MESSAGE)
    name = _get_required_acs_text(acs, "name")
    version = _get_required_acs_text(acs, "version")
    description = _get_optional_acs_text(acs, "description")
    payload: dict[str, Any] = {
        "name": name,
        "version": version,
        "acs": acs,
        "is_ontology": is_ontology,
    }
    if description is not None:
        payload["description"] = description
    if logo_url is not None:
        payload["logo_url"] = logo_url

    try:
        existing = client.find_my_agent_by_name_version(name=name, version=version)
        if existing and existing.get("id"):
            result = client.update_agent(agent_id=str(existing["id"]), payload=payload)
            output = {
                "action": "updated",
                "agent_type": "ontology" if is_ontology else "standard",
                **_flatten_agent(result),
                "agent": result,
            }
        else:
            result = client.create_agent(payload=payload)
            output = {
                "action": "created",
                "agent_type": "ontology" if is_ontology else "standard",
                **_flatten_agent(result),
                "agent": result,
            }
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    print_result(output, as_json=as_json)


@main.command("submit")
@click.option("--agent-id", required=True, help="Draft Agent UUID for manual approval")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def submit_agent_for_approval(
    ctx: click.Context,
    agent_id: str,
    as_json: bool,
) -> None:
    """提交草稿 Agent 进入人工审核流程。"""
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.submit_agent(agent_id=agent_id)
        output = {
            "message": "Submitted",
            "mode": "manual-approval",
            **_flatten_agent(result),
            "agent": result,
        }
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(output, as_json=as_json)


@main.command("register-entity")
@click.option(
    "--ontology-aic",
    required=True,
    help="Approved ontology AIC for derived entity registration",
)
@click.option(
    "--payload-file",
    default=None,
    help="Optional UTF-8 JSON object. Allowed keys: endPoints, entityUserId, entityMeta, certificate",
)
@click.option(
    "--mtls-cert-file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Override ontology mTLS certificate path",
)
@click.option(
    "--mtls-key-file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Override ontology mTLS private key path",
)
@click.option(
    "--mtls-server-ca-file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Override CA file used to verify the registry 9002 server certificate",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def register_entity(
    ctx: click.Context,
    ontology_aic: str,
    payload_file: str | None,
    mtls_cert_file: str | None,
    mtls_key_file: str | None,
    mtls_server_ca_file: str | None,
    as_json: bool,
) -> None:
    """基于已审批本体 AIC 注册派生实体。"""
    client: RegistryApiClient = ctx.obj["client"]
    payload = _load_entity_payload_file(payload_file)
    try:
        result = client.register_entity_via_atr(
            ontology_aic=ontology_aic,
            entity_payload=payload,
            mtls_cert_file=mtls_cert_file,
            mtls_key_file=mtls_key_file,
            mtls_server_ca_file=mtls_server_ca_file,
        )
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    print_result(
        {
            "message": "Entity registered",
            "mode": "derived-entity",
            "approval_status": "APPROVED",
            "aic": result.get("entityAic"),
            "entity": result,
        },
        as_json=as_json,
    )


@main.command("check")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def check_agent(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    """按 ACS 定位当前用户 Agent，并输出状态摘要。"""
    client: RegistryApiClient = ctx.obj["client"]
    acs = _load_acs_file(acs_file)
    if acs is None:
        raise click.ClickException(ACS_FILE_REQUIRED_MESSAGE)
    local_aic, name, version = _extract_acs_identity(acs)
    try:
        agent, source = _resolve_agent_from_acs(client, acs)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    if agent is None:
        print_result(
            {
                "status": "missing",
                "source": source,
                "name": name,
                "version": version,
                "aic": local_aic,
            },
            as_json=as_json,
        )
        return

    output = {
        "status": _derive_agent_status(agent),
        "source": source,
        **_flatten_agent(agent),
    }
    print_result(output, as_json=as_json)


@main.command("delete")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def delete_agent(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    """按 ACS 定位并删除当前用户 Agent。"""
    client: RegistryApiClient = ctx.obj["client"]
    acs = _load_acs_file(acs_file)
    if acs is None:
        raise click.ClickException(ACS_FILE_REQUIRED_MESSAGE)
    local_aic, name, version = _extract_acs_identity(acs)
    try:
        agent, source = _resolve_agent_from_acs(client, acs)
        if agent is None:
            print_result(
                {
                    "status": "missing",
                    "source": source,
                    "name": name,
                    "version": version,
                    "aic": local_aic,
                },
                as_json=as_json,
            )
            return
        agent_id = str(agent.get("id") or "")
        if not agent_id:
            raise click.ClickException("Located agent is missing id")
        client.delete_agent(agent_id)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    output = {
        "status": "deleted",
        "source": source,
        **_flatten_agent(agent),
    }
    print_result(output, as_json=as_json)


@main.command("sync")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def sync_acs(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    """根据 registry 当前记录回写本地 ACS metadata。"""
    client: RegistryApiClient = ctx.obj["client"]
    acs_path = Path(acs_file)
    acs = _load_acs_file(acs_file)
    if acs is None:
        raise click.ClickException(ACS_FILE_REQUIRED_MESSAGE)
    local_aic, name, version = _extract_acs_identity(acs)
    try:
        agent, source = _resolve_agent_from_acs(client, acs)
        if agent is None:
            print_result(
                {
                    "status": "missing",
                    "source": source,
                    "name": name,
                    "version": version,
                    "aic": local_aic,
                },
                as_json=as_json,
            )
            return
        agent_id = str(agent.get("id") or "")
        detailed_agent = agent
        if agent_id:
            detailed_agent = client.get_my_agent(agent_id)
        updated_acs = _sync_acs_payload_from_agent(acs, detailed_agent)
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc

    changed = updated_acs != acs
    if changed:
        _write_json_preserve_order(acs_path, updated_acs)

    output = {
        "status": "synced" if changed else "unchanged",
        "source": source,
        **_flatten_agent(detailed_agent),
    }
    print_result(output, as_json=as_json)


if __name__ == "__main__":
    main()
