"""Audit 链哈希协议（纯函数，无 I/O）。

实现 AMP-API-Design-Audit.md §4.4 定义的两步哈希计算：
1. raw_log_hash = hex(sha256(JCS(raw_log)))
2. current_hash = hex(sha256(JCS(chain_preimage)))

关键约束（C-AUDIT-CHAIN-4）：
- timestamp_str 必须使用 raw_log 中的原始 ISO 8601 字符串，禁止用 Python datetime 格式化
- 链前像只包含不可变字段，chain_verified/anchor_id 等回写字段不得进入前像
- previous_hash = None 表示子链 genesis 记录，不能用空字符串混淆
"""

from __future__ import annotations

import hashlib
from typing import Any

import jcs


def compute_raw_log_hash(raw_log: dict[str, Any]) -> str:
    """对 LogRecord 原始 dict 做 JCS 规范化（RFC 8785），计算 SHA-256，返回 hex 字符串。

    注意：必须传入原始 JSON 反序列化的 dict，不能从 JSONB 回读（JSONB 会重排键）。

    Args:
        raw_log: AMP LogRecord 反序列化后的原始 dict。

    Returns:
        SHA-256 hex 摘要（小写，64 字符）。
    """
    canonical_bytes: bytes = jcs.canonicalize(raw_log)
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_current_hash(
    audit_id: str,
    log_id: str,
    timestamp_str: str,
    aic: str,
    chain_id: str,
    chain_seq: int,
    raw_log_hash: str,
    previous_hash: str | None,
) -> str:
    """构造版本化链前像对象，JCS 规范化后计算 SHA-256，返回 hex 字符串。

    链前像结构（hash_version = 1）：
    {
        "v": 1,
        "auditId": "...",
        "logId": "...",
        "timestamp": "2026-05-28T12:34:56.789Z",   ← 原始字符串，不做任何格式化
        "aic": "...",
        "chainId": "audit-chain-000",
        "chainSeq": 123,
        "rawLogHash": "...",
        "previousHash": "... or null"
    }

    Args:
        audit_id: 本条记录的 UUID（字符串形式）。
        log_id: 原始 LogRecord.log_id。
        timestamp_str: raw_log 中的原始 ISO 8601 时间戳字符串（禁止 datetime 格式化后的结果）。
        aic: Agent Instance Context 标识符。
        chain_id: 逻辑子链 ID，格式 "audit-chain-NNN"。
        chain_seq: 本条在子链中的序号（0 为创世记录）。
        raw_log_hash: 由 compute_raw_log_hash() 计算的原始日志哈希。
        previous_hash: 子链上一条记录的 current_hash，创世记录传 None。

    Returns:
        SHA-256 hex 摘要（小写，64 字符）。
    """
    preimage: dict[str, Any] = {
        "v": 1,
        "auditId": audit_id,
        "logId": log_id,
        "timestamp": timestamp_str,
        "aic": aic,
        "chainId": chain_id,
        "chainSeq": chain_seq,
        "rawLogHash": raw_log_hash,
        "previousHash": previous_hash,
    }
    canonical_bytes = jcs.canonicalize(preimage)
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_chain_id(aic: str, logical_chain_count: int) -> str:
    """根据 AIC 的稳定哈希取模，路由到对应逻辑子链。

    chain_id 格式：`"audit-chain-NNN"`（数字部分零填充到 logical_chain_count 最大索引位数）。

    Args:
        aic: Agent Instance Context 标识符。
        logical_chain_count: 子链总数（来自配置 audit.logical_chain_count）。

    Returns:
        子链 ID 字符串，如 "audit-chain-007"（256 条链时宽度为 3）。
    """
    # 使用 SHA-256 前 8 字节做稳定哈希（不依赖 Python hash() 的随机种子）
    digest = hashlib.sha256(aic.encode()).digest()
    index = int.from_bytes(digest[:8], "big") % logical_chain_count
    width = len(str(logical_chain_count - 1))
    return f"audit-chain-{index:0{width}d}"
