"""acps_sdk.amp.HeartbeatEmitter 单元测试。

使用同步 pytest（无 pytest-asyncio），异步方法用 asyncio.run() 验证。
"""

import asyncio
import json
import uuid
from pathlib import Path

from acps_sdk.amp import HeartbeatEmitter, HeartbeatLogRecord, LogRecord


# ─── emit_sync ────────────────────────────────────────────────────────────────


def test_emit_sync_writes_valid_heartbeat_record(tmp_path: Path) -> None:
    """emit_sync() 写入的行能被 HeartbeatLogRecord.model_validate() 接受。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="1.2.3.test")

    log_id = emitter.emit_sync()

    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    record = HeartbeatLogRecord.model_validate(data)
    assert record.log_id == log_id
    assert record.log_type == "heartbeat"
    assert record.aic == "1.2.3.test"
    assert record.schema_version == "1.0"
    assert "+" in record.timestamp or "Z" in record.timestamp


def test_emit_sync_serialization_format(tmp_path: Path) -> None:
    """序列化结果顶层 snake_case，body 子字段 camelCase（uptimeSeconds）。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="test-aic")

    emitter.emit_sync()

    data = json.loads(log_file.read_text().strip())

    # 顶层 snake_case
    assert data["log_type"] == "heartbeat"
    assert "schema_version" in data
    assert "log_id" in data
    assert "timestamp" in data
    assert "aic" in data
    # integrity 不写（心跳不签名）
    assert "integrity" not in data

    # body camelCase
    body = data["body"]
    assert "uptimeSeconds" in body
    assert isinstance(body["uptimeSeconds"], (int, float))
    assert body["uptimeSeconds"] >= 0.0
    # 确认无 snake_case 别名泄露
    assert "uptime_seconds" not in body


def test_emit_sync_also_accepted_by_log_record(tmp_path: Path) -> None:
    """写入的行同时能被接收侧通用模型 LogRecord.model_validate() 接受。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="test-aic")

    emitter.emit_sync()

    data = json.loads(log_file.read_text().strip())
    record = LogRecord.model_validate(data)
    assert record.log_type == "heartbeat"
    assert record.aic == "test-aic"


def test_emit_sync_uptime_increases(tmp_path: Path) -> None:
    """多次 emit_sync() 后 uptimeSeconds 应递增（同一 emitter 实例）。"""
    import time

    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="aic")

    emitter.emit_sync()
    time.sleep(0.05)
    emitter.emit_sync()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    uptime1 = json.loads(lines[0])["body"]["uptimeSeconds"]
    uptime2 = json.loads(lines[1])["body"]["uptimeSeconds"]
    assert uptime2 > uptime1


def test_emit_sync_appends_multiple_lines(tmp_path: Path) -> None:
    """多次 emit_sync() 追加多行，不覆盖。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="aic")

    emitter.emit_sync()
    emitter.emit_sync()
    emitter.emit_sync()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 3
    ids = [json.loads(raw_line)["log_id"] for raw_line in lines]
    # 所有 log_id 唯一
    assert len(set(ids)) == 3


def test_emit_sync_creates_parent_dirs(tmp_path: Path) -> None:
    """父目录不存在时自动创建。"""
    log_file = tmp_path / "nested" / "deep" / "heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="aic")
    emitter.emit_sync()
    assert log_file.exists()


def test_emit_sync_failure_does_not_raise(tmp_path: Path) -> None:
    """写入失败时不 raise，只 WARNING（只读文件系统模拟：传目录路径）。"""
    emitter = HeartbeatEmitter(tmp_path, aic="aic")  # tmp_path 是目录，会失败
    emitter.emit_sync()  # 静默吞掉异常


def test_emit_sync_log_id_is_valid_uuid(tmp_path: Path) -> None:
    """emit_sync() 返回的 log_id 必须是合法 UUID 字符串。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="aic")
    log_id = emitter.emit_sync()
    parsed = uuid.UUID(log_id)
    assert str(parsed) == log_id


# ─── emit (async) ─────────────────────────────────────────────────────────────


def test_emit_async_equivalent_to_emit_sync(tmp_path: Path) -> None:
    """async emit() 行为等价于 emit_sync()。"""
    log_file = tmp_path / "amp_heartbeat.jsonl"
    emitter = HeartbeatEmitter(log_file, aic="aic")

    log_id = asyncio.run(emitter.emit())

    assert uuid.UUID(log_id)
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["log_id"] == log_id
    assert data["log_type"] == "heartbeat"


# ─── run_periodic ─────────────────────────────────────────────────────────────


def test_run_periodic_produces_multiple_records_then_cancels(tmp_path: Path) -> None:
    """run_periodic(0.05) 跑 ~0.2s 后 cancel，文件 >= 2 行，cancel 正常退出。"""

    async def _run() -> None:
        log_file = tmp_path / "amp_heartbeat.jsonl"
        emitter = HeartbeatEmitter(log_file, aic="aic")
        task = asyncio.create_task(emitter.run_periodic(0.05))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) >= 2
        for line in lines:
            data = json.loads(line)
            assert data["log_type"] == "heartbeat"

    asyncio.run(_run())


def test_run_periodic_first_record_emitted_immediately(tmp_path: Path) -> None:
    """run_periodic() 立即发首条（不等 interval_seconds 后才发）。"""

    async def _run() -> None:
        log_file = tmp_path / "amp_heartbeat.jsonl"
        emitter = HeartbeatEmitter(log_file, aic="aic")
        task = asyncio.create_task(emitter.run_periodic(60.0))  # 60s 间隔
        await asyncio.sleep(0.05)  # 只等 50ms
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) >= 1

    asyncio.run(_run())
