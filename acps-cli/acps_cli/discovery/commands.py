"""Discovery CLI commands."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from acps_cli.discovery.client import (
    DiscoveryError,
    get_dsp_status,
    get_health_status,
    get_registry_info,
    query,
    register_webhook,
    run_dsp_action,
    trigger_sync,
)
from acps_cli.registry.storage import TokenStore
from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager
from acps_cli.shared.cli_logging import setup_cli_logging
from acps_cli.shared.config import load_toml_config
from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import RootCliRuntime
from acps_cli.shared.unified_config import build_registry_auth_config

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryPayloadOptions:
    """Structured options used to build a DiscoveryRequest payload."""

    base_payload: dict[str, Any] | None
    query_str: str | None
    query_type: str | None
    limit: int | None
    filter_payload: dict[str, Any] | None
    context_payload: dict[str, Any] | None
    forward_depth_limit: int | None
    forward_fanout_limit: int | None
    forward_fanout_remaining: int | None
    forward_chain: tuple[str, ...]
    forward_trusted_servers: tuple[str, ...]
    forward_signatures: tuple[str, ...]
    forward_each_timeout_ms: int | None
    forward_total_timeout_ms: int | None


def _resolve_server_url(ctx_obj: dict[str, Any]) -> str:
    """从上下文中解析 discovery server URL，未配置时报错。"""
    url = ctx_obj.get("server_base_url")
    if not url:
        raise click.ClickException(
            "Discovery server URL is required. Provide --server-url or configure [discovery].base_url in acps-cli.toml"
        )
    return url.rstrip("/")


def _resolve_registry_admin_auth_headers(ctx_obj: dict[str, Any]) -> dict[str, str]:
    """从本地管理员 token 文件解析 Registry 管理认证头。"""
    toml_data = ctx_obj.get("toml_data") or {}
    config_dir = ctx_obj.get("config_dir")
    runtime = RootCliRuntime(
        config_path=None,
        verbose=False,
        toml_data=toml_data,
        resolved_config_path=None,
        config_dir=config_dir,
    )
    auth_config = build_registry_auth_config(runtime, admin=True)
    if auth_config.mode == "oidc":
        try:
            access_token = OidcAuthSessionManager(auth_config).get_access_token()
        except AuthSessionError as exc:
            raise click.ClickException(str(exc)) from exc
        return {"Authorization": f"Bearer {access_token}"}

    token_data = TokenStore(Path(auth_config.token_file)).load()
    access_token_value = token_data.get("access_token") if token_data else None
    if not isinstance(access_token_value, str) or not access_token_value:
        raise click.ClickException(
            "Registry admin token is required for webhook registration. Run acps-cli admin auth login first"
        )
    return {"Authorization": f"Bearer {access_token_value}"}


def _echo_json(payload: dict[str, Any]) -> None:
    """Pretty-print JSON payload to stdout."""
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _load_json_object_from_text(value: str, label: str) -> dict[str, Any]:
    """Parse a JSON object from inline text."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} JSON must be an object")
    return payload


def _load_json_object_from_file(file_path: Path, label: str) -> dict[str, Any]:
    """Parse a JSON object from a file."""
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid {label} JSON file: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} JSON file must contain an object")
    return payload


def _load_json_source(
    *,
    json_text: str | None,
    file_path: Path | None,
    text_option: str,
    file_option: str,
    label: str,
) -> dict[str, Any] | None:
    """Load a JSON object from either inline text or file."""
    if json_text is not None and file_path is not None:
        raise click.ClickException(f"{text_option} and {file_option} cannot be used together")
    if json_text is not None:
        return _load_json_object_from_text(json_text, label)
    if file_path is not None:
        return _load_json_object_from_file(file_path, label)
    return None


def _set_payload_field(payload: dict[str, Any], key: str, value: Any) -> None:
    """Set a payload field only when a concrete value is provided."""
    if value is not None:
        payload[key] = value


def _build_query_payload(options: QueryPayloadOptions) -> dict[str, Any]:
    """Build DiscoveryRequest payload from CLI options."""
    payload = dict(options.base_payload or {})

    if options.query_type is not None:
        payload["type"] = options.query_type
    elif "type" not in payload:
        payload["type"] = "explicit"

    if options.query_str is not None:
        payload["query"] = options.query_str

    if options.limit is not None:
        payload["limit"] = options.limit
    elif "limit" not in payload:
        payload["limit"] = 5

    _set_payload_field(payload, "filter", options.filter_payload)
    _set_payload_field(payload, "context", options.context_payload)
    _set_payload_field(payload, "forwardDepthLimit", options.forward_depth_limit)
    _set_payload_field(payload, "forwardFanoutLimit", options.forward_fanout_limit)
    _set_payload_field(payload, "forwardFanoutRemaining", options.forward_fanout_remaining)
    _set_payload_field(payload, "forwardChain", list(options.forward_chain) or None)
    _set_payload_field(payload, "forwardTrustedServers", list(options.forward_trusted_servers) or None)
    _set_payload_field(payload, "forwardSignatures", list(options.forward_signatures) or None)
    _set_payload_field(payload, "forwardEachTimeoutMs", options.forward_each_timeout_ms)
    _set_payload_field(payload, "forwardTotalTimeoutMs", options.forward_total_timeout_ms)

    return payload


@click.group(cls=FlexibleGroup)
@click.option("--config", "config_path", default=None, help="Path to acps-cli.toml config file.")
@click.option("--server-url", default=None, help="Override discovery server base URL")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx, config_path, server_url, verbose):
    """ACPs Discovery service CLI."""
    setup_cli_logging(verbose)
    ctx.ensure_object(dict)
    toml_data, resolved_path = load_toml_config(config_path)
    ctx.obj["server_base_url"] = server_url or toml_data.get("discovery", {}).get("server_base_url")
    ctx.obj["toml_data"] = toml_data
    ctx.obj["config_dir"] = resolved_path.parent if resolved_path else None
    LOGGER.debug("Discovery CLI initialized")


@main.command()
@click.option(
    "--hard-reset/--no-hard-reset",
    default=True,
    show_default=True,
    help="Clear discovery data before running sync.",
)
@click.option(
    "--expect-acs-min",
    type=click.IntRange(min=0),
    default=1,
    show_default=True,
    help="Require at least N ACS objects after sync.",
)
@click.option("--skip-acs-check", is_flag=True, help="Do not assert ACS object count after sync.")
@click.pass_context
def sync(ctx, hard_reset, expect_acs_min, skip_acs_check):
    """Trigger DSP data sync."""
    url = _resolve_server_url(ctx.obj)
    LOGGER.debug("Triggering discovery sync via %s", url)
    try:
        min_acs_count = None if skip_acs_check else expect_acs_min
        trigger_sync(url, hard_reset=hard_reset, min_acs_count=min_acs_count)
        click.echo("Sync triggered successfully.")
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@main.command(name="query")
@click.argument("query_str", required=False)
@click.option("--type", "query_type", default=None, help="Discovery request type.")
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=50),
    default=None,
    help="Maximum number of results.",
)
@click.option(
    "--request-json",
    default=None,
    help="Inline DiscoveryRequest JSON object. Cannot be combined with --request-file.",
)
@click.option(
    "--request-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Path to DiscoveryRequest JSON file.",
)
@click.option(
    "--filter-json",
    default=None,
    help="Inline DiscoveryFilter JSON object. Cannot be combined with --filter-file.",
)
@click.option(
    "--filter-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Path to DiscoveryFilter JSON file.",
)
@click.option(
    "--context-json",
    default=None,
    help="Inline context JSON object. Cannot be combined with --context-file.",
)
@click.option(
    "--context-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Path to discovery context JSON file.",
)
@click.option("--forward-depth-limit", type=click.IntRange(min=1, max=5), default=None)
@click.option("--forward-fanout-limit", type=click.IntRange(min=1, max=5), default=None)
@click.option("--forward-fanout-remaining", type=click.IntRange(min=0, max=5), default=None)
@click.option("--forward-chain", multiple=True, help="Append values to forwardChain.")
@click.option(
    "--forward-trusted-server",
    "forward_trusted_servers",
    multiple=True,
    help="Append values to forwardTrustedServers.",
)
@click.option(
    "--forward-signature",
    "forward_signatures",
    multiple=True,
    help="Append values to forwardSignatures.",
)
@click.option("--forward-each-timeout-ms", type=click.IntRange(min=1), default=None)
@click.option("--forward-total-timeout-ms", type=click.IntRange(min=1), default=None)
@click.pass_context
def query_cmd(ctx: click.Context, /, **options: Any) -> None:
    """Query discovery service."""
    url = _resolve_server_url(ctx.obj)
    try:
        base_payload = _load_json_source(
            json_text=options.get("request_json"),
            file_path=options.get("request_file"),
            text_option="--request-json",
            file_option="--request-file",
            label="request",
        )
        filter_payload = _load_json_source(
            json_text=options.get("filter_json"),
            file_path=options.get("filter_file"),
            text_option="--filter-json",
            file_option="--filter-file",
            label="filter",
        )
        context_payload = _load_json_source(
            json_text=options.get("context_json"),
            file_path=options.get("context_file"),
            text_option="--context-json",
            file_option="--context-file",
            label="context",
        )
        payload = _build_query_payload(
            QueryPayloadOptions(
                base_payload=base_payload,
                query_str=options.get("query_str"),
                query_type=options.get("query_type"),
                limit=options.get("limit"),
                filter_payload=filter_payload,
                context_payload=context_payload,
                forward_depth_limit=options.get("forward_depth_limit"),
                forward_fanout_limit=options.get("forward_fanout_limit"),
                forward_fanout_remaining=options.get("forward_fanout_remaining"),
                forward_chain=options.get("forward_chain") or (),
                forward_trusted_servers=options.get("forward_trusted_servers") or (),
                forward_signatures=options.get("forward_signatures") or (),
                forward_each_timeout_ms=options.get("forward_each_timeout_ms"),
                forward_total_timeout_ms=options.get("forward_total_timeout_ms"),
            )
        )
        LOGGER.debug(
            "Submitting discovery query to %s with payload keys=%s",
            url,
            sorted(payload),
        )
        result = query(url, payload)
        _echo_json(result)
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@main.command()
@click.pass_context
def status(ctx):
    """Check discovery service status."""
    url = _resolve_server_url(ctx.obj)
    try:
        status_code = get_health_status(url)
        click.echo(f"Status: {status_code}")
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@main.group()
def dsp():
    """DSP admin commands for e2e orchestration."""


@dsp.command(name="status")
@click.option(
    "--expect-acs-min",
    type=click.IntRange(min=0),
    default=None,
    help="Require at least N ACS objects in the returned status.",
)
@click.pass_context
def dsp_status(ctx, expect_acs_min):
    """Get current DSP status as JSON."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(get_dsp_status(url, min_acs_count=expect_acs_min))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="registry-info")
@click.pass_context
def dsp_registry_info(ctx):
    """Get connected registry information as JSON."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(get_registry_info(url))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="sync")
@click.pass_context
def dsp_sync(ctx):
    """Run one DSP sync cycle without reset."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(run_dsp_action(url, "sync", timeout=180))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="start")
@click.pass_context
def dsp_start(ctx):
    """Start DSP background sync."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(run_dsp_action(url, "start"))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="stop")
@click.pass_context
def dsp_stop(ctx):
    """Stop DSP background sync."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(run_dsp_action(url, "stop"))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="reset")
@click.pass_context
def dsp_reset(ctx):
    """Reset DSP state without clearing synced agent data."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(run_dsp_action(url, "reset"))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="hard-reset")
@click.pass_context
def dsp_hard_reset(ctx):
    """Clear synced agent data and reset DSP state."""
    url = _resolve_server_url(ctx.obj)
    try:
        _echo_json(run_dsp_action(url, "hard-reset"))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@dsp.command(name="register-webhook")
@click.option("--url", "webhook_url", required=True, help="Webhook callback URL.")
@click.option("--secret", required=True, help="Shared webhook secret.")
@click.option("--type", "types", multiple=True, help="Subscribe to object type. Repeatable.")
@click.option("--event", "events", multiple=True, help="Subscribe to event type. Repeatable.")
@click.option("--description", default=None, help="Optional webhook description.")
@click.pass_context
def dsp_register_webhook(ctx, webhook_url, secret, types, events, description):
    """Register a webhook on discovery for DSP push notifications."""
    url = _resolve_server_url(ctx.obj)
    auth_headers = _resolve_registry_admin_auth_headers(ctx.obj)
    payload: dict[str, Any] = {
        "url": webhook_url,
        "secret": secret,
        "types": list(types) or ["acs"],
        "events": list(events) or ["data_change"],
    }
    if description is not None:
        payload["description"] = description

    try:
        _echo_json(register_webhook(url, payload, headers=auth_headers))
    except DiscoveryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None
