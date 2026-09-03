from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acps_cli.main import main
from tests._monitor_support import run_monitor_fixture_action

pytestmark = pytest.mark.e2e


def _invoke_monitor_json(config_path: Path, *argv: str) -> Any:
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(config_path), "monitor", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_monitor_heartbeat_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("heartbeat-e2e")

    payload = _invoke_monitor_json(monitor_conf, "heartbeat", "liveness", seed["aic"])

    assert payload["data"]["aic"] == seed["aic"]
    assert payload["data"]["isAlive"] is True
    assert payload["data"]["livenessState"] == "alive"


def test_monitor_metrics_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("metrics-e2e")

    payload = _invoke_monitor_json(monitor_conf, "metrics", "snapshots", "--aic", seed["aic"], "--limit", "5")

    items = payload["items"]
    assert items
    assert items[0]["aic"] == seed["aic"]
    assert items[0]["uptimeSeconds"] == 456.0


def test_monitor_access_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("access-e2e")

    payload = _invoke_monitor_json(
        monitor_conf,
        "access",
        "events",
        "--aic",
        seed["aic"],
        "--start",
        seed["start_at"],
        "--end",
        seed["end_at"],
        "--limit",
        "10",
    )

    items = payload["items"]
    assert items
    event = next(item for item in items if item["logId"] == seed["log_id"])
    assert event["aic"] == seed["aic"]
    assert event["requestRoute"] == "/rpc/acps-cli/e2e-access"
    assert event["responseStatus"] == 202


def test_monitor_message_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("message-e2e")

    payload = _invoke_monitor_json(
        monitor_conf,
        "message",
        "events",
        "--message-id",
        seed["message_id"],
        "--start",
        seed["start_at"],
        "--end",
        seed["end_at"],
        "--limit",
        "10",
    )

    items = payload["items"]
    assert items
    event = next(item for item in items if item["logId"] == seed["log_id"])
    assert event["messageId"] == seed["message_id"]
    assert event["eventType"] == "send"


def test_monitor_system_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("system-e2e")

    payload = _invoke_monitor_json(
        monitor_conf,
        "system",
        "events",
        "--correlation-id",
        seed["correlation_id"],
        "--start",
        seed["start_at"],
        "--end",
        seed["end_at"],
        "--limit",
        "10",
    )

    items = payload["items"]
    assert items
    event = next(item for item in items if item["logId"] == seed["log_id"])
    assert event["aic"] == seed["aic"]
    assert event["correlationId"] == seed["correlation_id"]


def test_monitor_audit_e2e_workflow(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("audit-e2e")

    query_payload = _invoke_monitor_json(
        monitor_conf,
        "audit",
        "records",
        "--aic",
        seed["aic"],
        "--start",
        seed["start_at"],
        "--end",
        seed["end_at"],
        "--limit",
        "10",
    )

    items = query_payload["items"]
    assert items
    record_summary = next(item for item in items if item["logId"] == seed["log_id"])
    assert record_summary["aic"] == seed["aic"]
    assert record_summary["integrity"]["signatureVerified"] is True

    record_payload = _invoke_monitor_json(monitor_conf, "audit", "record", record_summary["auditId"])
    assert record_payload["logId"] == seed["log_id"]
    assert record_payload["aic"] == seed["aic"]
