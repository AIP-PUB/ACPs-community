"""AMP Audit 日志发射器。

同步核心 _write_line() 追加写 NDJSON；
async emit() 通过 asyncio.to_thread() 包装，不阻塞事件循环；
同步调用方（如不在 asyncio 上下文的代码）使用 emit_sync()。

签名支持（可选）：
    传入 signer 参数后，每条日志在写入前自动完成 Ed25519 / RSA 签名，
    签名结果以 integrity 字段随记录持久化。未传 signer 时行为与旧版完全一致。
"""

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from acps_sdk.amp.models import AuditBody, AuditLogRecord
from acps_sdk.amp.signer import AuditSigner

_logger = logging.getLogger(__name__)


class AuditEmitter:
    """AMP Audit 日志发射器。

    将审计事件以 NDJSON 格式追加写入本地文件，
    供 Fluent Bit 尾追后转发到 Kafka。

    Args:
        log_file: 目标 NDJSON 日志文件路径（不存在时自动创建父目录）。
        aic: 发射方的 Agent Instance Context 标识符。
        signer: 可选签名器；传入后每条日志自动附加 integrity 字段。
                未传时日志不含签名，monitor-server 将以 missing_public_key 标记入库。
    """

    def __init__(
        self,
        log_file: Path,
        aic: str,
        signer: AuditSigner | None = None,
    ) -> None:
        self._log_file = log_file
        self._aic = aic
        self._signer = signer

    def emit_sync(self, body: AuditBody, **kwargs: str | None) -> str:
        """构造并写入一条 AuditLogRecord，返回 log_id。

        可在同步上下文（非 asyncio 环境）中直接调用。
        """
        record = AuditLogRecord(
            log_id=AuditLogRecord.new_log_id(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            aic=self._aic,
            body=body,
            **kwargs,  # type: ignore[arg-type]
        )
        self._write_line(record)
        return record.log_id

    async def emit(self, body: AuditBody, **kwargs: str | None) -> str:
        """异步版 emit_sync，通过 asyncio.to_thread 避免阻塞事件循环。"""
        return await asyncio.to_thread(self.emit_sync, body, **kwargs)

    def _write_line(self, record: AuditLogRecord) -> None:
        """同步写入一行 JSON 到日志文件（写入失败只打 WARNING，不 raise）。

        签名流程（仅当 signer 已配置）：
          1. 将 record 序列化为 dict（不含 integrity，因其为 None）
          2. JCS（RFC 8785）规范化
          3. Ed25519 签名 → Base64url 编码（无填充）
          4. 将 integrity 字段注入 dict
          签名异常时降级为不含 integrity 写入，保证日志不丢失。
        """
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record_dict = record.model_dump(mode="json", by_alias=True, exclude_none=True)

            if self._signer is not None:
                try:
                    import jcs  # amp-sign extra

                    canonical: bytes = jcs.canonicalize(record_dict)
                    sig_bytes: bytes = self._signer.sign(canonical)
                    sig_b64: str = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()
                    record_dict["integrity"] = {
                        "alg": self._signer.alg,
                        "kid": self._signer.kid,
                        "sig": sig_b64,
                    }
                except Exception:
                    _logger.warning(
                        "AuditEmitter: 签名失败，记录将不含 integrity 字段",
                        exc_info=True,
                    )

            line = json.dumps(record_dict, ensure_ascii=False)
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _logger.warning("AuditEmitter: 写入失败，跳过", exc_info=True)
