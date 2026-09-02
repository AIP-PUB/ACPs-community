"""tests/amp/test_amp_metrics_emitter.py — MetricsEmitter 单元测试（Step E1）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from acps_sdk.amp.metrics_emitter import MetricsEmitter, SampleProvider
from acps_sdk.amp.models import LoadMetrics, MetricsBody, WindowMetrics


def _make_sampler(uptime: float = 100.0) -> SampleProvider:
    """构造一个返回固定 MetricsBody 的合成采样器。"""
    m = MagicMock(spec=SampleProvider)
    m.sample.return_value = MetricsBody(uptime_seconds=uptime)
    return m


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "amp_metrics.jsonl"


# ── emit_sync ─────────────────────────────────────────────────────────────────


def test_emit_sync_writes_valid_ndjson(tmp_log: Path) -> None:
    """emit_sync 写入合法 NDJSON，log_type=metrics，含 resource 字段。"""
    sampler = _make_sampler(uptime=123.0)
    resource = {"service.name": "svc-test", "deployment.environment.name": "dev"}
    emitter = MetricsEmitter(tmp_log, aic="aic-001", sampler=sampler, resource=resource)

    log_id = emitter.emit_sync()

    assert tmp_log.exists()
    lines = tmp_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["log_type"] == "metrics"
    assert record["aic"] == "aic-001"
    assert record["log_id"] == log_id
    assert "timestamp" in record
    assert "body" in record
    assert record["resource"] == resource  # D-6 resource 字段存在


def test_emit_sync_body_uses_camel_case_aliases(tmp_log: Path) -> None:
    """序列化后 body 字段使用 camelCase alias（by_alias=True）。"""
    sampler = MagicMock(spec=SampleProvider)
    sampler.sample.return_value = MetricsBody(
        uptime_seconds=50.0,
        load_metrics=LoadMetrics(active_tasks=2, queued_tasks=3),
    )
    emitter = MetricsEmitter(tmp_log, aic="aic-001", sampler=sampler)
    emitter.emit_sync()

    record = json.loads(tmp_log.read_text().strip())
    body = record["body"]
    assert "uptimeSeconds" in body
    assert "loadMetrics" in body
    assert "activeTasks" in body["loadMetrics"]


def test_emit_sync_exclude_none(tmp_log: Path) -> None:
    """序列化后 None 字段不输出（exclude_none=True）。"""
    sampler = _make_sampler()
    emitter = MetricsEmitter(tmp_log, aic="aic-001", sampler=sampler)
    emitter.emit_sync()

    record = json.loads(tmp_log.read_text().strip())
    assert "resource" not in record  # resource 为 None 时不出现


def test_emit_sync_write_failure_only_warns(tmp_log: Path, caplog: pytest.LogCaptureFixture) -> None:
    """写入失败（目录不可写）时只 WARNING 不 raise。"""
    sampler = _make_sampler()
    bad_path = Path("/readonly_nonexistent_dir/amp_metrics.jsonl")
    emitter = MetricsEmitter(bad_path, aic="aic-001", sampler=sampler)

    with caplog.at_level("WARNING"):
        emitter.emit_sync()  # 不应 raise

    assert any("写入失败" in m or "MetricsEmitter" in m for m in caplog.messages)


# ── emit (async) ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_emit_async_writes_line(tmp_log: Path) -> None:
    """异步 emit 写一行（与 emit_sync 等效）。"""
    sampler = _make_sampler()
    emitter = MetricsEmitter(tmp_log, aic="aic-002", sampler=sampler)

    await emitter.emit()

    assert tmp_log.exists()
    lines = tmp_log.read_text().strip().splitlines()
    assert len(lines) == 1


# ── run_periodic ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_periodic_emits_immediately_then_interval(tmp_log: Path) -> None:
    """run_periodic 立即发首条，间隔后再发；cancel 不抛 CancelledError。"""
    sampler = _make_sampler()
    emitter = MetricsEmitter(tmp_log, aic="aic-003", sampler=sampler)

    task = asyncio.create_task(emitter.run_periodic(0.05))
    await asyncio.sleep(0.12)  # 够发 2-3 条
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass  # 正常退出

    lines = tmp_log.read_text().strip().splitlines()
    assert len(lines) >= 2  # 至少 2 条
