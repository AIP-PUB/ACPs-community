"""Heartbeat 模块 — 多 shard 游标编解码（§6.4 第 3 条）。

游标携带：
  - 各 shard 的当前扫描位置（last_seen_at_ms + last_aic 联合游标）
  - filter+sort 指纹（防止游标与查询参数错配，AMP_CURSOR_INVALID）
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from app.heartbeat.exception import CursorInvalidError
from app.heartbeat.schema import HeartbeatLivenessQueryRequest


@dataclass(frozen=True)
class ShardPosition:
    """单 shard 扫描位置（(last_seen_at_ms, aic) 联合游标，同 score 组内按 aic 续读）。"""

    last_seen_at_ms: int
    last_aic: str


@dataclass(frozen=True)
class LivenessQueryCursor:
    """多 shard 查询游标（§6.4 第 3 条）。"""

    positions: dict[str, ShardPosition]
    """shard id → 位置；缺失表示该 shard 已扫尽"""

    fingerprint: str
    """filter+sort 指纹，防游标与查询参数错配"""


def filter_fingerprint(request: HeartbeatLivenessQueryRequest) -> str:
    """计算 filter+sort 指纹（SHA-256 前 16 字节 hex），用于游标校验。

    Args:
        request: liveness query 请求体。

    Returns:
        16 字节（32 字符）hex 指纹字符串。
    """
    canonical = json.dumps(
        {
            "filter": request.filter.model_dump(mode="json") if request.filter else None,
            "sort": [s.model_dump(mode="json") for s in request.sort] if request.sort else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def encode_cursor(cursor: LivenessQueryCursor) -> str:
    """将游标编码为 URL-safe base64 字符串。

    Args:
        cursor: 游标数据。

    Returns:
        base64url 编码字符串（无填充）。
    """
    payload = {
        "positions": {
            shard: {"last_seen_at_ms": pos.last_seen_at_ms, "last_aic": pos.last_aic}
            for shard, pos in cursor.positions.items()
        },
        "fingerprint": cursor.fingerprint,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


def decode_cursor(raw: str, expected_fingerprint: str) -> LivenessQueryCursor:
    """从 base64 字符串解码游标，并校验指纹。

    Args:
        raw: base64url 编码字符串。
        expected_fingerprint: 当前查询参数计算出的指纹。

    Returns:
        解码后的游标。

    Raises:
        CursorInvalidError: 损坏的 base64、JSON 格式错误、指纹不匹配等。
    """
    try:
        # 补充 base64 padding（urlsafe_b64decode 需要正确的长度）
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        fp = data["fingerprint"]
        if fp != expected_fingerprint:
            raise CursorInvalidError()
        positions: dict[str, ShardPosition] = {}
        for shard, pos_data in data["positions"].items():
            positions[shard] = ShardPosition(
                last_seen_at_ms=int(pos_data["last_seen_at_ms"]),
                last_aic=str(pos_data["last_aic"]),
            )
        return LivenessQueryCursor(positions=positions, fingerprint=fp)
    except CursorInvalidError:
        raise
    except Exception as exc:
        raise CursorInvalidError() from exc
