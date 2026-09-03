"""tests/amp/test_amp_message_emitter.py — MessageEmitter 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from acps_sdk.amp.message_emitter import MessageEmitter
from acps_sdk.amp.models import MessageBody, MessageDestination


def _make_body() -> MessageBody:
    return MessageBody(
        event_type="send",
        system="rabbitmq",
        destination=MessageDestination(name="group.test", kind="exchange"),
        message_id="msg-001",
        payload_size_bytes=128,
    )


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "amp_message.jsonl"


def test_emit_sync_writes_valid_ndjson(tmp_log: Path) -> None:
    emitter = MessageEmitter(tmp_log, aic="aic-leader")
    log_id = emitter.emit_sync(
        _make_body(),
        trace_id="t" * 32,
        span_id="s" * 16,
        parent_span_id="",
        correlation_id="sess-1",
    )

    assert tmp_log.exists()
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_type"] == "message"
    assert record["aic"] == "aic-leader"
    assert record["log_id"] == log_id
    assert record["trace_id"] == "t" * 32
    assert record["span_id"] == "s" * 16
    assert record["parent_span_id"] == ""
    assert record["correlation_id"] == "sess-1"
    assert "integrity" not in record


def test_emit_sync_body_uses_camel_case_aliases(tmp_log: Path) -> None:
    emitter = MessageEmitter(tmp_log, aic="aic-leader")
    emitter.emit_sync(_make_body())

    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    body = record["body"]
    assert body["eventType"] == "send"
    assert body["messageId"] == "msg-001"
    assert body["payloadSizeBytes"] == 128


def test_emit_sync_exclude_none(tmp_log: Path) -> None:
    emitter = MessageEmitter(tmp_log, aic="aic-leader")
    emitter.emit_sync(_make_body())

    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert "severity_text" not in record
    assert "severityText" not in record


def test_emit_sync_write_failure_only_warns(tmp_log: Path, caplog: pytest.LogCaptureFixture) -> None:
    emitter = MessageEmitter(tmp_log, aic="aic-leader")
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        log_id = emitter.emit_sync(_make_body())
    assert log_id
    assert "MessageEmitter" in caplog.text


@pytest.mark.asyncio
async def test_emit_async_equivalent_to_emit_sync(tmp_log: Path) -> None:
    emitter = MessageEmitter(tmp_log, aic="aic-leader")
    log_id = await emitter.emit(_make_body(), trace_id="trace-1", span_id="span-1")
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_id"] == log_id
    assert record["trace_id"] == "trace-1"


def test_message_emitter_importable_from_amp_package() -> None:
    from acps_sdk.amp import MessageEmitter as Exported

    assert Exported is MessageEmitter
