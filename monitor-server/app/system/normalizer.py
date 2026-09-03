"""app/system/normalizer.py — LogRecord(+自由格式 body) → 可索引文档（确定性，纯函数）。

C-SYSTEM-WRITE-2（severity 顶层优先）/-3（raw_body 必留）/-5（search_text 写时生成）/-7（message 恒存在）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from acps_sdk.amp.models import LogRecord

from app.system import indices
from app.system.exception import InvalidSystemRecordError

SEVERITY_UNSPECIFIED: Final = 0

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class SystemEventDoc:
    """规范化产物：一条待 Bulk Index 的 OpenSearch 文档。"""

    log_id: str
    index: str
    timestamp_ms: int
    source: dict[str, Any]

    def as_bulk_action(self, *, indexed_at_iso: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (action_meta, source)；indexed_at 写入时刻填充（设计 §2.4）。"""
        action_meta = {"index": {"_index": self.index, "_id": self.log_id}}
        source = {**self.source, "indexed_at": indexed_at_iso}
        return action_meta, source


def derive_message(body: Any, *, max_length: int) -> str:
    """C-SYSTEM-WRITE-7 确定性规则（设计 §3.1 步骤 3）。

    str → 直取（截 max_length）
    dict 含可读 message/msg → 取其值（str 化后截断）
    dict 其它 → sort_keys JSON 摘要（截 max_length）
    int/float/bool → str(value)
    list / None / 缺省 → ""
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body[:max_length]
    if isinstance(body, (int, float, bool)):
        return str(body)
    if isinstance(body, list):
        return ""
    if isinstance(body, dict):
        for key in ("message", "msg"):
            val = body.get(key)
            if isinstance(val, str):
                return val[:max_length]
        summary = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return summary[:max_length]
    return ""


def resolve_severity_number(record: LogRecord) -> int:
    """C-SYSTEM-WRITE-2：顶层 severity_number 非空直取；缺省 → SEVERITY_UNSPECIFIED(0)。"""
    if record.severity_number is not None:
        return record.severity_number
    return SEVERITY_UNSPECIFIED


def extract_structured_fields(body: Any) -> dict[str, str | None]:
    """body 是 dict 时提取 category/component/module（标量键）；非 dict → 全 None（设计 §3.1 步骤 4）。"""
    result: dict[str, str | None] = {"category": None, "component": None, "module": None}
    if not isinstance(body, dict):
        return result
    for field in ("category", "component", "module"):
        val = body.get(field)
        if (isinstance(val, (str, int, float, bool)) and not isinstance(val, bool)) or isinstance(val, str):
            if isinstance(val, str):
                result[field] = val
            else:
                result[field] = None
        elif val is None or not isinstance(val, (str,)):
            result[field] = None
    return result


def normalize_tags(body: Any, *, max_term_bytes: int = 32768) -> dict[str, str]:
    """body.tags 是 dict → 键值统一转字符串，value 截至 max_term_bytes（Lucene 上限）。

    超长截断但事件不丢（设计 §3.1 步骤 4 / §4.1）。
    """
    if not isinstance(body, dict):
        return {}
    tags_raw = body.get("tags")
    if not isinstance(tags_raw, dict):
        return {}
    result: dict[str, str] = {}
    for k, v in tags_raw.items():
        str_val = str(v)
        encoded = str_val.encode("utf-8")
        if len(encoded) > max_term_bytes:
            str_val = encoded[:max_term_bytes].decode("utf-8", errors="ignore")
        result[str(k)] = str_val
    return result


def build_search_text(
    *,
    message: str,
    body: Any,
    resource: dict[str, Any] | None,
    max_length: int,
) -> str:
    """C-SYSTEM-WRITE-5（设计 §3.1 步骤 5 / §2.3）：message + body 可读标量 + resource 标量。

    拼接、清洗（去控制符、折叠空白）、截 max_length。
    查询阶段禁止临时遍历 raw_body → 投影必须写时完成。
    """
    parts: list[str] = []
    if message:
        parts.append(message)
    if isinstance(body, dict):
        for v in _collect_scalar_values(body):
            parts.append(v)
    if resource:
        for v in resource.values():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                parts.append(str(v))
    combined = " ".join(parts)
    combined = _CONTROL_CHAR_RE.sub(" ", combined)
    combined = re.sub(r"\s+", " ", combined).strip()
    return combined[:max_length]


def _collect_scalar_values(d: dict[str, Any]) -> list[str]:
    """递归提取 dict 内所有可读标量字符串（str/int/float，排除 bool）。"""
    results: list[str] = []
    for v in d.values():
        if isinstance(v, str):
            results.append(v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            results.append(str(v))
        elif isinstance(v, dict):
            results.extend(_collect_scalar_values(v))
    return results


def build_document(
    record: LogRecord,
    *,
    log_id: str,
    search_text_max_length: int,
    tag_max_term_bytes: int = 32768,
) -> SystemEventDoc:
    """编排规范化步骤，产出 SystemEventDoc。

    timestamp 不可解析 → raise InvalidSystemRecordError（writer 据此投 DLQ）。
    """
    try:
        dt = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
        timestamp_ms = int(dt.timestamp() * 1000)
    except (ValueError, AttributeError) as exc:
        raise InvalidSystemRecordError(f"Cannot parse timestamp '{record.timestamp}': {exc}") from exc

    index = indices.index_for_timestamp(timestamp_ms)
    message = derive_message(record.body, max_length=search_text_max_length)
    severity_number = resolve_severity_number(record)
    structured = extract_structured_fields(record.body)
    tags = normalize_tags(record.body, max_term_bytes=tag_max_term_bytes)
    search_text = build_search_text(
        message=message,
        body=record.body,
        resource=record.resource,
        max_length=search_text_max_length,
    )

    source: dict[str, Any] = {
        indices.FIELD_LOG_ID: log_id,
        indices.FIELD_TIMESTAMP: record.timestamp,
        indices.FIELD_AIC: record.aic,
        indices.FIELD_TRACE_ID: record.trace_id,
        indices.FIELD_CORRELATION_ID: record.correlation_id,
        indices.FIELD_SEVERITY_NUMBER: severity_number,
        indices.FIELD_SEVERITY_TEXT: record.severity_text,
        indices.FIELD_MESSAGE: message,
        indices.FIELD_CATEGORY: structured["category"],
        indices.FIELD_COMPONENT: structured["component"],
        indices.FIELD_MODULE: structured["module"],
        indices.FIELD_TAGS: tags,
        indices.FIELD_SEARCH_TEXT: search_text,
        indices.FIELD_RAW_BODY: record.body,
    }

    return SystemEventDoc(
        log_id=log_id,
        index=index,
        timestamp_ms=timestamp_ms,
        source=source,
    )
