"""AMP LogRecord 顶层模型 — 从 acps-sdk 重新导出。

接收侧（Kafka Consumer、测试）直接导入本模块，无需关心 SDK 内部路径：
    from app.core.amp_schema import LogRecord, LogRecordIntegrity

关于 timestamp：保留为原始字符串，不做 datetime 转换，
因为链哈希（C-AUDIT-CHAIN-4）依赖字节级一致性。
"""

from acps_sdk.amp.models import LogRecord, LogRecordIntegrity

__all__ = ["LogRecord", "LogRecordIntegrity"]
