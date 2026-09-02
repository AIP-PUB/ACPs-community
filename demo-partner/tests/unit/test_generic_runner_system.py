"""tests/unit/test_generic_runner_system.py — GenericRunner System 发射单元测试（ES2–ES3）。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskState, TextDataItem

from partners.generic_runner import GenericRunner


def _read_system_records(runner: GenericRunner) -> list[dict[str, Any]]:
    log_file = runner._system_emitter._log_file
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_system_emitter_path_and_resource(mock_generic_runner: GenericRunner) -> None:
    emitter = mock_generic_runner._system_emitter
    assert "amp_system_test_agent.jsonl" in emitter._log_file.name
    assert emitter._resource is not None
    assert emitter._resource["service.name"] == "demo-partner-test_agent"


@pytest.mark.asyncio
async def test_call_llm_success_emits_sp1(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    result = await mock_generic_runner._call_llm("decision", "default", "sys", "user", task_id="tid123")

    assert result
    records = _read_system_records(mock_generic_runner)
    assert len(records) == 1
    body = records[0]["body"]
    assert body["category"] == "llm"
    assert records[0]["severity_number"] == 9
    assert body["model"] == "test-model"
    assert body["task_id"] == "tid123"
    assert body["tags"]["task_id"] == "tid123"
    assert records[0]["correlation_id"] == "tid123"


@pytest.mark.asyncio
async def test_call_llm_success_without_task_id(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    await mock_generic_runner._call_llm("decision", "default", "sys", "user")

    body = _read_system_records(mock_generic_runner)[0]["body"]
    assert "task_id" not in body


@pytest.mark.asyncio
async def test_call_llm_failure_emits_sp2_and_reraises(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    with (
        patch.object(
            mock_generic_runner.llm_clients["default"].chat.completions,
            "create",
            new_callable=AsyncMock,
            side_effect=Exception("llm_err"),
        ),
        pytest.raises(Exception, match="llm_err"),
    ):
        await mock_generic_runner._call_llm("analysis", "default", "sys", "user", task_id="tid123")

    record = _read_system_records(mock_generic_runner)[0]
    body = record["body"]
    assert body["category"] == "llm"
    assert record["severity_number"] == 17
    assert body["error_type"] == "Exception"
    assert "llm_err" in body["error_message"]
    assert body["model"] == "test-model"


@pytest.mark.asyncio
async def test_call_llm_emit_failure_does_not_block(mock_generic_runner: GenericRunner) -> None:
    with patch.object(mock_generic_runner._system_emitter, "emit_sync", side_effect=RuntimeError("emit fail")):
        result = await mock_generic_runner._call_llm("decision", "default", "sys", "user")
    assert result


@pytest.mark.asyncio
async def test_call_llm_backward_compatible_signature(mock_generic_runner: GenericRunner) -> None:
    await mock_generic_runner._call_llm("decision", "default", "prompt", "user")


@pytest.mark.asyncio
async def test_execute_skill_success_emits_sp1_and_sp3(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    mock_generic_runner.prompts = {
        "skills": {
            "china_transport.intercity": {"system": "skill prompt {{slots_text}}"},
        }
    }
    mock_generic_runner.skills_config = {"china_transport.intercity": {"name": "城际"}}

    await mock_generic_runner._execute_skill(
        "china_transport.intercity",
        "slots",
        "request",
        {"llm_profile": "default"},
        {},
        task_id="tid123",
    )

    records = _read_system_records(mock_generic_runner)
    assert len(records) == 2
    categories = {r["body"]["category"] for r in records}
    assert categories == {"llm", "skill"}
    skill_rec = next(r for r in records if r["body"]["category"] == "skill")
    assert skill_rec["body"]["module"] == "intercity"
    assert skill_rec["body"]["skill_id"] == "china_transport.intercity"
    assert isinstance(skill_rec["body"]["elapsed_ms"], int)
    assert skill_rec["body"]["task_id"] == "tid123"


@pytest.mark.asyncio
async def test_execute_skill_without_task_id(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")
    mock_generic_runner.prompts = {"skills": {"intercity": {"system": "x"}}}
    mock_generic_runner.skills_config = {"intercity": {"name": "城际"}}

    await mock_generic_runner._execute_skill("intercity", "", "req", {"llm_profile": "default"}, {})

    skill_rec = next(r for r in _read_system_records(mock_generic_runner) if r["body"]["category"] == "skill")
    assert "task_id" not in skill_rec["body"]


@pytest.mark.asyncio
async def test_execute_skill_failure_emits_sp2_and_sp4(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")
    mock_generic_runner.prompts = {"skills": {"intercity": {"system": "x"}}}
    mock_generic_runner.skills_config = {"intercity": {"name": "城际"}}

    with patch.object(
        mock_generic_runner.llm_clients["default"].chat.completions,
        "create",
        new_callable=AsyncMock,
        side_effect=Exception("skill llm fail"),
    ):
        result = await mock_generic_runner._execute_skill(
            "intercity", "", "req", {"llm_profile": "default"}, {}, task_id="tid123"
        )

    assert "Execution Failed" in result
    records = _read_system_records(mock_generic_runner)
    assert len(records) >= 2
    assert any(r["body"]["category"] == "llm" for r in records)
    skill_rec = next(r for r in records if r["body"]["category"] == "skill")
    assert skill_rec["severity_number"] == 17
    assert skill_rec["body"]["error_type"]


@pytest.mark.asyncio
async def test_execute_skill_emit_failure_still_returns(mock_generic_runner: GenericRunner) -> None:
    mock_generic_runner.prompts = {"skills": {"intercity": {"system": "x"}}}
    mock_generic_runner.skills_config = {"intercity": {"name": "城际"}}
    with (
        patch.object(
            mock_generic_runner.llm_clients["default"].chat.completions,
            "create",
            new_callable=AsyncMock,
            side_effect=Exception("skill llm fail"),
        ),
        patch.object(mock_generic_runner._system_emitter, "emit_sync", side_effect=RuntimeError("emit fail")),
    ):
        result = await mock_generic_runner._execute_skill("intercity", "", "req", {"llm_profile": "default"}, {})
    assert "Execution Failed" in result


@pytest.mark.asyncio
async def test_on_start_capacity_rejection_emits_sp5(mock_generic_runner: GenericRunner) -> None:
    log_file = mock_generic_runner._system_emitter._log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")
    mock_generic_runner.config["concurrency"]["max_concurrent_tasks"] = 1

    from datetime import datetime, timedelta, timezone

    from acps_sdk.aip.aip_base_model import TaskResult, TaskStatus

    beijing = timezone(timedelta(hours=8))
    busy_task = TaskResult(
        id="r1",
        sentAt=datetime.now(beijing).isoformat(),
        senderRole="partner",
        senderId="p",
        taskId="existing",
        sessionId="s1",
        status=TaskStatus(state=TaskState.Working, stateChangedAt=datetime.now(beijing).isoformat()),
    )
    from partners.generic_runner import TaskContext

    mock_generic_runner.tasks["existing"] = TaskContext(task=busy_task)

    command = TaskCommand(
        id="cmd-1",
        sentAt=datetime.now(beijing).isoformat(),
        senderRole="leader",
        senderId="leader",
        command=TaskCommandType.Start,
        taskId="new-task",
        sessionId="s1",
        dataItems=[TextDataItem(text="hello")],
    )

    result = await mock_generic_runner.on_start(command, None)

    assert result.status.state == TaskState.Rejected
    assert result.senderId == mock_generic_runner._aic
    record = _read_system_records(mock_generic_runner)[0]
    body = record["body"]
    assert body["category"] == "capacity"
    assert record["severity_number"] == 13
    assert body["active_tasks"] == 1
    assert body["max_concurrent"] == 1


@pytest.mark.asyncio
async def test_on_start_sp5_emit_failure_still_rejects(mock_generic_runner: GenericRunner) -> None:
    mock_generic_runner.config["concurrency"]["max_concurrent_tasks"] = 0
    command = TaskCommand(
        id="cmd-2",
        sentAt="2026-01-01T00:00:00+08:00",
        senderRole="leader",
        senderId="leader",
        command=TaskCommandType.Start,
        taskId="t2",
        sessionId="s1",
        dataItems=[TextDataItem(text="hello")],
    )
    with patch.object(mock_generic_runner._system_emitter, "emit_sync", side_effect=RuntimeError("emit fail")):
        result = await mock_generic_runner.on_start(command, None)
    assert result.status.state == TaskState.Rejected
