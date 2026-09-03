"""AMP Heartbeat 日志发射器。

将心跳事件以 NDJSON 周期性追加写入本地文件，供 Fluent Bit 尾追后转发到 Kafka amp.heartbeat。

与 AuditEmitter 的差异：
- 心跳是周期性事件 → 提供 run_periodic() 后台循环；
- 心跳 integrity 可选且 Writer 不校验 → 不接收 signer，不写 integrity；
- body 仅含可选 uptimeSeconds，由 emitter 依据进程运行时长自动计算。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from acps_sdk.amp.models import HeartbeatBody, HeartbeatLogRecord

_logger = logging.getLogger(__name__)


class HeartbeatEmitter:
    """AMP Heartbeat 日志发射器。

    Args:
        log_file: 目标 NDJSON 文件路径（不存在时自动创建父目录）。
        aic: 发射方 Agent Instance Context 标识符。
    """

    def __init__(self, log_file: Path, aic: str) -> None:
        self._log_file = log_file
        self._aic = aic
        self._start_monotonic = time.monotonic()

    def emit_sync(self, **kwargs: object) -> str:
        """构造并写入一条 HeartbeatLogRecord，返回 log_id（同步上下文用）。"""
        uptime = round(time.monotonic() - self._start_monotonic, 3)
        record = HeartbeatLogRecord(
            log_id=HeartbeatLogRecord.new_log_id(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            aic=self._aic,
            body=HeartbeatBody(uptime_seconds=uptime),
            **kwargs,  # type: ignore[arg-type]
        )
        self._write_line(record)
        return record.log_id

    async def emit(self, **kwargs: object) -> str:
        """异步版 emit_sync，通过 asyncio.to_thread 不阻塞事件循环。"""
        return await asyncio.to_thread(self.emit_sync, **kwargs)

    async def run_periodic(self, interval_seconds: float) -> None:
        """周期性发射心跳，直到被 cancel。

        立即发首条（验证更快），随后每 interval_seconds 一条。
        单次失败已在 _write_line 内吞掉，循环不中断。被 cancel 时正常退出。
        """
        while True:
            await self.emit()
            await asyncio.sleep(interval_seconds)

    def _write_line(self, record: HeartbeatLogRecord) -> None:
        """同步写一行 JSON（失败只 WARNING 不 raise）。"""
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record_dict = record.model_dump(mode="json", by_alias=True, exclude_none=True)
            line = json.dumps(record_dict, ensure_ascii=False)
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _logger.warning("HeartbeatEmitter: 写入失败，跳过", exc_info=True)
