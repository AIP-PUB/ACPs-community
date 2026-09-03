"""Unified registry command groups used by the new acps-cli entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import click

from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager
from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import get_root_runtime
from acps_cli.shared.unified_config import ServiceAuthConfig, build_registry_auth_config, build_registry_legacy_section

from . import admin_commands, commands
from .client import RegistryApiClient
from .config import CliOverrides, Config, ConfigError
from .exceptions import RegistryClientError
from .output import print_result

DEFAULT_USER_CREDENTIAL_FILE_NAME = "registry-user.json"
DEFAULT_ADMIN_CREDENTIAL_FILE_NAME = "registry-admin.json"


def _invoke_legacy_callback(command: click.Command, **kwargs: Any) -> None:
    callback = cast("Callable[..., None] | None", command.callback)
    if callback is None:
        raise click.ClickException(f"Command callback is missing for '{command.name}'.")
    callback(**kwargs)


def _build_registry_context(
    ctx: click.Context,
    *,
    server_url: str | None,
    mtls_url: str | None,
    credential_env_prefix: str,
    default_token_name: str,
    require_mtls: bool = False,
) -> None:
    runtime = get_root_runtime(ctx)
    admin = credential_env_prefix == "REGISTRY_ADMIN"
    auth_config = build_registry_auth_config(runtime, admin=admin)
    legacy_section = build_registry_legacy_section(
        runtime,
        cli_base_url=server_url,
        cli_mtls_url=mtls_url,
        admin=admin,
        require_mtls=require_mtls,
    )
    try:
        config = Config(
            toml_section=legacy_section,
            overrides=CliOverrides(
                server_base_url=legacy_section["server_base_url"],
                mtls_base_url=legacy_section.get("mtls_base_url"),
                token_file=legacy_section["token_file"],
            ),
            credential_env_prefix=credential_env_prefix,
            default_token_name=default_token_name,
            config_file_dir=runtime.config_dir,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    oidc_session = OidcAuthSessionManager(auth_config) if auth_config.mode == "oidc" else None
    try:
        client = RegistryApiClient(config, auth_mode=auth_config.mode, oidc_session=oidc_session)
    except TypeError:
        client = RegistryApiClient(config)
    ctx.obj = {
        "config": config,
        "client": client,
        "auth_config": auth_config,
        "auth_session": oidc_session,
    }


def _auth_config(ctx: click.Context) -> ServiceAuthConfig:
    auth_config = (ctx.obj or {}).get("auth_config")
    if not isinstance(auth_config, ServiceAuthConfig):
        raise click.ClickException("Registry auth configuration is not initialized.")
    return auth_config


def _oidc_auth_session(ctx: click.Context) -> OidcAuthSessionManager:
    auth_session = (ctx.obj or {}).get("auth_session")
    if auth_session is None:
        raise click.ClickException("OIDC auth session is not initialized.")
    return cast("OidcAuthSessionManager", auth_session)


def _print_device_prompt(prompt: Any, *, as_json: bool) -> None:
    click.echo(
        f"Open this URL in a browser: {prompt.verification_uri_complete or prompt.verification_uri}",
        err=as_json,
    )
    click.echo(f"Enter this code if prompted: {prompt.user_code}", err=as_json)


def _registry_local_status_payload(ctx: click.Context) -> dict[str, Any]:
    client: RegistryApiClient = ctx.obj["client"]
    token_data = client.token_store.load()
    return {
        "service": "registry",
        "account_kind": _auth_config(ctx).account_kind,
        "auth_mode": "local",
        "authenticated": bool(token_data and token_data.get("access_token")),
        "has_refresh_token": bool(token_data and token_data.get("refresh_token")),
        "token_type": token_data.get("token_type") if token_data else None,
    }


def _run_oidc_login(ctx: click.Context, *, success_message: str, as_json: bool) -> None:
    auth_session = _oidc_auth_session(ctx)
    try:
        auth_session.login(on_prompt=lambda prompt: _print_device_prompt(prompt, as_json=as_json))
        payload = {"message": success_message, **auth_session.whoami(), "authenticated": True}
    except AuthSessionError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(payload, as_json=as_json)


def _run_oidc_refresh(ctx: click.Context, *, success_message: str, as_json: bool) -> None:
    auth_session = _oidc_auth_session(ctx)
    try:
        auth_session.refresh()
        payload = {"message": success_message, **auth_session.whoami(), "authenticated": True}
    except AuthSessionError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(payload, as_json=as_json)


def _run_oidc_logout(ctx: click.Context, *, as_json: bool) -> None:
    auth_session = _oidc_auth_session(ctx)
    try:
        payload = auth_session.logout()
    except AuthSessionError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(payload, as_json=as_json)


def _reject_oidc_login_options(
    *, username: str | None, password: str | None, name: str | None = None, org_name: str | None = None
) -> None:
    invalid_options = []
    if username is not None:
        invalid_options.append("--username")
    if password is not None:
        invalid_options.append("--password")
    if name is not None:
        invalid_options.append("--name")
    if org_name is not None:
        invalid_options.append("--org-name")
    if invalid_options:
        joined = ", ".join(invalid_options)
        raise click.ClickException(f"{joined} is not supported when registry.auth.mode=oidc")


@click.group(cls=FlexibleGroup, name="auth", help="User authentication commands.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.pass_context
def auth_group(ctx: click.Context, server_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=None,
        credential_env_prefix="REGISTRY_USER",
        default_token_name=DEFAULT_USER_CREDENTIAL_FILE_NAME,
    )


@auth_group.command("login")
@click.option("--username", default=None, help="Username")
@click.option("--password", default=None, help="Password")
@click.option("--name", default=None, help="Display name for auto registration")
@click.option("--org-name", default=None, help="Organization name for auto registration")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_login(
    ctx: click.Context,
    username: str | None,
    password: str | None,
    name: str | None,
    org_name: str | None,
    as_json: bool,
) -> None:
    if _auth_config(ctx).mode == "oidc":
        _reject_oidc_login_options(username=username, password=password, name=name, org_name=org_name)
        _run_oidc_login(ctx, success_message="Registry login successful", as_json=as_json)
        return
    _invoke_legacy_callback(
        commands.login,
        username=username,
        password=password,
        name=name,
        org_name=org_name,
        as_json=as_json,
    )


@auth_group.command("whoami")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_whoami(ctx: click.Context, as_json: bool) -> None:
    _invoke_legacy_callback(commands.whoami, as_json=as_json)


@auth_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_status(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        try:
            payload = _oidc_auth_session(ctx).status()
        except AuthSessionError as exc:
            raise click.ClickException(str(exc)) from exc
        print_result(payload, as_json=as_json)
        return
    print_result(_registry_local_status_payload(ctx), as_json=as_json)


@auth_group.command("refresh")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_refresh(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        _run_oidc_refresh(ctx, success_message="Registry session refreshed", as_json=as_json)
        return
    client: RegistryApiClient = ctx.obj["client"]
    try:
        refreshed = client.refresh_local_token()
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "message": "Registry session refreshed",
        "auth_mode": "local",
        "has_refresh_token": bool(refreshed.get("refresh_token")),
        "token_type": refreshed.get("token_type", "bearer"),
    }
    print_result(payload, as_json=as_json)


@auth_group.command("logout")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_logout(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        _run_oidc_logout(ctx, as_json=as_json)
        return
    _invoke_legacy_callback(commands.logout, as_json=as_json)


@auth_group.command("change-password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_change_password(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        raise click.ClickException(
            "Password changes are managed by the OIDC identity provider in registry.auth.mode=oidc"
        )
    _invoke_legacy_callback(commands.change_password, as_json=as_json)


@click.group(cls=FlexibleGroup, name="agent", help="Manage Agent drafts and review lifecycle.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.pass_context
def agent_group(ctx: click.Context, server_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=None,
        credential_env_prefix="REGISTRY_USER",
        default_token_name=DEFAULT_USER_CREDENTIAL_FILE_NAME,
    )


@agent_group.command("list")
@click.option("--page", "page_num", default=1, type=int, show_default=True)
@click.option("--page-size", default=20, type=int, show_default=True)
@click.option("--status", "statuses", multiple=True, help="Filter statuses, can repeat")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_list(
    ctx: click.Context,
    page_num: int,
    page_size: int,
    statuses: tuple[str, ...],
    as_json: bool,
) -> None:
    _invoke_legacy_callback(
        commands.list_agents,
        page_num=page_num,
        page_size=page_size,
        statuses=statuses,
        as_json=as_json,
    )


@agent_group.command("save")
@click.option("--logo-url", default=None, help="Agent logo URL")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--ontology/--no-ontology", "is_ontology", default=False, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_save(
    ctx: click.Context,
    logo_url: str | None,
    acs_file: str,
    is_ontology: bool,
    as_json: bool,
) -> None:
    _invoke_legacy_callback(
        commands.upsert_agent,
        logo_url=logo_url,
        acs_file=acs_file,
        is_ontology=is_ontology,
        as_json=as_json,
    )


@agent_group.command("submit")
@click.option("--agent-id", required=True, help="Draft Agent UUID for manual approval")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_submit(ctx: click.Context, agent_id: str, as_json: bool) -> None:
    _invoke_legacy_callback(commands.submit_agent_for_approval, agent_id=agent_id, as_json=as_json)


@agent_group.command("check")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_check(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    _invoke_legacy_callback(commands.check_agent, acs_file=acs_file, as_json=as_json)


@agent_group.command("sync")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_sync(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    _invoke_legacy_callback(commands.sync_acs, acs_file=acs_file, as_json=as_json)


@agent_group.command("delete")
@click.option("--acs-file", required=True, help="Path to ACS JSON file")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def agent_delete(ctx: click.Context, acs_file: str, as_json: bool) -> None:
    _invoke_legacy_callback(commands.delete_agent, acs_file=acs_file, as_json=as_json)


@click.group(cls=FlexibleGroup, name="entity", help="Manage derived entities.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.option("--mtls-url", default=None, help="Override registry mTLS base URL.")
@click.pass_context
def entity_group(ctx: click.Context, server_url: str | None, mtls_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=mtls_url,
        credential_env_prefix="REGISTRY_USER",
        default_token_name=DEFAULT_USER_CREDENTIAL_FILE_NAME,
        require_mtls=True,
    )


@entity_group.command("derive")
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
def entity_derive(
    ctx: click.Context,
    ontology_aic: str,
    payload_file: str | None,
    mtls_cert_file: str | None,
    mtls_key_file: str | None,
    mtls_server_ca_file: str | None,
    as_json: bool,
) -> None:
    """Derive and register an entity from an approved ontology AIC."""
    _invoke_legacy_callback(
        commands.register_entity,
        ontology_aic=ontology_aic,
        payload_file=payload_file,
        mtls_cert_file=mtls_cert_file,
        mtls_key_file=mtls_key_file,
        mtls_server_ca_file=mtls_server_ca_file,
        as_json=as_json,
    )


@click.group(cls=FlexibleGroup, name="eab", help="Manage external account binding credentials.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.pass_context
def cert_eab_group(ctx: click.Context, server_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=None,
        credential_env_prefix="REGISTRY_USER",
        default_token_name=DEFAULT_USER_CREDENTIAL_FILE_NAME,
    )


@cert_eab_group.command("fetch")
@click.option("--aic", required=True, help="Agent AIC")
@click.option("--output", "output_path", required=True, help="Output JSON file path")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def cert_eab_fetch(ctx: click.Context, aic: str, output_path: str, as_json: bool) -> None:
    _invoke_legacy_callback(commands.get_eab, aic=aic, output_path=output_path, as_json=as_json)


@click.group(cls=FlexibleGroup, name="auth", help="Registry administrator authentication commands.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.pass_context
def admin_auth_group(ctx: click.Context, server_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=None,
        credential_env_prefix="REGISTRY_ADMIN",
        default_token_name=DEFAULT_ADMIN_CREDENTIAL_FILE_NAME,
    )


@admin_auth_group.command("login")
@click.option("--username", default=None, help="Username")
@click.option("--password", default=None, help="Password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_login(ctx: click.Context, username: str | None, password: str | None, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        _reject_oidc_login_options(username=username, password=password)
        _run_oidc_login(ctx, success_message="Registry admin login successful", as_json=as_json)
        return
    _invoke_legacy_callback(admin_commands.login, username=username, password=password, as_json=as_json)


@admin_auth_group.command("whoami")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_whoami(ctx: click.Context, as_json: bool) -> None:
    client: RegistryApiClient = ctx.obj["client"]
    try:
        result = client.whoami()
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    print_result(result, as_json=as_json)


@admin_auth_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_status(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        try:
            payload = _oidc_auth_session(ctx).status()
        except AuthSessionError as exc:
            raise click.ClickException(str(exc)) from exc
        print_result(payload, as_json=as_json)
        return
    print_result(_registry_local_status_payload(ctx), as_json=as_json)


@admin_auth_group.command("refresh")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_refresh(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        _run_oidc_refresh(ctx, success_message="Registry admin session refreshed", as_json=as_json)
        return
    client: RegistryApiClient = ctx.obj["client"]
    try:
        refreshed = client.refresh_local_token()
    except RegistryClientError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "message": "Registry admin session refreshed",
        "auth_mode": "local",
        "has_refresh_token": bool(refreshed.get("refresh_token")),
        "token_type": refreshed.get("token_type", "bearer"),
    }
    print_result(payload, as_json=as_json)


@admin_auth_group.command("logout")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_logout(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        _run_oidc_logout(ctx, as_json=as_json)
        return
    _invoke_legacy_callback(admin_commands.logout, as_json=as_json)


@admin_auth_group.command("change-password")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_auth_change_password(ctx: click.Context, as_json: bool) -> None:
    if _auth_config(ctx).mode == "oidc":
        raise click.ClickException(
            "Password changes are managed by the OIDC identity provider in registry.auth.mode=oidc"
        )
    _invoke_legacy_callback(admin_commands.change_password, as_json=as_json)


@click.group(cls=FlexibleGroup, name="registry", help="Registry administration commands.")
@click.option("--server-url", default=None, help="Override registry server base URL.")
@click.pass_context
def admin_registry_group(ctx: click.Context, server_url: str | None) -> None:
    _build_registry_context(
        ctx,
        server_url=server_url,
        mtls_url=None,
        credential_env_prefix="REGISTRY_ADMIN",
        default_token_name=DEFAULT_ADMIN_CREDENTIAL_FILE_NAME,
    )


@admin_registry_group.group("review", help="Review submitted Agents.")
def admin_registry_review_group() -> None:
    return None


@admin_registry_review_group.command("list")
@click.option("--page", "page_num", default=1, type=int, show_default=True)
@click.option("--page-size", default=20, type=int, show_default=True)
@click.option("--status", "statuses", multiple=True, help="Filter statuses, can repeat")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_review_list(
    ctx: click.Context,
    page_num: int,
    page_size: int,
    statuses: tuple[str, ...],
    as_json: bool,
) -> None:
    _invoke_legacy_callback(
        admin_commands.list_reviews,
        page_num=page_num,
        page_size=page_size,
        statuses=statuses,
        as_json=as_json,
    )


@admin_registry_review_group.command("approve")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--comments", default=None, help="Optional review comments")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_review_approve(
    ctx: click.Context,
    agent_id: str,
    comments: str | None,
    as_json: bool,
) -> None:
    _invoke_legacy_callback(
        admin_commands.approve_review,
        agent_id=agent_id,
        comments=comments,
        as_json=as_json,
    )


@admin_registry_review_group.command("reject")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--comments", required=True, help="Reject reason")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_review_reject(ctx: click.Context, agent_id: str, comments: str, as_json: bool) -> None:
    _invoke_legacy_callback(
        admin_commands.reject_review,
        agent_id=agent_id,
        comments=comments,
        as_json=as_json,
    )


@admin_registry_group.group("agent", help="Apply administrative Agent state changes.")
def admin_registry_agent_group() -> None:
    return None


@admin_registry_agent_group.command("disable")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--reason", default="Staff disable", show_default=True, help="Disable reason")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_agent_disable(ctx: click.Context, agent_id: str, reason: str, as_json: bool) -> None:
    _invoke_legacy_callback(
        admin_commands.disable_agent,
        agent_id=agent_id,
        reason=reason,
        as_json=as_json,
    )


@admin_registry_agent_group.command("enable")
@click.option("--agent-id", required=True, help="Agent UUID")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_agent_enable(ctx: click.Context, agent_id: str, as_json: bool) -> None:
    _invoke_legacy_callback(admin_commands.enable_agent, agent_id=agent_id, as_json=as_json)


@admin_registry_group.group("user", help="Manage registry user accounts.")
def admin_registry_user_group() -> None:
    return None


@admin_registry_user_group.command("reset-password")
@click.option("--user-id", required=True, help="Target user UUID")
@click.option("--new-password", default=None, help="New password for the target user")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def admin_registry_user_reset_password(
    ctx: click.Context,
    user_id: str,
    new_password: str | None,
    as_json: bool,
) -> None:
    if _auth_config(ctx).mode == "oidc":
        raise click.ClickException(
            "User password reset is managed by the OIDC identity provider in registry.auth.mode=oidc"
        )
    _invoke_legacy_callback(
        admin_commands.reset_user_password,
        user_id=user_id,
        new_password=new_password,
        as_json=as_json,
    )
