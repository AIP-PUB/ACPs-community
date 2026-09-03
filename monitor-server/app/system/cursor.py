"""app/system/cursor.py — search_after + PIT 游标编解码（设计 §3.2 步骤 5）。

游标 = Base64URL(JSON{pit, after:[ts_ms, log_id], fp})。
翻页除 cursor 外参数/排序须一致（指纹防漂移）。
PIT 过期 → store 搜索抛 OpenSearchQueryError → service 转 CursorInvalidError(AMP_CURSOR_INVALID)。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.system.exception import CursorInvalidError


@dataclass(frozen=True)
class SystemCursorState:
    """解码后的游标状态。"""

    pit_id: str
    search_after: list[Any]  # [timestamp_ms, log_id]
    fingerprint: str


def encode_cursor(*, pit_id: str, search_after: list[Any], fingerprint: str) -> str:
    """编码游标为 Base64URL JSON 字符串。"""
    payload = {"pit": pit_id, "after": search_after, "fp": fingerprint}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str | None, *, expected_fingerprint: str) -> SystemCursorState | None:
    """解码游标；None → None（首页）；损坏/指纹不匹配 → CursorInvalidError(400)。"""
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        payload = json.loads(raw)
        pit_id = str(payload["pit"])
        search_after = list(payload["after"])
        fp = str(payload["fp"])
    except Exception as exc:
        raise CursorInvalidError("The pagination cursor is malformed or cannot be decoded.") from exc
    if fp != expected_fingerprint:
        raise CursorInvalidError("The pagination cursor fingerprint does not match. Query parameters may have changed.")
    return SystemCursorState(pit_id=pit_id, search_after=search_after, fingerprint=fp)


def query_fingerprint(
    *,
    time_range: Any,
    filter_: Any,
    sort: Any,
    keyword: str | None,
) -> str:
    """稳定哈希（timeRange + filter + sort + keyword）；SHA-256 截 16 hex。

    任一参数变化都会产生不同指纹，翻页时携带旧游标会被 decode_cursor 拒绝。
    """
    payload = {
        "time_range": _serialize(time_range),
        "filter": _serialize(filter_),
        "sort": _serialize(sort),
        "keyword": keyword,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest[:16]


def _serialize(obj: Any) -> Any:
    """将 pydantic model 序列化为可 JSON 化结构。"""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    return obj
