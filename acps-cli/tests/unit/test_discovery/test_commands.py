"""Unit tests for discovery CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from acps_cli.main import main

REGISTRY_OIDC_CONFIG = (
    '[registry]\nbase_url = "http://localhost:9001"\n\n'
    '[registry.auth]\nmode = "oidc"\n'
    'issuer = "https://issuer.example/realms/acps-registry"\n'
    'client_id = "registry-cli"\n'
)


def test_query_supports_structured_request_options() -> None:
    runner = CliRunner()

    with patch("acps_cli.discovery.commands.query", return_value={"ok": True}) as mock_query:
        result = runner.invoke(
            main,
            [
                "discover",
                "--server-url",
                "http://localhost:9005",
                "query",
                "北京美食",
                "--type",
                "filtered",
                "--limit",
                "3",
                "--filter-json",
                '{"conditions":[{"field":"aic","op":"eq","value":"AIC-001"}]}',
                "--context-json",
                '{"traceId":"trace-001"}',
                "--forward-depth-limit",
                "2",
                "--forward-fanout-limit",
                "3",
                "--forward-fanout-remaining",
                "2",
                "--forward-chain",
                "AIC-DS-A",
                "--forward-chain",
                "AIC-DS-B",
                "--forward-trusted-server",
                "AIC-DS-C",
                "--forward-signature",
                "sig-1",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = mock_query.call_args.args[1]
    assert payload == {
        "type": "filtered",
        "query": "北京美食",
        "limit": 3,
        "filter": {"conditions": [{"field": "aic", "op": "eq", "value": "AIC-001"}]},
        "context": {"traceId": "trace-001"},
        "forwardDepthLimit": 2,
        "forwardFanoutLimit": 3,
        "forwardFanoutRemaining": 2,
        "forwardChain": ["AIC-DS-A", "AIC-DS-B"],
        "forwardTrustedServers": ["AIC-DS-C"],
        "forwardSignatures": ["sig-1"],
    }


def test_query_request_file_merges_command_line_overrides(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "type": "filtered",
                "limit": 9,
                "filter": {"conditions": [{"field": "active", "op": "eq", "value": True}]},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    with patch("acps_cli.discovery.commands.query", return_value={"ok": True}) as mock_query:
        result = runner.invoke(
            main,
            [
                "discover",
                "--server-url",
                "http://localhost:9005",
                "query",
                "--request-file",
                str(request_file),
                "--limit",
                "2",
                "--forward-chain",
                "AIC-DS-A",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = mock_query.call_args.args[1]
    assert payload["type"] == "filtered"
    assert payload["limit"] == 2
    assert payload["forwardChain"] == ["AIC-DS-A"]
    assert payload["filter"] == {"conditions": [{"field": "active", "op": "eq", "value": True}]}


def test_query_rejects_multiple_request_sources(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text("{}", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "discover",
            "--server-url",
            "http://localhost:9005",
            "query",
            "--request-json",
            "{}",
            "--request-file",
            str(request_file),
        ],
    )

    assert result.exit_code != 0
    assert "cannot be used together" in result.output


def test_sync_supports_incremental_mode() -> None:
    runner = CliRunner()

    with patch("acps_cli.discovery.commands.trigger_sync") as mock_trigger_sync:
        result = runner.invoke(
            main,
            [
                "admin",
                "discovery",
                "--server-url",
                "http://localhost:9005",
                "run-sync",
                "--no-hard-reset",
                "--skip-acs-check",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_trigger_sync.assert_called_once_with(
        "http://localhost:9005",
        hard_reset=False,
        min_acs_count=None,
    )


def test_dsp_status_outputs_json() -> None:
    runner = CliRunner()

    with patch(
        "acps_cli.discovery.commands.get_dsp_status",
        return_value={"is_running": True, "object_count_by_type": {"acs": 2}},
    ) as mock_get_dsp_status:
        result = runner.invoke(
            main,
            [
                "admin",
                "discovery",
                "--server-url",
                "http://localhost:9005",
                "dsp",
                "status",
                "--expect-acs-min",
                "2",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_get_dsp_status.assert_called_once_with("http://localhost:9005", min_acs_count=2)
    assert json.loads(result.output)["object_count_by_type"]["acs"] == 2


def test_dsp_register_webhook_uses_defaults() -> None:
    runner = CliRunner()

    with (
        patch(
            "acps_cli.discovery.commands._resolve_registry_admin_auth_headers",
            return_value={"Authorization": "Bearer admin-token"},
        ) as mock_auth_headers,
        patch(
            "acps_cli.discovery.commands.register_webhook",
            return_value={"id": "wh-001", "status": "active"},
        ) as mock_register_webhook,
    ):
        result = runner.invoke(
            main,
            [
                "admin",
                "discovery",
                "--server-url",
                "http://localhost:9005",
                "dsp",
                "register-webhook",
                "--url",
                "http://localhost:9015/admin/dsp/webhooks/receive",
                "--secret",
                "shared-secret",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_auth_headers.assert_called_once()
    mock_register_webhook.assert_called_once_with(
        "http://localhost:9005",
        {
            "url": "http://localhost:9015/admin/dsp/webhooks/receive",
            "secret": "shared-secret",
            "types": ["acs"],
            "events": ["data_change"],
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert json.loads(result.output)["id"] == "wh-001"


def test_resolve_registry_admin_auth_headers_uses_oidc_session(tmp_path: Path) -> None:
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text(REGISTRY_OIDC_CONFIG, encoding="utf-8")
    ctx_obj = {
        "toml_data": {
            "registry": {
                "base_url": "http://localhost:9001",
                "auth": {
                    "mode": "oidc",
                    "issuer": "https://issuer.example/realms/acps-registry",
                    "client_id": "registry-cli",
                },
            }
        },
        "config_dir": tmp_path,
    }

    with patch(
        "acps_cli.discovery.commands.OidcAuthSessionManager",
        return_value=SimpleNamespace(get_access_token=lambda: "oidc-admin-token"),
    ):
        from acps_cli.discovery.commands import _resolve_registry_admin_auth_headers

        headers = _resolve_registry_admin_auth_headers(ctx_obj)

    assert headers == {"Authorization": "Bearer oidc-admin-token"}


def test_resolve_registry_admin_auth_headers_uses_local_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "tokens" / "admin.json"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(json.dumps({"access_token": "local-admin-token", "token_type": "bearer"}), encoding="utf-8")
    ctx_obj = {
        "toml_data": {
            "registry": {"base_url": "http://localhost:9001"},
            "auth": {"admin_token_file": "tokens/admin.json"},
        },
        "config_dir": tmp_path,
    }

    from acps_cli.discovery.commands import _resolve_registry_admin_auth_headers

    headers = _resolve_registry_admin_auth_headers(ctx_obj)

    assert headers == {"Authorization": "Bearer local-admin-token"}
