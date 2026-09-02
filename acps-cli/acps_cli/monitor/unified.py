"""统一 CLI 下的 monitor 命令组。"""

from __future__ import annotations

import click

from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import get_root_runtime
from acps_cli.shared.unified_config import build_monitor_auth_config, build_monitor_runtime_context

from . import commands


def _build_monitor_context(ctx: click.Context, server_url: str | None, api_prefix: str | None) -> None:
    runtime = get_root_runtime(ctx)
    context = build_monitor_runtime_context(
        runtime,
        cli_base_url=server_url,
        cli_api_prefix=api_prefix,
    )
    context["auth_config"] = build_monitor_auth_config(runtime)
    ctx.obj = context


@click.group(cls=FlexibleGroup, name="monitor", help="Query AMP monitor-server data.")
@click.option("--server-url", default=None, help="Override monitor-server base URL.")
@click.option("--api-prefix", default=None, help="Override AMP API prefix.")
@click.pass_context
def monitor_group(ctx: click.Context, server_url: str | None, api_prefix: str | None) -> None:
    _build_monitor_context(ctx, server_url, api_prefix)


monitor_group.add_command(commands.status, name="status")
monitor_group.add_command(commands.auth_group, name="auth")
monitor_group.add_command(commands.heartbeat_group, name="heartbeat")
monitor_group.add_command(commands.metrics_group, name="metrics")
monitor_group.add_command(commands.access_group, name="access")
monitor_group.add_command(commands.message_group, name="message")
monitor_group.add_command(commands.system_group, name="system")
monitor_group.add_command(commands.audit_group, name="audit")
