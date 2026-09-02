"""tests/unit/test_access_setup.py — access_setup.py 与 TaskExecutor 注入单元测试（EA3）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def test_start_trace_generates_readable_context() -> None:
    import assistant.access_setup as access_setup

    access_setup.set_current_trace(None)
    ctx = access_setup.start_trace()
    assert ctx.trace_id
    assert ctx.span_id
    assert access_setup.get_current_trace() is ctx


@pytest.mark.asyncio
async def test_concurrent_trace_context_isolation() -> None:
    import assistant.access_setup as access_setup

    async def worker() -> str:
        ctx = access_setup.start_trace()
        await asyncio.sleep(0.01)
        current = access_setup.get_current_trace()
        assert current is not None
        return current.trace_id

    trace_ids = await asyncio.gather(*(worker() for _ in range(5)))
    assert len(set(trace_ids)) == 5


@pytest.mark.asyncio
async def test_get_or_create_client_injects_access_params() -> None:
    from assistant.core.executor import ExecutorConfig, TaskExecutor

    executor = TaskExecutor(
        leader_aic="test-leader",
        config=ExecutorConfig(),
        acs_cache={},
    )
    executor._last_partner_tasks = {
        "partner-food": {
            "selection": MagicMock(skill_name="beijing_food", skill_id="food.skill"),
        }
    }

    with patch("assistant.core.executor.AipRpcClient") as mock_client_cls:
        await executor._get_or_create_client("partner-food", "http://localhost:9021/rpc")
        kwargs = mock_client_cls.call_args.kwargs
        from assistant.access_setup import LEADER_ACCESS_EMITTER, LEADER_SERVICE_NAME, get_current_trace

        assert kwargs["access_emitter"] is LEADER_ACCESS_EMITTER
        assert kwargs["expected_partner_aic"] == "partner-food"
        assert kwargs["identity_binding_enabled"] is True
        assert kwargs["callee_aic"] == "partner-food"
        assert kwargs["caller_service"] == LEADER_SERVICE_NAME
        assert kwargs["callee_service"] == "demo-partner-beijing_food"
        assert kwargs["trace_context_provider"] is get_current_trace
