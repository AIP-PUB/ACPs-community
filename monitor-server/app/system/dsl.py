"""app/system/dsl.py — SystemEventQueryRequest → OpenSearch DSL bool 查询（纯函数）。

设计 §3.2 步骤 3/4、§5.2/§5.3 / spec §6.7.2/§6.7.4。
字段路径来自白名单常量；值经 DSL 结构承载（参数无注入面）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from app.system.exception import (
    InvalidFilterError,
    UnsupportedFieldError,
    UnsupportedOperatorError,
)

if TYPE_CHECKING:
    from app.core.amp_api_schema import AMPFilter
    from app.system.planner import ResolvedSort


@dataclass(frozen=True)
class FieldSpec:
    """字段元数据：文档字段名 + 字段类型。"""

    doc_field: str
    kind: str  # "keyword" | "message" | "numeric" | "tags"


# 稳定可过滤字段白名单（spec §6.7.2 + 设计 §3.2 步骤 3）
EVENT_FILTER_FIELDS: Final[dict[str, FieldSpec]] = {
    "logId": FieldSpec("log_id", "keyword"),
    "aic": FieldSpec("aic", "keyword"),
    "traceId": FieldSpec("trace_id", "keyword"),
    "correlationId": FieldSpec("correlation_id", "keyword"),
    "category": FieldSpec("category", "keyword"),
    "component": FieldSpec("component", "keyword"),
    "module": FieldSpec("module", "keyword"),
    "severityText": FieldSpec("severity_text", "keyword"),
    "severityNumber": FieldSpec("severity_number", "numeric"),
    "message": FieldSpec("message.keyword", "message"),
}

MAX_IN_SET_SIZE: Final = 256


def _resolve_field(field_path: str) -> FieldSpec:
    """精确命中白名单 → 返回；tags.* 前缀 → tags；rawBody 深层 → UnsupportedFieldError。"""
    # rawBody 深层路径拒绝（C-SYSTEM-QUERY-3）
    if field_path.startswith(("rawBody.", "raw_body.")):
        raise UnsupportedFieldError(field_path)
    # 精确匹配白名单
    if field_path in EVENT_FILTER_FIELDS:
        return EVENT_FILTER_FIELDS[field_path]
    # tags.* 动态匹配
    if field_path.startswith("tags."):
        sub = field_path[len("tags.") :]
        return FieldSpec(f"tags.{sub}", "tags")
    raise UnsupportedFieldError(field_path)


def _compile_keyword_field(spec: FieldSpec, op: str, value: Any) -> dict[str, Any]:
    """keyword 类字段算子编译（aic/traceId/category/severityText 等）。

    eq/ne → term(case_insensitive=true)；in/nin → bool.should 展开多 term（非 terms，§3.2 第1条）
    eqCs/neCs/inCs/ninCs → 普通 term/terms（大小写敏感）。
    """
    doc_field = spec.doc_field
    if op == "eq":
        return {"term": {doc_field: {"value": value, "case_insensitive": True}}}
    if op == "ne":
        return {"bool": {"must_not": {"term": {doc_field: {"value": value, "case_insensitive": True}}}}}
    if op == "in":
        values = _validate_set(value, op)
        return {
            "bool": {
                "should": [{"term": {doc_field: {"value": v, "case_insensitive": True}}} for v in values],
                "minimum_should_match": 1,
            }
        }
    if op == "nin":
        values = _validate_set(value, op)
        return {
            "bool": {
                "must_not": {
                    "bool": {
                        "should": [{"term": {doc_field: {"value": v, "case_insensitive": True}}} for v in values],
                        "minimum_should_match": 1,
                    }
                }
            }
        }
    if op == "eqCs":
        return {"term": {doc_field: value}}
    if op == "neCs":
        return {"bool": {"must_not": {"term": {doc_field: value}}}}
    if op == "inCs":
        values = _validate_set(value, op)
        return {"terms": {doc_field: values}}
    if op == "ninCs":
        values = _validate_set(value, op)
        return {"bool": {"must_not": {"terms": {doc_field: values}}}}
    if op == "exists":
        return {"exists": {"field": doc_field}}
    raise UnsupportedOperatorError(op, doc_field)


def _compile_message_field(op: str, value: Any) -> dict[str, Any]:
    """message 过滤作用在 message.keyword（ignore_above:256，设计 §5.3 第 4 条）。

    仅精确算子：eq/eqCs/in/inCs。contains/startsWith/endsWith/exists → UnsupportedOperatorError。
    """
    doc_field = "message.keyword"
    if op == "eq":
        return {"term": {doc_field: {"value": value, "case_insensitive": True}}}
    if op == "eqCs":
        return {"term": {doc_field: value}}
    if op == "in":
        values = _validate_set(value, op)
        return {"bool": {"should": [{"term": {doc_field: {"value": v, "case_insensitive": True}}} for v in values]}}
    if op == "inCs":
        values = _validate_set(value, op)
        return {"terms": {doc_field: values}}
    # 子串/前缀/后缀/exists 不支持（全文检索走 keyword 参数）
    raise UnsupportedOperatorError(op, "message")


def _compile_numeric_field(spec: FieldSpec, op: str, value: Any) -> dict[str, Any]:
    """severityNumber 数值字段算子。"""
    doc_field = spec.doc_field
    if op == "eq":
        return {"term": {doc_field: value}}
    if op == "ne":
        return {"bool": {"must_not": {"term": {doc_field: value}}}}
    if op == "gt":
        return {"range": {doc_field: {"gt": value}}}
    if op == "gte":
        return {"range": {doc_field: {"gte": value}}}
    if op == "lt":
        return {"range": {doc_field: {"lt": value}}}
    if op == "lte":
        return {"range": {doc_field: {"lte": value}}}
    if op == "between":
        return {"range": {doc_field: {"gte": value[0], "lte": value[1]}}}
    if op == "in":
        values = _validate_set(value, op)
        return {"terms": {doc_field: values}}
    if op == "nin":
        values = _validate_set(value, op)
        return {"bool": {"must_not": {"terms": {doc_field: values}}}}
    raise UnsupportedOperatorError(op, doc_field)


def _compile_tags_field(spec: FieldSpec, op: str, value: Any) -> dict[str, Any]:
    """tags.* flat_object（设计 §5.3 第 3 条 / §4.1）：仅大小写敏感精确算子。

    eqCs/neCs/inCs/ninCs → 支持。eq/ne/in/nin（case_insensitive）/ exists → UnsupportedOperatorError。
    """
    doc_field = spec.doc_field
    if op == "eqCs":
        return {"term": {doc_field: value}}
    if op == "neCs":
        return {"bool": {"must_not": {"term": {doc_field: value}}}}
    if op == "inCs":
        return {"terms": {doc_field: list(value)}}
    if op == "ninCs":
        return {"bool": {"must_not": {"terms": {doc_field: list(value)}}}}
    # eq/ne/in/nin（含 case_insensitive 变体）和 exists 均不支持
    raise UnsupportedOperatorError(op, doc_field)


def compile_filter(filter_: AMPFilter | None) -> list[dict[str, Any]]:
    """单层 logic='and' 过滤器 → OpenSearch filter 子句列表。

    logic != 'and' → InvalidFilterError（§12 O-2，首版只支持单层 and）。
    """
    if filter_ is None:
        return []
    if filter_.logic != "and":
        raise InvalidFilterError(f"Only 'and' logic is supported. Got: '{filter_.logic}'")
    if not filter_.conditions:
        return []
    clauses: list[dict[str, Any]] = []
    for cond in filter_.conditions:
        spec = _resolve_field(cond.field)
        clause: dict[str, Any]
        if spec.kind == "keyword":
            clause = _compile_keyword_field(spec, cond.op, cond.value)
        elif spec.kind == "message":
            clause = _compile_message_field(cond.op, cond.value)
        elif spec.kind == "numeric":
            clause = _compile_numeric_field(spec, cond.op, cond.value)
        elif spec.kind == "tags":
            clause = _compile_tags_field(spec, cond.op, cond.value)
        else:
            raise UnsupportedFieldError(cond.field)
        clauses.append(clause)
    return clauses


def build_keyword_query(keyword: str | None) -> dict[str, Any] | None:
    """C-SYSTEM-QUERY-2：keyword → multi_match on message + search_text；None → None。"""
    if keyword is None:
        return None
    return {
        "multi_match": {
            "query": keyword,
            "fields": ["message", "search_text"],
        }
    }


def build_time_range_clause(*, from_ms: int, to_ms: int) -> dict[str, Any]:
    """timeRange 左闭右开 → range: {timestamp: {gte: from_iso, lt: to_iso}}。"""
    from_iso = datetime.fromtimestamp(from_ms / 1000.0, tz=UTC).isoformat()
    to_iso = datetime.fromtimestamp(to_ms / 1000.0, tz=UTC).isoformat()
    return {"range": {"timestamp": {"gte": from_iso, "lt": to_iso}}}


def build_sort(resolved_sort: list[ResolvedSort]) -> list[dict[str, Any]]:
    """排序数组 + 末尾追加 log_id 唯一 tiebreaker(asc)（C-SYSTEM-QUERY-5）。"""
    sort_list: list[dict[str, Any]] = []
    for rs in resolved_sort:
        sort_list.append({rs.doc_field: {"order": rs.order}})
    sort_list.append({"log_id": {"order": "asc"}})
    return sort_list


def build_search_body(
    *,
    filter_clauses: list[dict[str, Any]],
    keyword_query: dict[str, Any] | None,
    time_clause: dict[str, Any],
    scope_clauses: list[dict[str, Any]],
    sort: list[dict[str, Any]],
    search_after: list[Any] | None,
    size: int,
) -> dict[str, Any]:
    """组装最终 query body（PIT 在 store 注入）。

    bool.must: [keyword_query?]
    bool.filter: [time_clause, *filter_clauses, *scope_clauses]
    """
    must: list[dict[str, Any]] = []
    if keyword_query is not None:
        must.append(keyword_query)

    filter_list: list[dict[str, Any]] = [time_clause, *filter_clauses, *scope_clauses]

    body: dict[str, Any] = {
        "query": {
            "bool": {
                "must": must,
                "filter": filter_list,
            }
        },
        "sort": sort,
        "size": size,
    }
    if search_after is not None:
        body["search_after"] = search_after
    return body


def _validate_set(value: Any, op: str) -> list[Any]:
    """验证 in/nin 集合大小 ≤ MAX_IN_SET_SIZE；超出 → InvalidFilterError。"""
    if not isinstance(value, (list, tuple)):
        raise InvalidFilterError(f"Operator '{op}' requires a list value.")
    if len(value) > MAX_IN_SET_SIZE:
        raise InvalidFilterError(f"Operator '{op}' set size {len(value)} exceeds maximum {MAX_IN_SET_SIZE}.")
    return list(value)
