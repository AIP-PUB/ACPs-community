"""demo-leader System Emitter 单例。

复用 amp_setup.LEADER_AIC；系统事件日志写入 logs/amp_system.jsonl，
由 Fluent Bit 转发到 amp.system。
System 事件驱动（无周期任务），仅暴露 emitter 单例。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acps_sdk.amp import SystemEmitter
from assistant.amp_setup import LEADER_AIC

_SYSTEM_LOG_FILE = Path(__file__).parent.parent.parent / "logs" / "amp_system.jsonl"

LEADER_SERVICE_NAME = "demo-leader"

_SYSTEM_RESOURCE: dict[str, Any] = {
    "service.name": LEADER_SERVICE_NAME,
    "service.namespace": "acps-demo",
    "deployment.environment.name": "dev",
}

LEADER_SYSTEM_EMITTER = SystemEmitter(
    _SYSTEM_LOG_FILE,
    aic=LEADER_AIC,
    resource=_SYSTEM_RESOURCE,
)
