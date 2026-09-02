from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from acps_cli.main import main
from tests._monitor_support import run_monitor_fixture_action

pytestmark = pytest.mark.integration


def _invoke_monitor_json(config_path: Path, *argv: str) -> Any:
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(config_path), "monitor", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_monitor_status_reads_real_health_endpoint(monitor_conf: Path) -> None:
    payload = _invoke_monitor_json(monitor_conf, "status")

    assert payload["status"] == "ok"
    assert payload["service"] == "AMP Monitor Server"
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["redis"] == "ok"
    assert payload["checks"]["victoria_metrics"] == "ok"
    assert payload["checks"]["clickhouse"] == "ok"


def test_monitor_heartbeat_liveness_reads_seeded_redis_state(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("heartbeat-direct")

    payload = _invoke_monitor_json(monitor_conf, "heartbeat", "liveness", seed["aic"])

    assert payload["data"]["aic"] == seed["aic"]
    assert payload["data"]["isAlive"] is True
    assert payload["data"]["livenessState"] == "alive"


def test_monitor_metrics_snapshots_reads_seeded_snapshot(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("metrics-direct")

    payload = _invoke_monitor_json(monitor_conf, "metrics", "snapshots", "--aic", seed["aic"], "--limit", "5")

    items = payload["items"]
    assert items
    assert items[0]["aic"] == seed["aic"]
    assert items[0]["uptimeSeconds"] == 321.0


def test_monitor_access_events_reads_seeded_clickhouse_row(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("access-direct")

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
    assert event["requestRoute"] == "/rpc/acps-cli/live-access"
    assert event["responseStatus"] == 201


def test_monitor_message_events_reads_seeded_clickhouse_row(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("message-direct")

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


def test_monitor_system_events_reads_seeded_opensearch_doc(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("system-direct")

    payload = _invoke_monitor_json(
        monitor_conf,
        "system",
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
    assert event["message"] == "acps-cli monitor live system direct"


def test_monitor_audit_records_reads_seeded_postgres_row(monitor_conf: Path) -> None:
    seed = run_monitor_fixture_action("audit-direct")

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
