"""AMP Access 日志发射器。

将一次同步请求/响应交互以 NDJSON 追加写入本地文件，供 Fluent Bit 转发到 Kafka amp.access。
与 AuditEmitter 同构（事件驱动、同步核心 + asyncio.to_thread 异步包装），差异：
- 不签名（Access Writer 不校验 integrity）；
- body（AccessBody）由调用现场组装；
- 顶层接受 trace_id/span_id/parent_span_id/correlation_id 等追踪字段。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from acps_sdk.amp.models import AccessBody, AccessLogRecord

_logger = logging.getLogger(__name__)


class AccessEmitter:
    """AMP Access 日志发射器。"""

    def __init__(
        self,
        log_file: Path,
        aic: str,
        *,
        resource: dict[str, Any] | None = None,
    ) -> None:
        self._log_file = log_file
        self._aic = aic
        self._resource = resource

    def emit_sync(
        self,
        body: AccessBody,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        correlation_id: str | None = None,
        severity_text: str | None = None,
        severity_number: int | None = None,
    ) -> str:
        """构造并写入一条 AccessLogRecord，返回 log_id。"""
        record = AccessLogRecord(
            log_id=AccessLogRecord.new_log_id(),
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            aic=self._aic,
            body=body,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            correlation_id=correlation_id,
            severity_text=severity_text,
            severity_number=severity_number,
        )
        self._write_line(record)
        return record.log_id

    async def emit(self, body: AccessBody, **trace_kwargs: str | int | None) -> str:
        """异步版 emit_sync，通过 asyncio.to_thread 避免阻塞事件循环。"""
        return await asyncio.to_thread(self.emit_sync, body, **trace_kwargs)

    def _write_line(self, record: AccessLogRecord) -> None:
        """同步写入一行 JSON（失败只 WARNING，不 raise）。"""
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record_dict = record.model_dump(mode="json", by_alias=True, exclude_none=True)
            if self._resource:
                record_dict["resource"] = self._resource
            line = json.dumps(record_dict, ensure_ascii=False)
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _logger.warning("AccessEmitter: 写入失败，跳过", exc_info=True)
