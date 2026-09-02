"""组级选项位置无关解析。"""

from __future__ import annotations

import click
from click.testing import CliRunner

from acps_cli.shared.flexible_group import FlexibleGroup, reorder_group_args


def _build_sample_cli() -> click.Group:
    @click.group(cls=FlexibleGroup)
    @click.option("--config", default=None)
    @click.option("--verbose", is_flag=True)
    @click.pass_context
    def cli(ctx: click.Context, config: str | None, verbose: bool) -> None:
        ctx.ensure_object(dict)
        ctx.obj["config"] = config
        ctx.obj["verbose"] = verbose

    @cli.group()
    @click.option("--server-url", default=None)
    @click.option("--mtls-url", default=None)
    @click.pass_context
    def entity(ctx: click.Context, server_url: str | None, mtls_url: str | None) -> None:
        ctx.obj["entity_server_url"] = server_url
        ctx.obj["mtls_url"] = mtls_url

    @entity.command()
    @click.option("--ontology-aic", required=True)
    @click.option("--json", "as_json", is_flag=True)
    @click.pass_context
    def derive(ctx: click.Context, ontology_aic: str, as_json: bool) -> None:
        click.echo(
            "|".join(
                [
                    f"config={ctx.obj.get('config')}",
                    f"verbose={ctx.obj.get('verbose')}",
                    f"mtls={ctx.obj.get('mtls_url')}",
                    f"entity_server={ctx.obj.get('entity_server_url')}",
                    f"aic={ontology_aic}",
                    f"json={as_json}",
                ]
            )
        )

    @cli.group()
    @click.option("--server-url", default=None)
    @click.pass_context
    def cert(ctx: click.Context, server_url: str | None) -> None:
        ctx.obj["ca_server_url"] = server_url

    @cert.group()
    @click.option("--server-url", default=None)
    @click.pass_context
    def eab(ctx: click.Context, server_url: str | None) -> None:
        ctx.obj["registry_server_url"] = server_url

    @eab.command()
    @click.option("--aic", required=True)
    @click.pass_context
    def fetch(ctx: click.Context, aic: str) -> None:
        click.echo(
            "|".join(
                [
                    f"ca={ctx.obj.get('ca_server_url')}",
                    f"registry={ctx.obj.get('registry_server_url')}",
                    f"aic={aic}",
                ]
            )
        )

    @cert.command()
    @click.option("--aic", required=True)
    @click.pass_context
    def issue(ctx: click.Context, aic: str) -> None:
        click.echo(f"ca={ctx.obj.get('ca_server_url')}|aic={aic}")

    return cli


def test_reorder_hoists_group_option_after_subcommand() -> None:
    cli = _build_sample_cli()
    entity = cli.commands["entity"]
    assert isinstance(entity, click.Group)
    ctx = click.Context(entity)

    result = reorder_group_args(
        entity,
        ctx,
        ["derive", "--mtls-url", "https://custom.example.com:8443", "--ontology-aic", "1.2.3"],
    )

    assert result == [
        "--mtls-url",
        "https://custom.example.com:8443",
        "derive",
        "--ontology-aic",
        "1.2.3",
    ]


def test_reorder_pushes_child_option_written_before_subcommand() -> None:
    cli = _build_sample_cli()
    entity = cli.commands["entity"]
    assert isinstance(entity, click.Group)
    ctx = click.Context(entity)

    result = reorder_group_args(
        entity,
        ctx,
        ["--ontology-aic", "1.2.3", "--mtls-url", "https://custom.example.com:8443", "derive"],
    )

    assert result == [
        "--mtls-url",
        "https://custom.example.com:8443",
        "derive",
        "--ontology-aic",
        "1.2.3",
    ]


def test_reorder_does_not_steal_duplicate_server_url_from_child() -> None:
    cli = _build_sample_cli()
    cert = cli.commands["cert"]
    assert isinstance(cert, click.Group)
    ctx = click.Context(cert)

    result = reorder_group_args(
        cert,
        ctx,
        ["eab", "fetch", "--server-url", "http://registry.example:9001", "--aic", "1.2.3"],
    )

    assert result == ["eab", "fetch", "--server-url", "http://registry.example:9001", "--aic", "1.2.3"]


def test_entity_derive_accepts_mtls_url_in_any_position() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()
    argv_variants = (
        ["entity", "derive", "--mtls-url", "https://custom.example.com:8443", "--ontology-aic", "1.2.3"],
        ["entity", "--mtls-url", "https://custom.example.com:8443", "derive", "--ontology-aic", "1.2.3"],
        ["entity", "--ontology-aic", "1.2.3", "derive", "--mtls-url", "https://custom.example.com:8443"],
        ["entity", "derive", "--mtls-url=https://custom.example.com:8443", "--ontology-aic", "1.2.3"],
        [
            "entity",
            "derive",
            "--config",
            "demo.toml",
            "--verbose",
            "--mtls-url",
            "https://custom.example.com:8443",
            "--ontology-aic",
            "1.2.3",
            "--json",
        ],
    )

    for argv in argv_variants:
        result = runner.invoke(cli, argv)
        assert result.exit_code == 0, f"{argv!r} failed: {result.output}"
        assert "mtls=https://custom.example.com:8443" in result.output
        assert "aic=1.2.3" in result.output


def test_root_options_after_subcommand_still_apply() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()

    result = runner.invoke(
        cli,
        ["entity", "derive", "--config", "demo.toml", "--verbose", "--ontology-aic", "1.2.3"],
    )

    assert result.exit_code == 0, result.output
    assert "config=demo.toml" in result.output
    assert "verbose=True" in result.output


def test_cert_eab_server_url_after_fetch_stays_on_registry() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()

    result = runner.invoke(
        cli,
        ["cert", "eab", "fetch", "--server-url", "http://registry.example:9001", "--aic", "1.2.3"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ca=None|registry=http://registry.example:9001|aic=1.2.3"


def test_cert_issue_server_url_after_issue_applies_to_ca() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()

    result = runner.invoke(
        cli,
        ["cert", "issue", "--server-url", "http://ca.example:9443", "--aic", "1.2.3"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ca=http://ca.example:9443|aic=1.2.3"


def test_cert_keeps_split_server_urls_when_both_are_present() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()

    result = runner.invoke(
        cli,
        [
            "cert",
            "--server-url",
            "http://ca.example:9443",
            "eab",
            "fetch",
            "--server-url",
            "http://registry.example:9001",
            "--aic",
            "1.2.3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ca=http://ca.example:9443|registry=http://registry.example:9001|aic=1.2.3"


def test_subcommand_help_is_not_stolen_by_parent_group() -> None:
    runner = CliRunner()
    cli = _build_sample_cli()

    derive_help = runner.invoke(cli, ["entity", "derive", "--help"])
    entity_help = runner.invoke(cli, ["entity", "--help"])

    assert derive_help.exit_code == 0, derive_help.output
    assert entity_help.exit_code == 0, entity_help.output
    assert "--ontology-aic" in derive_help.output
    assert "--mtls-url" not in derive_help.output
    assert "--mtls-url" in entity_help.output
    assert "derive" in entity_help.output
