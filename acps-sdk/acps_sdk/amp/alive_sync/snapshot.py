"""AMP alive-sync NDJSON snapshot 解析（存储/传输无关，由 discovery 负责流式 HTTP）。

snapshot 格式：
  行 0：snapshot-meta（AliveSnapshotMeta）
  行 1+：kind="snapshot" op="upsert" 的 AliveDeltaEnvelope（存活 AIC 一行）
"""
from __future__ import annotations

import json

from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_TYPE,
    SNAPSHOT_META_RECORD_TYPE,
    AliveDeltaEnvelope,
    AliveSnapshotMeta,
    aic_from_object_id,
    seq_from_str,
)

from acps_sdk.amp.alive_sync.errors import SnapshotProtocolError
from acps_sdk.amp.alive_sync.store import AliveRecord


def parse_meta_line(raw: str | bytes) -> AliveSnapshotMeta:
    """解析 snapshot 首行（snapshot-meta）。

    Args:
        raw: NDJSON 首行文本或字节。

    Returns:
        AliveSnapshotMeta 实例。

    Raises:
        SnapshotProtocolError: JSON 解析失败、recordType 或 type 字段不符合契约。
    """
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except Exception as exc:
        raise SnapshotProtocolError(f"snapshot meta 行 JSON 解析失败: {exc}") from exc

    if data.get("recordType") != SNAPSHOT_META_RECORD_TYPE:
        raise SnapshotProtocolError(
            f"snapshot meta 行 recordType 非法: 期望 '{SNAPSHOT_META_RECORD_TYPE}'，"
            f"实际 '{data.get('recordType')}'"
        )
    if data.get("type") != ALIVE_DELTA_TYPE:
        raise SnapshotProtocolError(
            f"snapshot meta 行 type 非法: 期望 '{ALIVE_DELTA_TYPE}'，"
            f"实际 '{data.get('type')}'"
        )

    try:
        return AliveSnapshotMeta.model_validate(data)
    except Exception as exc:
        raise SnapshotProtocolError(f"snapshot meta 行字段校验失败: {exc}") from exc


def parse_snapshot_row(raw: str | bytes) -> AliveDeltaEnvelope:
    """解析 snapshot 数据行（kind="snapshot" op="upsert"）。

    Args:
        raw: NDJSON 数据行文本或字节。

    Returns:
        AliveDeltaEnvelope 实例。

    Raises:
        SnapshotProtocolError: JSON 解析失败、kind/op/payload 不符合契约。
    """
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except Exception as exc:
        raise SnapshotProtocolError(f"snapshot 数据行 JSON 解析失败: {exc}") from exc

    if data.get("kind") != "snapshot":
        raise SnapshotProtocolError(
            f"snapshot 数据行 kind 非法: 期望 'snapshot'，实际 '{data.get('kind')}'"
        )
    if data.get("op") != "upsert":
        raise SnapshotProtocolError(
            f"snapshot 数据行 op 非法: 期望 'upsert'，实际 '{data.get('op')}'"
        )
    if not data.get("payload"):
        raise SnapshotProtocolError("snapshot 数据行 payload 为空")

    try:
        return AliveDeltaEnvelope.model_validate(data)
    except Exception as exc:
        raise SnapshotProtocolError(f"snapshot 数据行字段校验失败: {exc}") from exc


def snapshot_row_to_record(env: AliveDeltaEnvelope) -> AliveRecord:
    """将 snapshot upsert 行转换为存储 AliveRecord。

    Args:
        env: 已通过 parse_snapshot_row 校验的 AliveDeltaEnvelope。

    Returns:
        AliveRecord（alive=True，version=seq_from_str(env.version)）。
    """
    return AliveRecord(
        aic=aic_from_object_id(env.id),
        alive=True,
        last_seen_at=env.payload.last_seen_at if env.payload else None,
        version=seq_from_str(env.version),
        shard=env.shard,
    )
