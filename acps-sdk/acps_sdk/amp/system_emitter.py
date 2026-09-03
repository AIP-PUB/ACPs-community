"""AMP System 日志发射器。

将一次内部系统事件以 NDJSON 追加写入本地文件，供 Fluent Bit 转发到 Kafka amp.system。
与 AccessEmitter 同构（事件驱动、同步核心 + asyncio.to_thread 异步包装），差异：
- body 是自由格式 dict[str, Any]（无 SystemBody 强类型，直接使用 LogRecord）；
- 使用 severity_number / severity_text（System 日志的核心过滤维度）；
- 不签名（System Writer 不校验 integrity）；
- 不传播 trace 上下文（System 日志是单侧内部事件，不跨进程传播 traceparent）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from acps_sdk.amp.models import LogRecord

_logger = logging.getLogger(__name__)


def _new_log_id() -> str:
    """生成 log_id（Python 3.14+ uuid7，旧版本 fallback uuid4）。"""
    try:
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    except AttributeError:
        return str(uuid.uuid4())


class SystemEmitter:
    """AMP System 日志发射器。"""

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
        body: dict[str, Any] | None,
        *,
        severity_number: int | None = None,
        severity_text: str | None = None,
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """构造并写入一条 system LogRecord，返回 log_id。"""
        log_id = _new_log_id()
        record = LogRecord(
            schema_version="1.0",
            log_type="system",
            log_id=log_id,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            aic=self._aic,
            body=body,
            severity_number=severity_number,
            severity_text=severity_text,
            trace_id=trace_id,
            correlation_id=correlation_id,
            resource=self._resource,
        )
        self._write_line(record)
        return log_id

    async def emit(
        self,
        body: dict[str, Any] | None,
        **kwargs: int | str | None,
    ) -> str:
        """异步版 emit_sync，通过 asyncio.to_thread 避免阻塞事件循环。"""
        return await asyncio.to_thread(self.emit_sync, body, **kwargs)

    def _write_line(self, record: LogRecord) -> None:
        """同步写入一行 JSON（失败只 WARNING，不 raise）。"""
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                record.model_dump(mode="json", by_alias=True, exclude_none=True),
                ensure_ascii=False,
            )
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _logger.warning("SystemEmitter: 写入失败，跳过", exc_info=True)
