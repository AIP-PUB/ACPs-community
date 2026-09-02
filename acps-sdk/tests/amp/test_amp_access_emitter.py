"""tests/amp/test_amp_access_emitter.py — AccessEmitter 单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from acps_sdk.amp.access_emitter import AccessEmitter
from acps_sdk.amp.models import AccessBody, AccessParticipant, AccessRequest, AccessResponse


def _make_body() -> AccessBody:
    return AccessBody(
        request=AccessRequest(method="POST", route="/rpc", url="http://localhost/rpc"),
        response=AccessResponse(statusCode=200),
        caller=AccessParticipant(aic="caller-aic", serviceName="demo-leader"),
        callee=AccessParticipant(aic="callee-aic", serviceName="demo-partner"),
        durationMs=12,
    )


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "amp_access.jsonl"


def test_emit_sync_writes_valid_ndjson(tmp_log: Path) -> None:
    emitter = AccessEmitter(tmp_log, aic="aic-leader")
    log_id = emitter.emit_sync(
        _make_body(),
        trace_id="t" * 32,
        span_id="s" * 16,
        parent_span_id="",
        correlation_id="sess-1",
    )

    assert tmp_log.exists()
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_type"] == "access"
    assert record["aic"] == "aic-leader"
    assert record["log_id"] == log_id
    assert record["trace_id"] == "t" * 32
    assert record["span_id"] == "s" * 16
    assert record["parent_span_id"] == ""
    assert record["correlation_id"] == "sess-1"
    assert "integrity" not in record


def test_emit_sync_body_uses_camel_case_aliases(tmp_log: Path) -> None:
    emitter = AccessEmitter(tmp_log, aic="aic-leader")
    emitter.emit_sync(_make_body())

    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    body = record["body"]
    assert body["durationMs"] == 12
    assert body["caller"]["serviceName"] == "demo-leader"


def test_emit_sync_exclude_none(tmp_log: Path) -> None:
    emitter = AccessEmitter(tmp_log, aic="aic-leader")
    emitter.emit_sync(_make_body())

    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert "severity_text" not in record
    assert "severityText" not in record


def test_emit_sync_write_failure_only_warns(tmp_log: Path, caplog: pytest.LogCaptureFixture) -> None:
    emitter = AccessEmitter(tmp_log, aic="aic-leader")
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        log_id = emitter.emit_sync(_make_body())
    assert log_id
    assert "AccessEmitter" in caplog.text


@pytest.mark.asyncio
async def test_emit_async_equivalent_to_emit_sync(tmp_log: Path) -> None:
    emitter = AccessEmitter(tmp_log, aic="aic-leader")
    log_id = await emitter.emit(_make_body(), trace_id="trace-1", span_id="span-1")
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_id"] == log_id
    assert record["trace_id"] == "trace-1"


def test_access_emitter_importable_from_amp_package() -> None:
    """AccessEmitter 须从 acps_sdk.amp 包根导出（demo-leader/partner 使用方式）。"""
    from acps_sdk.amp import AccessEmitter as Exported

    assert Exported is AccessEmitter
