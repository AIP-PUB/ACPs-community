"""app/metrics/cursor.py — 快照索引游标（snapshots/query）。

实现设计 §4.3「amp:metrics:snapshot:index 稳定顺序 (observedAt desc, aic asc)，
cursor 必须按此顺序编解码」及 spec §6.1.2 规则 5（指纹防错配）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.amp_api_schema import AMPFilter
from app.metrics.exception import CursorInvalidError


@dataclass(frozen=True)
class SnapshotCursor:
    """snapshots/query 分页游标（§6.1.2 规则 5，§4.3）。"""

    observed_at_ms: int
    """上一页末项的 observedAt 毫秒时间戳。"""

    aic: str
    """上一页末项的 aic（同 score 组内续读用）。"""

    fingerprint: str
    """filter 指纹，防游标与查询参数错配（spec §6.1.2 规则 5）。"""


def filter_fingerprint(filter_: AMPFilter | None, windows: list[str] | None) -> str:
    """计算 filter + windows 的 SHA-256 指纹（前 16 字节十六进制）。

    canonical-json：key 排序、紧凑序列化，保证相同语义输入产出相同指纹。

    Args:
        filter_: AMPFilter 实例或 None。
        windows: 窗口列表或 None。

    Returns:
        str: 16 字符十六进制指纹。
    """
    canonical: dict[str, Any] = {
        "filter": filter_.model_dump(mode="json", by_alias=False) if filter_ is not None else None,
        "windows": sorted(windows) if windows else None,
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def encode_cursor(cursor: SnapshotCursor) -> str:
    """序列化 SnapshotCursor 为 URL 安全 base64 字符串。

    Args:
        cursor: SnapshotCursor 实例。

    Returns:
        str: URL 安全 base64 字符串（无填充）。
    """
    payload = json.dumps(
        {
            "observed_at_ms": cursor.observed_at_ms,
            "aic": cursor.aic,
            "fingerprint": cursor.fingerprint,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def decode_cursor(raw: str, expected_fingerprint: str) -> SnapshotCursor:
    """反序列化游标并验证 filter 指纹（spec §6.1.2 规则 5）。

    Args:
        raw: encode_cursor 产出的字符串。
        expected_fingerprint: 当前请求参数产出的指纹（filter_fingerprint 的返回值）。

    Returns:
        SnapshotCursor

    Raises:
        CursorInvalidError: base64 解码失败、JSON 格式错误、字段缺失，或指纹不匹配（400）。
    """
    try:
        # 补全 base64 填充
        padding = 4 - len(raw) % 4
        padded = raw + "=" * (padding % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        data = json.loads(decoded)
        observed_at_ms = int(data["observed_at_ms"])
        aic = str(data["aic"])
        fingerprint = str(data["fingerprint"])
    except Exception as exc:
        raise CursorInvalidError("Cursor is malformed or cannot be decoded.") from exc

    if fingerprint != expected_fingerprint:
        raise CursorInvalidError("Cursor does not match current query parameters (filter/windows changed).")

    return SnapshotCursor(observed_at_ms=observed_at_ms, aic=aic, fingerprint=fingerprint)


__all__ = [
    "SnapshotCursor",
    "decode_cursor",
    "encode_cursor",
    "filter_fingerprint",
]
