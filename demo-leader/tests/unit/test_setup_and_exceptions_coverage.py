from __future__ import annotations

import asyncio

import pytest
from assistant.models.exceptions import (
    BadRequestError,
    InternalError,
    LLMResponseError,
    LLMTimeoutError,
    PartnerProtocolError,
    PartnerTimeoutError,
    PartnerUnavailableError,
    PayloadTooLargeError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)


def test_exception_models_include_status_codes_and_details() -> None:
    errors = [
        BadRequestError(details={"field": "query"}),
        ValidationError("invalid", details={"why": "bad"}),
        PayloadTooLargeError(max_size=10, actual_size=20, field="query"),
        RateLimitError(retry_after=30),
        InternalError(details={"trace": "id"}),
        ServiceUnavailableError("registry"),
        PartnerUnavailableError("partner-a", "offline"),
        PartnerTimeoutError("partner-a", 1000),
        PartnerProtocolError("partner-a", "bad payload"),
        LLMTimeoutError("planner", 60),
        LLMResponseError("planner", "not json", raw_response="x" * 600),
    ]

    assert [err.http_status_code for err in errors] == [400, 400, 413, 429, 500, 503, 503, 504, 502, 504, 502]
    assert errors[0].to_dict()["data"] == {"field": "query"}
    assert errors[2].to_dict()["data"]["actual_size"] == 20
    assert errors[10].to_dict()["data"]["raw_response"] == "x" * 500


@pytest.mark.asyncio
async def test_heartbeat_start_is_idempotent_and_stop_clears_task(monkeypatch: pytest.MonkeyPatch) -> None:
    import assistant.heartbeat_setup as heartbeat_setup

    first_gate = asyncio.Event()

    async def _run_periodic(_interval: float) -> None:
        await first_gate.wait()

    task_holder: dict[str, asyncio.Task] = {}
    original_create_task = asyncio.create_task

    def _create_task(coro, name: str | None = None):
        task = original_create_task(coro, name=name)
        task_holder["task"] = task
        return task

    monkeypatch.setattr(heartbeat_setup.LEADER_HEARTBEAT_EMITTER, "run_periodic", _run_periodic)
    monkeypatch.setattr(heartbeat_setup.asyncio, "create_task", _create_task)
    heartbeat_setup._hb_task = None

    heartbeat_setup.start_heartbeat()
    first_task = heartbeat_setup._hb_task
    heartbeat_setup.start_heartbeat()
    assert heartbeat_setup._hb_task is first_task

    await heartbeat_setup.stop_heartbeat()
    assert heartbeat_setup._hb_task is None
    assert task_holder["task"].cancelled() is True


@pytest.mark.asyncio
async def test_stop_heartbeat_noops_when_not_started() -> None:
    import assistant.heartbeat_setup as heartbeat_setup

    heartbeat_setup._hb_task = None
    await heartbeat_setup.stop_heartbeat()
    assert heartbeat_setup._hb_task is None
