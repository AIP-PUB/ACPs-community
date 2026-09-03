"""demo-leader Heartbeat Emitter 单例与周期任务管理。

复用 amp_setup.LEADER_AIC（从 atr/acs.json 读取）。
心跳写入 logs/amp_heartbeat.jsonl，由 Fluent Bit 转发到 Kafka amp.heartbeat。
emit 间隔默认 15s，可由 AMP_HEARTBEAT_INTERVAL_SECONDS 覆盖。
"""

import asyncio
import contextlib
import logging
import os

from acps_sdk.amp import HeartbeatEmitter
from assistant.amp_paths import resolve_amp_log_dir
from assistant.amp_setup import LEADER_AIC

_logger = logging.getLogger(__name__)

_HB_LOG_FILE = resolve_amp_log_dir() / "amp_heartbeat.jsonl"
_INTERVAL = float(os.environ.get("AMP_HEARTBEAT_INTERVAL_SECONDS", "15"))

LEADER_HEARTBEAT_EMITTER = HeartbeatEmitter(_HB_LOG_FILE, aic=LEADER_AIC)

_hb_task: asyncio.Task | None = None


def start_heartbeat() -> None:
    """在已运行的事件循环中启动周期心跳任务（幂等）。"""
    global _hb_task
    if _hb_task is None or _hb_task.done():
        _hb_task = asyncio.create_task(LEADER_HEARTBEAT_EMITTER.run_periodic(_INTERVAL), name="amp-heartbeat")
        _logger.info("AMP heartbeat started (aic=%s, interval=%ss)", LEADER_AIC, _INTERVAL)


async def stop_heartbeat() -> None:
    """取消周期心跳任务。"""
    global _hb_task
    if _hb_task is not None:
        _hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _hb_task
        _hb_task = None
