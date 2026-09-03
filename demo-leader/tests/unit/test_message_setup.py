"""tests/unit/test_message_setup.py — message_setup.py 与 GroupManager 注入单元测试（EM4）。"""

from __future__ import annotations

import asyncio
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


def test_start_trace_generates_readable_context() -> None:
    import assistant.message_setup as message_setup

    message_setup.set_current_trace(None)
    ctx = message_setup.start_trace()
    assert ctx.trace_id
    assert ctx.span_id
    assert message_setup.get_current_trace() is ctx


def test_start_trace_uses_specified_trace_id() -> None:
    import assistant.message_setup as message_setup

    message_setup.set_current_trace(None)
    ctx = message_setup.start_trace(trace_id="fixed-trace-id")
    assert ctx.trace_id == "fixed-trace-id"
    assert message_setup.get_current_trace() is ctx


@pytest.mark.asyncio
async def test_concurrent_trace_context_isolation() -> None:
    import assistant.message_setup as message_setup

    async def worker() -> str:
        ctx = message_setup.start_trace()
        await asyncio.sleep(0.01)
        current = message_setup.get_current_trace()
        assert current is not None
        return current.trace_id

    trace_ids = await asyncio.gather(*(worker() for _ in range(5)))
    assert len(set(trace_ids)) == 5


@pytest.mark.asyncio
async def test_create_group_manager_passes_message_params() -> None:
    from assistant.core.group_manager import GroupConfig, GroupManager, RabbitMQConfig, create_group_manager
    from assistant.message_setup import LEADER_MESSAGE_EMITTER, get_current_trace

    emitter = LEADER_MESSAGE_EMITTER
    provider = get_current_trace

    with patch("assistant.core.group_manager.GroupLeader") as mock_leader_cls:
        mock_leader_cls.return_value = MagicMock()
        manager = create_group_manager(
            leader_aic="test-leader",
            rabbitmq_config=RabbitMQConfig(host="localhost"),
            group_config=GroupConfig(enabled=True),
            message_emitter=emitter,
            trace_context_provider=provider,
        )
        assert isinstance(manager, GroupManager)
        assert manager._message_emitter is emitter
        assert manager._trace_context_provider is provider

        await manager.start()
        kwargs = mock_leader_cls.call_args.kwargs
        assert kwargs["message_emitter"] is emitter
        assert kwargs["trace_context_provider"] is provider
