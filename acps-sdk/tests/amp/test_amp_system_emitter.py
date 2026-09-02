"""tests/amp/test_amp_system_emitter.py — SystemEmitter 单元测试。"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from acps_sdk.amp.models import LogRecord
from acps_sdk.amp.system_emitter import SystemEmitter


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "amp_system.jsonl"


def test_emit_sync_writes_valid_ndjson(tmp_log: Path) -> None:
    resource = {"service.name": "demo-partner-test", "service.namespace": "acps-demo"}
    emitter = SystemEmitter(tmp_log, aic="aic-partner", resource=resource)
    log_id = emitter.emit_sync(
        {"message": "hi", "category": "llm"},
        severity_number=9,
        severity_text="INFO",
        correlation_id="tid-1",
    )

    assert tmp_log.exists()
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_type"] == "system"
    assert record["schema_version"] == "1.0"
    assert record["aic"] == "aic-partner"
    assert record["log_id"] == log_id
    assert record["severity_number"] == 9
    assert record["severity_text"] == "INFO"
    assert record["correlation_id"] == "tid-1"
    assert record["resource"] == resource
    assert record["body"]["message"] == "hi"
    assert "integrity" not in record


def test_emit_sync_exclude_none_trace_id(tmp_log: Path) -> None:
    emitter = SystemEmitter(tmp_log, aic="aic-partner")
    emitter.emit_sync({"message": "hi"}, severity_number=9, severity_text="INFO")

    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert "trace_id" not in record


def test_emit_sync_body_none(tmp_log: Path) -> None:
    emitter = SystemEmitter(tmp_log, aic="aic-partner")
    log_id = emitter.emit_sync(None, severity_number=9, severity_text="INFO")

    assert re.match(r"^[0-9a-f-]{36}$", log_id)
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_id"] == log_id
    assert "body" not in record


def test_log_id_is_valid_uuid(tmp_log: Path) -> None:
    emitter = SystemEmitter(tmp_log, aic="aic-partner")
    log_id = emitter.emit_sync({"message": "x"}, severity_number=9, severity_text="INFO")
    assert re.match(r"^[0-9a-f-]{36}$", log_id)


def test_resource_passed_to_log_record_constructor(tmp_log: Path) -> None:
    resource = {"service.name": "svc-a"}
    emitter = SystemEmitter(tmp_log, aic="aic-partner", resource=resource)
    captured: dict[str, Any] = {}
    original_init = LogRecord.__init__

    def _capture(self: LogRecord, **kwargs: Any) -> None:
        captured.update(kwargs)
        original_init(self, **kwargs)

    with patch.object(LogRecord, "__init__", _capture):
        emitter.emit_sync({"message": "hi"}, severity_number=9, severity_text="INFO")

    assert captured.get("resource") == resource


def test_new_log_id_fallback_uuid4(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_uuid7() -> str:
        raise AttributeError("uuid7")

    monkeypatch.setattr(uuid, "uuid7", _no_uuid7, raising=False)
    fixed = uuid.UUID("00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed)
    from acps_sdk.amp.system_emitter import _new_log_id

    assert _new_log_id() == str(fixed)


def test_emit_sync_write_failure_only_warns(tmp_log: Path, caplog: pytest.LogCaptureFixture) -> None:
    emitter = SystemEmitter(tmp_log, aic="aic-partner")
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        log_id = emitter.emit_sync({"message": "hi"}, severity_number=9, severity_text="INFO")
    assert log_id
    assert "SystemEmitter" in caplog.text


@pytest.mark.asyncio
async def test_emit_async_equivalent_to_emit_sync(tmp_log: Path) -> None:
    emitter = SystemEmitter(tmp_log, aic="aic-partner")
    log_id = await emitter.emit(
        {"message": "async"},
        severity_number=9,
        severity_text="INFO",
        correlation_id="corr-1",
    )
    record = json.loads(tmp_log.read_text(encoding="utf-8").strip())
    assert record["log_id"] == log_id
    assert record["correlation_id"] == "corr-1"
    assert record["severity_number"] == 9


def test_system_emitter_importable_from_amp_package() -> None:
    from acps_sdk.amp import SystemEmitter as Exported

    assert Exported is SystemEmitter
