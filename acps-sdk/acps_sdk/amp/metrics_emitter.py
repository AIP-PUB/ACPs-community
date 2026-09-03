"""AMP Metrics 日志发射器。

将指标事件以 NDJSON 周期性追加写入本地文件，供 Fluent Bit 尾追后转发到 Kafka amp.metrics。

与 HeartbeatEmitter 的差异：
- Metrics body 由外部 SampleProvider 提供（由 Agent 测量真实负载）；
- resource 字段携带服务标识（service.name / service.namespace / deployment.environment.name）；
- 失败只 WARNING，循环不中断，可 cancel。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from acps_sdk.amp.models import MetricsBody, MetricsLogRecord

_logger = logging.getLogger(__name__)


@runtime_checkable
class SampleProvider(Protocol):
    """指标采样协议：由使用方实现，按需提供 MetricsBody。"""

    def sample(self) -> MetricsBody:
        """采集一份当前指标快照，返回 MetricsBody。"""
        ...


class MetricsEmitter:
    """AMP Metrics 日志发射器。

    Args:
        log_file: 目标 NDJSON 文件路径（不存在时自动创建父目录）。
        aic: 发射方 Agent Instance Context 标识符。
        sampler: 提供 MetricsBody 的采样器（实现 SampleProvider 协议）。
        resource: 日志来源描述（service.name / service.namespace 等），可选。
    """

    def __init__(
        self,
        log_file: Path,
        aic: str,
        sampler: SampleProvider,
        resource: dict[str, Any] | None = None,
    ) -> None:
        self._log_file = log_file
        self._aic = aic
        self._sampler = sampler
        self._resource = resource

    def emit_sync(self) -> str:
        """采样并写入一条 MetricsLogRecord，返回 log_id（同步上下文用）。"""
        body = self._sampler.sample()
        record = MetricsLogRecord(
            log_id=MetricsLogRecord.new_log_id(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            aic=self._aic,
            body=body,
            resource=self._resource,
        )
        self._write_line(record)
        return record.log_id

    async def emit(self) -> str:
        """异步版 emit_sync，通过 asyncio.to_thread 不阻塞事件循环。"""
        return await asyncio.to_thread(self.emit_sync)

    async def run_periodic(self, interval_seconds: float) -> None:
        """周期性发射指标，直到被 cancel。

        立即发首条（验证更快），随后每 interval_seconds 一条。
        单次失败已在 _write_line 内吞掉，循环不中断。被 cancel 时正常退出。
        """
        while True:
            await self.emit()
            await asyncio.sleep(interval_seconds)

    def _write_line(self, record: MetricsLogRecord) -> None:
        """同步写一行 JSON（失败只 WARNING 不 raise）。"""
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record_dict = record.model_dump(mode="json", by_alias=True, exclude_none=True)
            line = json.dumps(record_dict, ensure_ascii=False)
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _logger.warning("MetricsEmitter: 写入失败，跳过", exc_info=True)


__all__ = ["MetricsEmitter", "SampleProvider"]
