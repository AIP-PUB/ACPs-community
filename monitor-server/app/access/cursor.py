"""app/access/cursor.py — keyset 游标编解码（§5.4）。

游标是「上一页最后一行排序键」的不透明编码（Base64(JSON) 内嵌查询指纹）。
翻页除 cursor 外参数/排序必须一致，否则 CursorInvalidError（C-ACCESS-QUERY-12）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.access.exception import CursorInvalidError

if TYPE_CHECKING:
    from app.access.filters import ResolvedSort
    from app.access.sql import KeysetBound


# ── 内部状态 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CursorState:
    """decode_cursor 解码产物；to_keyset_bound 将其转为 SQL 片段。"""

    sort_values: list[Any]
    tiebreak: dict[str, Any]
    fingerprint: str


# ── 公开函数 ──────────────────────────────────────────────────────────────────


def encode_cursor(*, sort_values: list[Any], tiebreak: dict[str, Any], fingerprint: str) -> str:
    """Base64(JSON({v, tb, fp})) 编码游标。

    事件级：sort_values=[排序值], tiebreak={"timestamp":..,"log_id":..}
    聚合级：tiebreak 为完整维度元组。
    """
    payload = {"v": sort_values, "tb": tiebreak, "fp": fingerprint}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(cursor: str | None, *, expected_fingerprint: str) -> CursorState | None:
    """解码游标；None → None；损坏/指纹不匹配 → raise CursorInvalidError(400)。"""
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor.encode())
        payload = json.loads(raw)
    except Exception as exc:
        raise CursorInvalidError("Cursor is corrupted or not valid base64/JSON") from exc
    if payload.get("fp") != expected_fingerprint:
        raise CursorInvalidError("Cursor fingerprint mismatch — query parameters may have changed")
    return CursorState(
        sort_values=payload.get("v", []),
        tiebreak=payload.get("tb", {}),
        fingerprint=payload["fp"],
    )


def query_fingerprint(
    *,
    api: str,
    time_range: Any,
    filter_: Any,
    sort: Any,
    extra: dict[str, Any],
) -> str:
    """稳定哈希（api + timeRange + filter + sort + extra）。

    翻页校验防参数漂移；SHA-256 截短为 16 字符十六进制。
    """
    canonical = json.dumps(
        {
            "api": api,
            "time_range": _serialize_any(time_range),
            "filter": _serialize_any(filter_),
            "sort": _serialize_any(sort),
            "extra": extra,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def to_keyset_bound(
    state: CursorState,
    sort: list[ResolvedSort],
    api: str,
) -> KeysetBound:
    """把游标状态转成 sql.KeysetBound WHERE 片段（禁 OFFSET，C-ACCESS-QUERY-12）。

    事件级（events/slow）→ (timestamp, log_id) < (?, ?)（降序）
    traces             → (last_seen_at, trace_id) < (?, ?)
    聚合级             → 排序字段 + 完整维度元组全序 keyset
    """
    from app.access.sql import KeysetBound

    params: dict[str, Any] = {}

    if api in ("events", "slow"):
        ts = state.tiebreak.get("timestamp", 0)
        lid = state.tiebreak.get("log_id", "")
        ts_ms = _iso_to_epoch_ms(ts)
        params = {"_ks_ts": ts_ms, "_ks_lid": lid}
        sql = (
            "AND (timestamp < fromUnixTimestamp64Milli({_ks_ts:Int64})"
            " OR (timestamp = fromUnixTimestamp64Milli({_ks_ts:Int64}) AND log_id < {_ks_lid:String}))"
        )
        return KeysetBound(sql=sql, params=params)

    if api == "traces":
        lsa = state.tiebreak.get("last_seen_at", 0)
        tid = state.tiebreak.get("trace_id", "")
        lsa_ms = _iso_to_epoch_ms(lsa)
        params = {"_ks_lsa": lsa_ms, "_ks_tid": tid}
        sql = (
            "AND (last_seen_at < fromUnixTimestamp64Milli({_ks_lsa:Int64})"
            " OR (last_seen_at = fromUnixTimestamp64Milli({_ks_lsa:Int64}) AND trace_id < {_ks_tid:String}))"
        )
        return KeysetBound(sql=sql, params=params)

    # 聚合级：使用第一个排序字段 + tiebreak 组合全序 keyset
    if sort:
        col = sort[0].column_or_alias
        order = sort[0].order
        cmp = "<" if order == "desc" else ">"
        val_key = "_ks_sort0"
        params[val_key] = state.sort_values[0] if state.sort_values else 0
        parts = [f"{col} {cmp} {{{val_key}:Int64}}"]
        for i, (k, v) in enumerate(state.tiebreak.items()):
            pk = f"_ks_tb{i}"
            params[pk] = v
            parts.append(f"{k} < {{{pk}:String}}")
        sql = "AND (" + " OR ".join(parts) + ")"
    else:
        sql = ""

    return KeysetBound(sql=sql, params=params)


# ── 私有辅助 ──────────────────────────────────────────────────────────────────


def _serialize_any(obj: Any) -> Any:
    """将任意对象序列化为可 JSON 编码的形式（Pydantic model → dict）。"""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (list, tuple)):
        return [_serialize_any(o) for o in obj]
    return str(obj)


def _iso_to_epoch_ms(ts: Any) -> int:
    """将 ISO 8601 时间戳字符串或 int 转换为 epoch 毫秒整数。

    游标中的 timestamp/last_seen_at 以 ISO 字符串形式存储，
    ClickHouse 参数化查询需要 Int64 epoch 毫秒（配合 fromUnixTimestamp64Milli()）。
    """
    if isinstance(ts, int):
        return ts
    if isinstance(ts, float):
        return int(ts)
    if not ts:
        return 0
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except ValueError, TypeError:
        return 0
