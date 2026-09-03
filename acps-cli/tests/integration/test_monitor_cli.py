from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from acps_cli.main import main

pytestmark = pytest.mark.integration

TIME_RANGE_REQUEST_JSON = '{"timeRange":{"startAt":"s","endAt":"e"}}'


def _config_file(tmp_path: Path) -> Path:
    config_path = tmp_path / "acps-cli.toml"
    config_path.write_text("# monitor CLI 测试生成\n", encoding="utf-8")
    return config_path


def _response(status_code: int, body: dict[str, object] | list[object] | str) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return response


def _invoke_monitor(runner: CliRunner, config_path: Path, *argv: str):
    return runner.invoke(
        main,
        ["--config", str(config_path), "monitor", "--server-url", "http://monitor.example.test:9009", *argv],
    )


def test_monitor_status_uses_health_endpoint(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"status": "ok"})) as mock_request:
        result = _invoke_monitor(runner, config_path, "status")

    assert result.exit_code == 0, result.output
    mock_request.assert_called_once_with(
        "GET",
        "http://monitor.example.test:9009/health",
        headers={"Accept": "application/json"},
        json=None,
        params=None,
        timeout=15.0,
    )
    assert json.loads(result.output) == {"status": "ok"}


def test_monitor_status_preserves_error_diagnostics(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(503, {"detail": "service unavailable"})):
        result = _invoke_monitor(runner, config_path, "status")

    assert result.exit_code != 0
    assert "service unavailable" in result.output
    assert "HTTP 503" in result.output


def test_heartbeat_query_builds_payload_from_shortcuts(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "heartbeat",
            "query",
            "--aic",
            "AIC-001",
            "--limit",
            "3",
            "--cursor",
            "cursor-1",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.args == (
        "POST",
        "http://monitor.example.test:9009/acps-amp-v1/heartbeat/liveness/query",
    )
    assert mock_request.call_args.kwargs["json"] == {
        "page": {"limit": 3, "cursor": "cursor-1"},
        "filter": {"logic": "and", "conditions": [{"field": "aic", "op": "eq", "value": "AIC-001"}]},
    }


@pytest.mark.parametrize(
    ("argv", "url"),
    [
        (("heartbeat", "summary"), "http://monitor.example.test:9009/acps-amp-v1/heartbeat/summary"),
        (
            ("heartbeat", "liveness", "AIC-001"),
            "http://monitor.example.test:9009/acps-amp-v1/heartbeat/liveness/AIC-001",
        ),
    ],
)
def test_heartbeat_get_commands_use_expected_paths(tmp_path: Path, argv: tuple[str, ...], url: str) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"ok": True})) as mock_request:
        result = _invoke_monitor(runner, config_path, *argv)

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.args == ("GET", url)


def test_path_lookup_commands_url_encode_resource_ids(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"ok": True})) as mock_request:
        heartbeat_result = _invoke_monitor(runner, config_path, "heartbeat", "liveness", "AIC/001 with space")
        trace_result = _invoke_monitor(runner, config_path, "access", "trace", "trace/001 with space")
        lifecycle_result = _invoke_monitor(runner, config_path, "message", "lifecycle", "msg/001 with space")
        record_result = _invoke_monitor(runner, config_path, "audit", "record", "audit/001 with space")
        task_result = _invoke_monitor(runner, config_path, "audit", "verify-task", "task/001 with space")

    assert heartbeat_result.exit_code == 0, heartbeat_result.output
    assert trace_result.exit_code == 0, trace_result.output
    assert lifecycle_result.exit_code == 0, lifecycle_result.output
    assert record_result.exit_code == 0, record_result.output
    assert task_result.exit_code == 0, task_result.output
    calls = mock_request.call_args_list
    assert calls[0].args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/heartbeat/liveness/AIC%2F001%20with%20space",
    )
    assert calls[1].args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/access/traces/trace%2F001%20with%20space",
    )
    assert calls[2].args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/message/lifecycles/msg%2F001%20with%20space",
    )
    assert calls[3].args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/audit/records/audit%2F001%20with%20space",
    )
    assert calls[4].args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/audit/integrity/verify/task%2F001%20with%20space",
    )


def test_heartbeat_query_requires_aic_without_request(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request") as mock_request:
        result = _invoke_monitor(runner, config_path, "heartbeat", "query")

    assert result.exit_code != 0
    assert "requires --aic" in result.output
    mock_request.assert_not_called()


def test_metrics_snapshots_builds_page_and_aic_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "metrics",
            "snapshots",
            "--aic",
            "AIC-001",
            "--limit",
            "5",
            "--cursor",
            "cursor-5",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "page": {"limit": 5, "cursor": "cursor-5"},
        "filter": {"logic": "and", "conditions": [{"field": "aic", "op": "eq", "value": "AIC-001"}]},
    }


def test_metrics_series_builds_metric_time_range_step_and_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "metrics",
            "series",
            "--metric",
            "cpu.usage",
            "--aic",
            "AIC-001",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--step",
            "1m",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "metric": "cpu.usage",
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "step": "1m",
        "filter": {"logic": "and", "conditions": [{"field": "aic", "op": "eq", "value": "AIC-001"}]},
    }


def test_metrics_rankings_propagates_server_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(404, {"detail": "analytics disabled"})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "metrics",
            "rankings",
            "--metric",
            "cpu.usage",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--top-n",
            "10",
        )

    assert result.exit_code != 0
    assert "analytics disabled" in result.output
    assert mock_request.call_args.kwargs["json"] == {
        "metric": "cpu.usage",
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "topN": 10,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("metrics", "series", "--request-json", '{"metric":"cpu.usage","timeRange":{"startAt":"s","endAt":"e"}}'),
        ("metrics", "rankings", "--request-json", '{"metric":"cpu.usage","timeRange":{"startAt":"s","endAt":"e"}}'),
    ],
)
def test_metrics_allows_request_payload_without_shortcut_required_fields(tmp_path: Path, argv: tuple[str, ...]) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(runner, config_path, *argv)

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == json.loads(argv[-1])


def test_access_events_builds_shortcut_payload(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "access",
            "events",
            "--aic",
            "AIC-001",
            "--trace-id",
            "trace-001",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--limit",
            "20",
            "--cursor",
            "cursor-20",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "page": {"limit": 20, "cursor": "cursor-20"},
        "filter": {
            "logic": "and",
            "conditions": [
                {"field": "aic", "op": "eq", "value": "AIC-001"},
                {"field": "traceId", "op": "eq", "value": "trace-001"},
            ],
        },
    }


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (("access", "operations", "--request-json", TIME_RANGE_REQUEST_JSON), "/access/operations/query"),
        (("access", "traces", "--request-json", TIME_RANGE_REQUEST_JSON), "/access/traces/query"),
        (("access", "slow", "--request-json", TIME_RANGE_REQUEST_JSON), "/access/slow-requests/top"),
        (("access", "errors", "--request-json", TIME_RANGE_REQUEST_JSON), "/access/errors/attribution"),
        (("access", "topology", "--request-json", TIME_RANGE_REQUEST_JSON), "/access/topology/query"),
        (("message", "lifecycles", "--request-json", TIME_RANGE_REQUEST_JSON), "/message/lifecycles/query"),
        (("message", "deadletters", "--request-json", TIME_RANGE_REQUEST_JSON), "/message/deadletters/query"),
        (("message", "destinations", "--request-json", TIME_RANGE_REQUEST_JSON), "/message/destinations/query"),
        (
            ("message", "throughput", "--request-json", '{"system":"mq","destinationName":"queue.demo"}'),
            "/message/destinations/throughput",
        ),
        (("audit", "verify", "--request-json", '{"recordIds":["audit-001"]}'), "/audit/integrity/verify"),
    ],
)
def test_payload_only_commands_require_and_forward_request(
    tmp_path: Path,
    argv: tuple[str, ...],
    path: str,
) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"ok": True})) as mock_request:
        result = _invoke_monitor(runner, config_path, *argv)

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.args[1] == f"http://monitor.example.test:9009/acps-amp-v1{path}"
    assert mock_request.call_args.kwargs["json"] == json.loads(argv[-1])


@pytest.mark.parametrize(
    "argv",
    [
        ("access", "operations"),
        ("access", "traces"),
        ("access", "slow"),
        ("access", "errors"),
        ("access", "topology"),
        ("message", "lifecycles"),
        ("message", "deadletters"),
        ("message", "destinations"),
        ("message", "throughput"),
        ("audit", "verify"),
    ],
)
def test_payload_only_commands_reject_missing_request(tmp_path: Path, argv: tuple[str, ...]) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request") as mock_request:
        result = _invoke_monitor(runner, config_path, *argv)

    assert result.exit_code != 0
    assert "requires --request-json or --request-file" in result.output
    mock_request.assert_not_called()


def test_access_trace_maps_include_events_query_parameter(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"traceId": "trace-001"})) as mock_request:
        result = _invoke_monitor(runner, config_path, "access", "trace", "trace-001", "--include-events")

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.args == (
        "GET",
        "http://monitor.example.test:9009/acps-amp-v1/access/traces/trace-001",
    )
    assert mock_request.call_args.kwargs["params"] == {"include_events": True}


def test_message_events_builds_shortcut_payload(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "message",
            "events",
            "--message-id",
            "msg-001",
            "--trace-id",
            "trace-001",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--limit",
            "15",
            "--cursor",
            "cursor-15",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "page": {"limit": 15, "cursor": "cursor-15"},
        "filter": {
            "logic": "and",
            "conditions": [
                {"field": "messageId", "op": "eq", "value": "msg-001"},
                {"field": "traceId", "op": "eq", "value": "trace-001"},
            ],
        },
    }


def test_message_lifecycle_maps_query_parameter_aliases(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"messageId": "msg-001"})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "message",
            "lifecycle",
            "msg-001",
            "--system",
            "mq",
            "--destination-name",
            "queue.demo",
            "--destination-kind",
            "queue",
            "--virtual-host",
            "/",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["params"] == {
        "system": "mq",
        "destinationName": "queue.demo",
        "destinationKind": "queue",
        "virtualHost": "/",
    }


def test_system_events_builds_shortcut_payload(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "system",
            "events",
            "--aic",
            "AIC-001",
            "--correlation-id",
            "corr-001",
            "--severity-min",
            "13",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--limit",
            "25",
            "--cursor",
            "cursor-25",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "page": {"limit": 25, "cursor": "cursor-25"},
        "filter": {
            "logic": "and",
            "conditions": [
                {"field": "aic", "op": "eq", "value": "AIC-001"},
                {"field": "correlationId", "op": "eq", "value": "corr-001"},
                {"field": "severityNumber", "op": "gte", "value": 13},
            ],
        },
    }


def test_audit_records_builds_keyword_and_filter_payload(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"items": []})) as mock_request:
        result = _invoke_monitor(
            runner,
            config_path,
            "audit",
            "records",
            "--aic",
            "AIC-001",
            "--keyword",
            "audit-001",
            "--start",
            "2026-06-25T00:00:00Z",
            "--end",
            "2026-06-25T01:00:00Z",
            "--limit",
            "8",
            "--cursor",
            "cursor-8",
        )

    assert result.exit_code == 0, result.output
    assert mock_request.call_args.kwargs["json"] == {
        "timeRange": {"startAt": "2026-06-25T00:00:00Z", "endAt": "2026-06-25T01:00:00Z"},
        "page": {"limit": 8, "cursor": "cursor-8"},
        "keyword": "audit-001",
        "filter": {"logic": "and", "conditions": [{"field": "aic", "op": "eq", "value": "AIC-001"}]},
    }


def test_audit_get_commands_use_expected_paths_and_params(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = _config_file(tmp_path)

    with patch("httpx.request", return_value=_response(200, {"ok": True})) as mock_request:
        record_result = _invoke_monitor(runner, config_path, "audit", "record", "audit-001")
        anchors_result = _invoke_monitor(runner, config_path, "audit", "anchors", "--chain-id", "audit-chain-001")
        task_result = _invoke_monitor(runner, config_path, "audit", "verify-task", "task-001")

    assert record_result.exit_code == 0, record_result.output
    assert anchors_result.exit_code == 0, anchors_result.output
    assert task_result.exit_code == 0, task_result.output
    calls = mock_request.call_args_list
    assert calls[0].args == ("GET", "http://monitor.example.test:9009/acps-amp-v1/audit/records/audit-001")
    assert calls[1].args == ("GET", "http://monitor.example.test:9009/acps-amp-v1/audit/anchors/latest")
    assert calls[1].kwargs["params"] == {"chain_id": "audit-chain-001"}
    assert calls[2].args == ("GET", "http://monitor.example.test:9009/acps-amp-v1/audit/integrity/verify/task-001")
