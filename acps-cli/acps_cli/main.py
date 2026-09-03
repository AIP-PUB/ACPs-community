"""Unified acps-cli command tree skeleton."""

from __future__ import annotations

from collections.abc import Iterable

import click

from acps_cli.ca.unified import admin_ca_group, cert_group
from acps_cli.discovery.unified import admin_discovery_group, discover_group
from acps_cli.monitor.unified import monitor_group
from acps_cli.mq.unified import admin_mq_group
from acps_cli.registry.unified import (
    admin_auth_group,
    admin_registry_group,
    agent_group,
    auth_group,
    cert_eab_group,
    entity_group,
)
from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.runtime import initialize_root_runtime


def _not_implemented(command_path: str) -> None:
    raise click.ClickException(f"Command '{command_path}' is not implemented yet.")


def _build_placeholder_command(name: str, help_text: str, command_path: str) -> click.Command:
    def callback() -> None:
        _not_implemented(command_path)

    callback.__name__ = f"{name.replace('-', '_')}_command"
    return click.Command(name=name, callback=callback, help=help_text)


def _build_group(name: str, help_text: str, children: Iterable[click.Command]) -> click.Group:
    group = FlexibleGroup(name=name, help=help_text)
    for child in children:
        group.add_command(child)
    return group


def _build_auth_group(prefix: str) -> click.Group:
    return _build_group(
        "auth",
        f"{prefix} authentication commands.",
        [
            _build_placeholder_command("login", "Log in and persist a token.", f"{prefix} auth login"),
            _build_placeholder_command("whoami", "Show the current identity.", f"{prefix} auth whoami"),
        ],
    )


def _build_agent_group() -> click.Group:
    return _build_group(
        "agent",
        "Manage Agent drafts and review lifecycle.",
        [
            _build_placeholder_command("list", "List your Agents.", "agent list"),
            _build_placeholder_command(
                "save",
                "Create or update an Agent draft; submitted Agents cannot be saved again.",
                "agent save",
            ),
            _build_placeholder_command("submit", "Submit an Agent draft for review.", "agent submit"),
            _build_placeholder_command("check", "Check the current Agent review status.", "agent check"),
            _build_placeholder_command("sync", "Sync the latest ACS state to local files.", "agent sync"),
            _build_placeholder_command("delete", "Delete an Agent draft that you own.", "agent delete"),
        ],
    )


def _build_entity_group() -> click.Group:
    return _build_group(
        "entity",
        "Manage derived entities.",
        [
            _build_placeholder_command(
                "derive",
                "Derive and register an entity from an existing subject AIC.",
                "entity derive",
            )
        ],
    )


def _build_cert_group() -> click.Group:
    return _build_group(
        "cert",
        "Manage certificate lifecycle operations.",
        [
            cert_eab_group,
            _build_placeholder_command("issue", "Issue a certificate.", "cert issue"),
            _build_placeholder_command("renew", "Renew an existing certificate.", "cert renew"),
            _build_placeholder_command("revoke", "Revoke a certificate.", "cert revoke"),
            _build_placeholder_command("status", "Show certificate enrollment status.", "cert status"),
            _build_group(
                "account-key",
                "Manage ACME account keys.",
                [
                    _build_placeholder_command(
                        "rollover",
                        "Roll over the ACME account key.",
                        "cert account-key rollover",
                    )
                ],
            ),
            _build_group(
                "trust-bundle",
                "Manage trust bundle files.",
                [
                    _build_placeholder_command(
                        "update",
                        "Update the local trust bundle.",
                        "cert trust-bundle update",
                    )
                ],
            ),
            _build_group(
                "crl",
                "Inspect certificate revocation lists.",
                [
                    _build_placeholder_command("download", "Download a CRL file.", "cert crl download"),
                    _build_placeholder_command("info", "Show CRL summary information.", "cert crl info"),
                    _build_placeholder_command("detail", "Show detailed CRL contents.", "cert crl detail"),
                ],
            ),
            _build_group(
                "ocsp",
                "Inspect OCSP status endpoints.",
                [
                    _build_placeholder_command("check", "Check OCSP service availability.", "cert ocsp check"),
                    _build_placeholder_command(
                        "cert-status",
                        "Check certificate status through OCSP.",
                        "cert ocsp cert-status",
                    ),
                ],
            ),
        ],
    )


def _build_discover_group() -> click.Group:
    return _build_group(
        "discover",
        "Run discovery queries and health checks.",
        [
            _build_placeholder_command("status", "Show discovery service status.", "discover status"),
            _build_placeholder_command("query", "Query discovery results.", "discover query"),
        ],
    )


def _build_admin_registry_group() -> click.Group:
    return _build_group(
        "registry",
        "Registry administration commands.",
        [
            _build_group(
                "review",
                "Review submitted Agents.",
                [
                    _build_placeholder_command(
                        "list",
                        "List Agents waiting for review.",
                        "admin registry review list",
                    ),
                    _build_placeholder_command(
                        "approve",
                        "Approve a submitted Agent.",
                        "admin registry review approve",
                    ),
                    _build_placeholder_command(
                        "reject",
                        "Reject a submitted Agent.",
                        "admin registry review reject",
                    ),
                ],
            ),
            _build_group(
                "agent",
                "Apply administrative Agent state changes.",
                [
                    _build_placeholder_command(
                        "disable",
                        "Disable an approved Agent.",
                        "admin registry agent disable",
                    ),
                    _build_placeholder_command(
                        "enable",
                        "Re-enable a disabled Agent.",
                        "admin registry agent enable",
                    ),
                ],
            ),
        ],
    )


def _build_admin_ca_group() -> click.Group:
    return _build_group(
        "ca",
        "CA administration commands.",
        [
            _build_group(
                "crl",
                "Manage CRL administration actions.",
                [
                    _build_placeholder_command("list", "List CRL records.", "admin ca crl list"),
                    _build_placeholder_command("refresh", "Refresh CRL output.", "admin ca crl refresh"),
                ],
            ),
            _build_group(
                "ocsp",
                "Inspect OCSP administration details.",
                [
                    _build_placeholder_command(
                        "responder-info",
                        "Show OCSP responder metadata.",
                        "admin ca ocsp responder-info",
                    ),
                    _build_placeholder_command("stats", "Show OCSP statistics.", "admin ca ocsp stats"),
                ],
            ),
        ],
    )


def _build_admin_discovery_group() -> click.Group:
    return _build_group(
        "discovery",
        "Discovery administration and DSP control commands.",
        [
            _build_placeholder_command(
                "run-sync",
                "Run one discovery sync orchestration cycle.",
                "admin discovery run-sync",
            ),
            _build_group(
                "dsp",
                "Manage detailed DSP control-plane actions.",
                [
                    _build_placeholder_command("status", "Show DSP status.", "admin discovery dsp status"),
                    _build_placeholder_command(
                        "registry-info",
                        "Show Registry integration details for DSP.",
                        "admin discovery dsp registry-info",
                    ),
                    _build_placeholder_command("sync", "Trigger a DSP sync.", "admin discovery dsp sync"),
                    _build_placeholder_command("start", "Start DSP processing.", "admin discovery dsp start"),
                    _build_placeholder_command("stop", "Stop DSP processing.", "admin discovery dsp stop"),
                    _build_placeholder_command("reset", "Reset DSP state.", "admin discovery dsp reset"),
                    _build_placeholder_command(
                        "hard-reset",
                        "Perform a destructive DSP hard reset.",
                        "admin discovery dsp hard-reset",
                    ),
                    _build_placeholder_command(
                        "register-webhook",
                        "Register the discovery webhook with the Registry.",
                        "admin discovery dsp register-webhook",
                    ),
                ],
            ),
        ],
    )


def _build_admin_group() -> click.Group:
    return _build_group(
        "admin",
        "Administrative and control-plane commands.",
        [
            admin_auth_group,
            admin_registry_group,
            admin_ca_group,
            admin_discovery_group,
            admin_mq_group,
        ],
    )


@click.group(cls=FlexibleGroup)
@click.option("--config", "config_path", default=None, help="Path to acps-cli.toml config file.")
@click.option("--verbose", is_flag=True, help="Enable verbose logging.")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """ACPs unified command line interface."""
    initialize_root_runtime(ctx, config_path, verbose)


main.add_command(auth_group)
main.add_command(agent_group)
main.add_command(entity_group)
main.add_command(cert_group)
main.add_command(discover_group)
main.add_command(monitor_group)
main.add_command(_build_admin_group())


if __name__ == "__main__":
    main()
