"""Unified CA command groups used by the new acps-cli entrypoint."""

from __future__ import annotations

import click

from acps_cli.registry.unified import cert_eab_group
from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import get_root_runtime
from acps_cli.shared.unified_config import build_ca_legacy_section

from . import commands
from .config import CliOverrides, Config


def _build_ca_context(ctx: click.Context, server_url: str | None) -> None:
    runtime = get_root_runtime(ctx)
    legacy_section = build_ca_legacy_section(runtime, cli_base_url=server_url)
    config = Config(
        legacy_section,
        overrides=CliOverrides(server_base_url=legacy_section["server_base_url"]),
        config_file_path=(str(runtime.resolved_config_path) if runtime.resolved_config_path else None),
    )
    ctx.obj = {"config": config}


def _group(name: str, help_text: str) -> click.Group:
    return FlexibleGroup(name=name, help=help_text)


@click.group(cls=FlexibleGroup, name="cert", help="Manage certificate lifecycle operations.")
@click.option("--server-url", default=None, help="Override CA server base URL.")
@click.pass_context
def cert_group(ctx: click.Context, server_url: str | None) -> None:
    _build_ca_context(ctx, server_url)


cert_group.add_command(cert_eab_group)
cert_group.add_command(commands.new_cert, name="issue")
cert_group.add_command(commands.renew_cert, name="renew")
cert_group.add_command(commands.revoke_cert, name="revoke")
cert_group.add_command(commands.status, name="status")

account_key_group = _group("account-key", "Manage ACME account keys.")
account_key_group.add_command(commands.key_rollover, name="rollover")
cert_group.add_command(account_key_group)

trust_bundle_group = _group("trust-bundle", "Manage trust bundle files.")
trust_bundle_group.add_command(commands.update_trust_bundle, name="update")
cert_group.add_command(trust_bundle_group)

crl_group = _group("crl", "Inspect certificate revocation lists.")
crl_group.add_command(commands.download_crl, name="download")
crl_group.add_command(commands.crl_info, name="info")
crl_group.add_command(commands.crl_detail, name="detail")
cert_group.add_command(crl_group)

ocsp_group = _group("ocsp", "Inspect OCSP status endpoints.")
ocsp_group.add_command(commands.check_ocsp, name="check")
ocsp_group.add_command(commands.ocsp_cert_status, name="cert-status")
cert_group.add_command(ocsp_group)


@click.group(cls=FlexibleGroup, name="ca", help="CA administration commands.")
@click.option("--server-url", default=None, help="Override CA server base URL.")
@click.pass_context
def admin_ca_group(ctx: click.Context, server_url: str | None) -> None:
    _build_ca_context(ctx, server_url)


admin_crl_group = _group("crl", "Manage CRL administration actions.")
admin_crl_group.add_command(commands.crl_list, name="list")
admin_crl_group.add_command(commands.refresh_crl, name="refresh")
admin_ca_group.add_command(admin_crl_group)

admin_ocsp_group = _group("ocsp", "Inspect OCSP administration details.")
admin_ocsp_group.add_command(commands.ocsp_responder_info, name="responder-info")
admin_ocsp_group.add_command(commands.ocsp_stats, name="stats")
admin_ca_group.add_command(admin_ocsp_group)
