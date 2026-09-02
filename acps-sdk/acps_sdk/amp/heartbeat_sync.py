"""AMP Heartbeat alive-delta 同步子协议线缆模型（spec §6.2.1）与编码约定（设计 §4.1、§4.3）。

Provider 写 Kafka/NDJSON、Consumer 读，双方共享本模块。
命名风格：Python snake_case + Field(alias="camelCase") + populate_by_name=True。
序列化：model_dump(mode="json", by_alias=True, exclude_none=True)。
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── 常量 ──────────────────────────────────────────────────────────────────────

ALIVE_DELTA_TYPE: Final = "amp-alive-delta"
ALIVE_DELTA_SCHEMA_VERSION: Final = "1"
SNAPSHOT_META_RECORD_TYPE: Final = "snapshot-meta"
SNAPSHOT_CONTENT_TYPE: Final = "application/x-ndjson"
ALIVE_OBJECT_ID_PREFIX: Final = "urn:amp:alive:"
HEARTBEAT_TOPIC_DEFAULT: Final = "amp.heartbeat"
ALIVE_DELTA_TOPIC_DEFAULT: Final = "amp.heartbeat.alive-delta"

# ── 类型别名 ──────────────────────────────────────────────────────────────────

DeltaOp = Literal["upsert", "delete"]
DeltaKind = Literal["snapshot", "enter_alive", "refresh_alive", "leave_alive"]

# ── 编码工具（线缆两侧必须一致，故入 SDK） ────────────────────────────────────


def alive_object_id(aic: str) -> str:
    """将 AIC 编码为 alive object id（§7.1）。

    Args:
        aic: Agent Identity Code。

    Returns:
        "urn:amp:alive:<aic>" 格式的 object id。
    """
    return f"{ALIVE_OBJECT_ID_PREFIX}{aic}"


def aic_from_object_id(object_id: str) -> str:
    """从 alive object id 反解 AIC。

    Args:
        object_id: "urn:amp:alive:<aic>" 格式的字符串。

    Returns:
        AIC 字符串。

    Raises:
        ValueError: 前缀不匹配。
    """
    if not object_id.startswith(ALIVE_OBJECT_ID_PREFIX):
        raise ValueError(f"object_id 前缀不匹配，期望 '{ALIVE_OBJECT_ID_PREFIX}'，实际 '{object_id}'")
    return object_id[len(ALIVE_OBJECT_ID_PREFIX):]


def shard_id(shard_index: int, shard_count: int) -> str:
    """将分片索引编码为线缆 shard id 字符串（§4.1）。

    宽度 = max(3, len(str(shard_count - 1)))，即 ≤1000 分片固定 3 位（hb-000），
    超过自动加宽（如 shard_count=10000 → 4 位 hb-0000）。

    Args:
        shard_index: 分片索引（0-based）。
        shard_count: 总分片数。

    Returns:
        "hb-NNN" 格式的 shard id。
    """
    width = max(3, len(str(shard_count - 1)))
    return f"hb-{shard_index:0{width}d}"


def shard_index_from_id(shard: str) -> int:
    """从 shard id 字符串反解分片索引。

    Args:
        shard: "hb-NNN" 格式的 shard id。

    Returns:
        分片索引整数。

    Raises:
        ValueError: 格式非法。
    """
    if not shard.startswith("hb-"):
        raise ValueError(f"shard id 格式非法，期望 'hb-NNN'，实际 '{shard}'")
    numeric_part = shard[3:]
    if not numeric_part.isdigit():
        raise ValueError(f"shard id 数字部分非法：'{numeric_part}'")
    return int(numeric_part)


def seq_to_str(seq: int) -> str:
    """将 seq 数值编码为线缆字符串（十进制整数字符串，§4.3）。

    Args:
        seq: 序列号整数。

    Returns:
        十进制整数字符串。
    """
    return str(seq)


def seq_from_str(value: str) -> int:
    """将线缆 seq 字符串解码为数值。

    跨进程 seq 比较必须经由本函数解码后按数值比较，禁止字符串字典序（§4.3、C-SYNC-3）。

    Args:
        value: 十进制整数字符串。

    Returns:
        整数 seq。

    Raises:
        ValueError: 非数字、负数或空串。
    """
    if not value:
        raise ValueError("seq 字符串不能为空")
    if not value.lstrip("-").isdigit():
        raise ValueError(f"seq 字符串非整数：'{value}'")
    result = int(value)
    if result < 0:
        raise ValueError(f"seq 不能为负数：{result}")
    return result


# ── Pydantic 模型 ─────────────────────────────────────────────────────────────


class AliveSetEntry(BaseModel):
    """单个 alive AIC 的 Sync Profile 线缆表示（spec §6.2.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    aic: str
    last_seen_at: str = Field(alias="lastSeenAt", description="ISO 8601 UTC 时间戳")
    source_timestamp: str | None = Field(default=None, alias="sourceTimestamp")


class AliveDeltaEnvelope(BaseModel):
    """alive-delta Kafka 消息信封（spec §6.2.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    shard: str
    seq: str = Field(description="十进制整数字符串，数值比较用 seq_from_str()")
    type: Literal["amp-alive-delta"]
    id: str = Field(description="urn:amp:alive:<aic>")
    version: str = Field(description="与 seq 同值")
    op: DeltaOp
    kind: DeltaKind
    payload: AliveSetEntry | None = Field(default=None, description="op=delete 可省略")


class HeartbeatSyncInfo(BaseModel):
    """alive-delta Sync Profile 元数据端点响应（/sync/info，spec §6.2.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["amp-alive-delta"]
    schema_version: str = Field(alias="schemaVersion")
    snapshot_content_type: Literal["application/x-ndjson"] = Field(alias="snapshotContentType")
    kafka_topic: str = Field(alias="kafkaTopic")
    shard_count: int = Field(alias="shardCount")
    refresh_emit_interval_seconds: int = Field(alias="refreshEmitIntervalSeconds")
    delta_retention_hours: int = Field(alias="deltaRetentionHours")
    current_published_seq_by_shard: dict[str, str] = Field(
        alias="currentPublishedSeqByShard",
        description="shard id → seq 字符串；Consumer 用 seq_from_str() 按数值比较",
    )


class AliveSnapshotMeta(BaseModel):
    """全量快照 NDJSON 首行元数据（snapshot-meta，spec §6.2.1）。"""

    model_config = ConfigDict(populate_by_name=True)

    record_type: Literal["snapshot-meta"] = Field(alias="recordType")
    type: Literal["amp-alive-delta"]
    cutover_seq_by_shard: dict[str, str] = Field(
        alias="cutoverSeqByShard",
        description="Consumer 从此 seq 之后订阅 delta，shard id → seq 字符串",
    )
    generated_at: str = Field(alias="generatedAt", description="ISO 8601 UTC 快照生成时间")
