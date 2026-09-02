from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from acps_cli.main import main

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "cli-help"
PROGRAM_NAME = "acps-cli"


def _load_fragments(file_name: str) -> list[str]:
    return [line.strip() for line in (FIXTURE_DIR / file_name).read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_fragments_in_order(output: str, fragments: list[str]) -> None:
    cursor = 0
    for fragment in fragments:
        next_cursor = output.find(fragment, cursor)
        assert next_cursor != -1, f"Missing help fragment: {fragment}\nActual output:\n{output}"
        cursor = next_cursor + len(fragment)


def test_unified_root_help_matches_golden_fragments() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert result.exit_code == 0
    _assert_fragments_in_order(result.output, _load_fragments("root.txt"))


def test_unified_admin_help_matches_golden_fragments() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["admin", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert result.exit_code == 0
    _assert_fragments_in_order(result.output, _load_fragments("admin.txt"))


def test_unified_cert_help_matches_golden_fragments() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["cert", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert result.exit_code == 0
    _assert_fragments_in_order(result.output, _load_fragments("cert.txt"))


def test_unified_admin_discovery_help_matches_golden_fragments() -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["admin", "discovery", "--help"],
        prog_name=PROGRAM_NAME,
        terminal_width=100,
    )

    assert result.exit_code == 0
    _assert_fragments_in_order(result.output, _load_fragments("admin-discovery.txt"))


def test_unified_monitor_help_matches_golden_fragments() -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["monitor", "--help"],
        prog_name=PROGRAM_NAME,
        terminal_width=100,
    )

    assert result.exit_code == 0
    _assert_fragments_in_order(result.output, _load_fragments("monitor.txt"))


def test_unified_registry_group_help_hides_timeout_override() -> None:
    runner = CliRunner()

    for argv in (
        ["auth", "--help"],
        ["agent", "--help"],
        ["entity", "--help"],
        ["cert", "eab", "--help"],
        ["admin", "auth", "--help"],
        ["admin", "registry", "--help"],
    ):
        result = runner.invoke(main, argv, prog_name=PROGRAM_NAME, terminal_width=100)
        assert result.exit_code == 0
        assert "--timeout" not in result.output


def test_unified_entity_help_exposes_mtls_url_override() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["entity", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert result.exit_code == 0
    assert "--server-url" in result.output
    assert "--mtls-url" in result.output


def test_unified_entity_derive_help_is_not_replaced_by_entity_help() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["entity", "derive", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert result.exit_code == 0
    assert "--ontology-aic" in result.output
    assert "--payload-file" in result.output
    assert "Manage derived entities." not in result.output


def test_unified_auth_help_exposes_change_password() -> None:
    runner = CliRunner()

    user_result = runner.invoke(main, ["auth", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)
    admin_result = runner.invoke(main, ["admin", "auth", "--help"], prog_name=PROGRAM_NAME, terminal_width=100)

    assert user_result.exit_code == 0
    assert admin_result.exit_code == 0
    assert "change-password" in user_result.output
    assert "change-password" in admin_result.output
    assert "logout" in user_result.output
    assert "logout" in admin_result.output


def test_unified_admin_registry_user_help_exposes_reset_password() -> None:
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["admin", "registry", "user", "--help"],
        prog_name=PROGRAM_NAME,
        terminal_width=100,
    )

    assert result.exit_code == 0
    assert "reset-password" in result.output
