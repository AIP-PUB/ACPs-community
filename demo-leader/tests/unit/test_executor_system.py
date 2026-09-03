"""tests/unit/test_executor_system.py — TaskExecutor System 埋点单元测试（ES5）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture
def system_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_file = tmp_path / "amp_system.jsonl"
    import assistant.system_setup as system_setup

    monkeypatch.setattr(system_setup, "_SYSTEM_LOG_FILE", log_file)
    monkeypatch.setattr(
        system_setup.LEADER_SYSTEM_EMITTER,
        "_log_file",
        log_file,
    )
    return log_file


def _read_records(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_start_one_failure_emits_sl1(system_log_file: Path) -> None:
    from assistant.core.executor import ExecutorConfig, TaskExecutor

    executor = TaskExecutor(leader_aic="test-leader", config=ExecutorConfig(), acs_cache={})
    partner_aic = "urn:acps:partner:food-beijing"
    partner_tasks = {
        partner_aic: {
            "endpoint": "http://localhost:9021/rpc",
            "selection": MagicMock(),
            "aip_task_id": "task-1",
        }
    }

    mock_client = MagicMock()
    mock_client.start_task = AsyncMock(side_effect=Exception("conn refused"))

    with patch.object(executor, "_get_or_create_client", AsyncMock(return_value=mock_client)):
        results = await executor._start_all_partners("session-abc", partner_tasks)

    assert results[partner_aic][0] is None
    records = _read_records(system_log_file)
    assert len(records) == 1
    body = records[0]["body"]
    assert body["category"] == "error"
    assert records[0]["severity_number"] == 17
    assert body["partner_aic"] == partner_aic
    assert body["error_type"] == "Exception"
    assert body["tags"]["partner_aic"] == partner_aic
    assert records[0]["correlation_id"] == "session-abc"


@pytest.mark.asyncio
async def test_start_one_emit_failure_still_returns(system_log_file: Path) -> None:
    import assistant.system_setup as system_setup
    from assistant.core.executor import ExecutorConfig, TaskExecutor

    executor = TaskExecutor(leader_aic="test-leader", config=ExecutorConfig(), acs_cache={})
    partner_aic = "urn:acps:partner:food"
    partner_tasks = {
        partner_aic: {
            "endpoint": "http://localhost:9021/rpc",
            "selection": MagicMock(),
            "aip_task_id": "task-1",
        }
    }
    mock_client = MagicMock()
    mock_client.start_task = AsyncMock(side_effect=Exception("conn refused"))

    with (
        patch.object(executor, "_get_or_create_client", AsyncMock(return_value=mock_client)),
        patch.object(system_setup.LEADER_SYSTEM_EMITTER, "emit_sync", side_effect=RuntimeError("emit fail")),
    ):
        results = await executor._start_all_partners("session-abc", partner_tasks)

    assert results[partner_aic][1] == "conn refused"


@pytest.mark.asyncio
async def test_poll_until_converged_timeout_emits_sl2(system_log_file: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from assistant.core.executor import ExecutionPhase, ExecutionResult, ExecutorConfig, TaskExecutor

    executor = TaskExecutor(
        leader_aic="test-leader",
        config=ExecutorConfig(convergence_timeout_s=1, poll_interval_ms=10),
        acs_cache={},
    )
    result = ExecutionResult(phase=ExecutionPhase.POLLING)

    start = datetime(2026, 1, 1, tzinfo=UTC)
    late = start + timedelta(seconds=5)

    with (
        patch("assistant.core.executor.asyncio.sleep", AsyncMock()),
        patch("assistant.core.executor.datetime") as mock_dt,
        patch.object(executor, "_poll_all_partners", AsyncMock(return_value={})),
    ):
        mock_dt.UTC = UTC
        mock_dt.now.side_effect = [start, late]
        final = await executor._poll_until_converged("session-xyz", {}, result)

    assert final.phase == ExecutionPhase.TIMEOUT
    records = _read_records(system_log_file)
    assert len(records) == 1
    body = records[0]["body"]
    assert body["category"] == "capacity"
    assert records[0]["severity_number"] == 13
    assert isinstance(body["round_count"], int)
    assert body["convergence_timeout_s"] == 1
    assert records[0]["correlation_id"] == "session-xyz"


@pytest.mark.asyncio
async def test_poll_timeout_emit_failure_still_breaks(system_log_file: Path) -> None:
    from datetime import UTC, datetime, timedelta

    import assistant.system_setup as system_setup
    from assistant.core.executor import ExecutionPhase, ExecutionResult, ExecutorConfig, TaskExecutor

    executor = TaskExecutor(
        leader_aic="test-leader",
        config=ExecutorConfig(convergence_timeout_s=1, poll_interval_ms=10),
        acs_cache={},
    )
    result = ExecutionResult(phase=ExecutionPhase.POLLING)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    late = start + timedelta(seconds=5)

    with (
        patch("assistant.core.executor.asyncio.sleep", AsyncMock()),
        patch("assistant.core.executor.datetime") as mock_dt,
        patch.object(executor, "_poll_all_partners", AsyncMock(return_value={})),
        patch.object(system_setup.LEADER_SYSTEM_EMITTER, "emit_sync", side_effect=RuntimeError("emit fail")),
    ):
        mock_dt.now.side_effect = [start, late]
        final = await executor._poll_until_converged("session-xyz", {}, result)

    assert final.phase == ExecutionPhase.TIMEOUT
