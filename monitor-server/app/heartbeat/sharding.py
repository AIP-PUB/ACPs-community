"""Heartbeat 模块 — Redis 分片哈希与 Kafka 输入分区反推（纯函数，C-SHARD-2/4）。

注意：shard_index_for_aic（Redis 分片）与 input_partition_for_aic（Kafka 输入分区）
是两套独立维度，分别有独立函数，不得互相替代。
"""

from __future__ import annotations

import hashlib
import struct

from acps_sdk.amp.heartbeat_sync import shard_id as sdk_shard_id

from app.core.config import settings


def stable_shard_hash(aic: str) -> int:
    """计算 AIC 的稳定哈希值（C-SHARD-2 全路径唯一实现点）。

    使用 SHA-256 前 8 字节大端整数，确保写入、查询、reconciler、relay、snapshot
    五条路径用同一哈希函数。

    Args:
        aic: Agent Identity Code。

    Returns:
        64 位无符号整数哈希值。
    """
    digest = hashlib.sha256(aic.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def shard_index_for_aic(aic: str, shard_count: int) -> int:
    """计算 AIC 对应的 Redis 分片索引（C-SHARD-2）。

    Args:
        aic: Agent Identity Code。
        shard_count: 总分片数。

    Returns:
        分片索引（0-based）。
    """
    return stable_shard_hash(aic) % shard_count


def shard_id_for_aic(aic: str) -> str:
    """计算 AIC 对应的 Redis 分片 id 字符串（C-SHARD-2）。

    复用 SDK 的 shard_id() 编码，使 Provider 与 Consumer 字符串格式一致。

    Args:
        aic: Agent Identity Code。

    Returns:
        "hb-NNN" 格式的 shard id。
    """
    count = settings.heartbeat_heartbeat_shard_count
    idx = shard_index_for_aic(aic, count)
    return sdk_shard_id(idx, count)


def all_shard_ids() -> list[str]:
    """返回全部分片 id 列表（按 settings.heartbeat_heartbeat_shard_count）。

    Returns:
        ["hb-000", ...] 按 shard 索引升序。
    """
    count = settings.heartbeat_heartbeat_shard_count
    return [sdk_shard_id(i, count) for i in range(count)]


def murmur2_kafka(data: bytes) -> int:
    """逐位复刻 Kafka Java 客户端 org.apache.kafka.common.utils.Utils.murmur2。

    Seed 0x9747b28c，含符号溢出语义（Java int 32 位有符号）。
    用于 input_partition_for_aic 的分区计算（C-SHARD-4）。

    Args:
        data: 待哈希字节串。

    Returns:
        32 位有符号整数（Python 中保持 int 范围）。
    """
    seed = 0x9747B28C
    # Kafka murmur2 常量
    m = 0x5BD1E995
    r = 24

    length = len(data)
    h = seed ^ length

    offset = 0
    while length >= 4:
        # 读取 4 字节小端 unsigned int
        (k,) = struct.unpack_from("<I", data, offset)
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF

        h = (h * m) & 0xFFFFFFFF
        h ^= k

        offset += 4
        length -= 4

    # 处理剩余字节
    remaining = len(data) - offset
    if remaining == 3:
        h ^= (data[offset + 2] & 0xFF) << 16
        h ^= (data[offset + 1] & 0xFF) << 8
        h ^= data[offset] & 0xFF
        h = (h * m) & 0xFFFFFFFF
    elif remaining == 2:
        h ^= (data[offset + 1] & 0xFF) << 8
        h ^= data[offset] & 0xFF
        h = (h * m) & 0xFFFFFFFF
    elif remaining == 1:
        h ^= data[offset] & 0xFF
        h = (h * m) & 0xFFFFFFFF

    # 最终混合
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15

    # 转换为 Java int（有符号 32 位）
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def input_partition_for_aic(aic: str, partition_count: int) -> int:
    """计算 AIC 在 Kafka amp.heartbeat topic 中的分区号（C-SHARD-4）。

    复刻 Kafka DefaultPartitioner：toPositive(murmur2(key)) % numPartitions。
    其中 toPositive = & 0x7FFFFFFF（消除符号位）。

    Args:
        aic: Agent Identity Code（作为 Kafka 消息 key）。
        partition_count: amp.heartbeat topic 分区数。

    Returns:
        Kafka 分区号（0-based）。
    """
    h = murmur2_kafka(aic.encode("utf-8"))
    return (h & 0x7FFFFFFF) % partition_count
