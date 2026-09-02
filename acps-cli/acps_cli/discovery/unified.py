"""Unified discovery command groups used by the new acps-cli entrypoint."""

from __future__ import annotations

import click

from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import get_root_runtime
from acps_cli.shared.unified_config import build_discovery_runtime_context

from . import commands


def _build_discovery_context(ctx: click.Context, server_url: str | None) -> None:
    runtime = get_root_runtime(ctx)
    ctx.obj = build_discovery_runtime_context(runtime, cli_base_url=server_url)


def _group(name: str, help_text: str) -> click.Group:
    return FlexibleGroup(name=name, help=help_text)


@click.group(cls=FlexibleGroup, name="discover", help="Run discovery queries and health checks.")
@click.option("--server-url", default=None, help="Override discovery server base URL.")
@click.pass_context
def discover_group(ctx: click.Context, server_url: str | None) -> None:
    _build_discovery_context(ctx, server_url)


discover_group.add_command(commands.status, name="status")
discover_group.add_command(commands.query_cmd, name="query")


@click.group(cls=FlexibleGroup, name="discovery", help="Discovery administration and DSP control commands.")
@click.option("--server-url", default=None, help="Override discovery server base URL.")
@click.pass_context
def admin_discovery_group(ctx: click.Context, server_url: str | None) -> None:
    _build_discovery_context(ctx, server_url)


admin_discovery_group.add_command(commands.sync, name="run-sync")

dsp_group = _group("dsp", "Manage detailed DSP control-plane actions.")
dsp_group.add_command(commands.dsp_status, name="status")
dsp_group.add_command(commands.dsp_registry_info, name="registry-info")
dsp_group.add_command(commands.dsp_sync, name="sync")
dsp_group.add_command(commands.dsp_start, name="start")
dsp_group.add_command(commands.dsp_stop, name="stop")
dsp_group.add_command(commands.dsp_reset, name="reset")
dsp_group.add_command(commands.dsp_hard_reset, name="hard-reset")
dsp_group.add_command(commands.dsp_register_webhook, name="register-webhook")
admin_discovery_group.add_command(dsp_group)
