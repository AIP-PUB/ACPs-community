"""
D4 · 执行器选择逻辑测试
"""

from __future__ import annotations

import sys
from pathlib import Path

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _acs(*, streaming: bool = False, notification: bool = False) -> dict:
    return {
        "aic": "test-aic",
        "capabilities": {
            "streaming": streaming,
            "notification": notification,
        },
    }


# ---------------------------------------------------------------------------
# select_executor_strategy
# ---------------------------------------------------------------------------


def test_selects_stream_strategy_when_streaming_true():
    from assistant.core.executor_selector import ExecutorStrategy, select_executor_strategy

    result = select_executor_strategy(_acs(streaming=True))
    assert result == ExecutorStrategy.STREAM


def test_selects_notification_strategy_when_notification_true():
    from assistant.core.executor_selector import ExecutorStrategy, select_executor_strategy

    result = select_executor_strategy(_acs(notification=True))
    assert result == ExecutorStrategy.NOTIFICATION


def test_selects_rpc_strategy_as_fallback():
    from assistant.core.executor_selector import ExecutorStrategy, select_executor_strategy

    result = select_executor_strategy(_acs())
    assert result == ExecutorStrategy.RPC


def test_stream_takes_priority_over_notification():
    """当 streaming 和 notification 同时为 true 时，优先选 streaming。"""
    from assistant.core.executor_selector import ExecutorStrategy, select_executor_strategy

    result = select_executor_strategy(_acs(streaming=True, notification=True))
    assert result == ExecutorStrategy.STREAM


# ---------------------------------------------------------------------------
# build_executor_for_partner
# ---------------------------------------------------------------------------


def test_selects_stream_executor_when_streaming_true():
    from assistant.core.executor_selector import build_executor_for_partner
    from assistant.core.stream_executor import StreamExecutor

    executor = build_executor_for_partner(
        acs_data=_acs(streaming=True),
        leader_id="l1",
        partner_base_url="http://partner",
    )
    assert isinstance(executor, StreamExecutor)
    assert executor.stream_client._expected_partner_aic == "test-aic"
    awaitable = executor.close()
    import asyncio

    asyncio.run(awaitable)


def test_selects_notification_executor_when_notification_true():
    from assistant.core.executor_selector import build_executor_for_partner
    from assistant.core.notification_executor import NotificationExecutor

    executor = build_executor_for_partner(
        acs_data=_acs(notification=True),
        leader_id="l1",
        partner_base_url="http://partner",
        callback_base_url="http://leader/cb",
    )
    assert isinstance(executor, NotificationExecutor)
    assert executor._expected_partner_aic == "test-aic"
    import asyncio

    asyncio.run(executor.close())


def test_falls_back_to_rpc_executor():
    from assistant.core.executor import TaskExecutor
    from assistant.core.executor_selector import build_executor_for_partner

    executor = build_executor_for_partner(
        acs_data=_acs(),
        leader_id="l1",
        partner_base_url="http://partner",
    )
    assert isinstance(executor, TaskExecutor)


def test_falls_back_to_rpc_when_notification_true_but_no_callback_url():
    """notification=true 但没有 callback_base_url 时，降级到 RPC。"""
    from assistant.core.executor import TaskExecutor
    from assistant.core.executor_selector import build_executor_for_partner

    executor = build_executor_for_partner(
        acs_data=_acs(notification=True),
        leader_id="l1",
        partner_base_url="http://partner",
        callback_base_url=None,  # 没有 callback URL
    )
    assert isinstance(executor, TaskExecutor)
