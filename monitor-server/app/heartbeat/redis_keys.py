"""Heartbeat 模块 — Redis Key 构造器（纯函数，§4.2 唯一落点）。

所有带 shard 参数的 key 必须包含 {hb-N} hash tag（C-SHARD-1）。
WATERMARKS_KEY 是跨 shard 聚合键，无 hash tag。
"""

from __future__ import annotations

from typing import Final

# 非分片键：无 hash tag（跨 shard 聚合用，§4.2）
WATERMARKS_KEY: Final = "amp:hb:writer_watermarks"


def latest_key(shard: str, aic: str) -> str:
    """AIC 最新心跳状态 Hash key。

    Args:
        shard: 分片 id，如 "hb-000"。
        aic: Agent Identity Code。

    Returns:
        "amp:hb:{hb-N}:latest:<aic>"
    """
    return f"amp:hb:{{{shard}}}:latest:{aic}"


def liveness_zset_key(shard: str) -> str:
    """AIC liveness ZSet key（score = last_seen_at_ms）。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:liveness_zset"
    """
    return f"amp:hb:{{{shard}}}:liveness_zset"


def delta_seq_key(shard: str) -> str:
    """Delta 序列号计数器 key。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:delta_seq"
    """
    return f"amp:hb:{{{shard}}}:delta_seq"


def delta_outbox_key(shard: str) -> str:
    """Delta outbox Redis Stream key。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:delta_outbox"
    """
    return f"amp:hb:{{{shard}}}:delta_outbox"


def published_seq_key(shard: str) -> str:
    """Relay 已发布最大 seq key。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:published_seq"
    """
    return f"amp:hb:{{{shard}}}:published_seq"


def scan_lock_key(shard: str) -> str:
    """Reconciler 扫描锁 key（防止多实例并发扫描）。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:scan_lock"
    """
    return f"amp:hb:{{{shard}}}:scan_lock"


def relay_lock_key(shard: str) -> str:
    """Relay 实例锁 key（epoch fencing 方案 b，§5.4）。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:relay_lock"
    """
    return f"amp:hb:{{{shard}}}:relay_lock"


def relay_epoch_key(shard: str) -> str:
    """Relay epoch 令牌 key（epoch fencing 方案 b，§5.4）。

    Args:
        shard: 分片 id。

    Returns:
        "amp:hb:{hb-N}:relay_epoch"
    """
    return f"amp:hb:{{{shard}}}:relay_epoch"
