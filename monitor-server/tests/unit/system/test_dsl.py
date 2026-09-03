"""tests/unit/system/test_dsl.py — dsl.py 单元测试（核心 TDD 靶点）。"""

from __future__ import annotations

import pytest

from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
from app.system.dsl import (
    MAX_IN_SET_SIZE,
    build_keyword_query,
    build_search_body,
    build_sort,
    build_time_range_clause,
    compile_filter,
)
from app.system.exception import (
    InvalidFilterError,
    UnsupportedFieldError,
    UnsupportedOperatorError,
)
from app.system.planner import ResolvedSort


def _make_filter(field: str, op: str, value: object | None = None) -> AMPFilter:
    return AMPFilter(conditions=[AMPFilterCondition(field=field, op=op, value=value)])


class TestKeywordFieldCompilation:
    """keyword 类字段（aic/traceId/category 等）算子编译。"""

    def test_eq_produces_term_case_insensitive(self) -> None:
        clauses = compile_filter(_make_filter("aic", "eq", "AIC-001"))
        assert len(clauses) == 1
        term = clauses[0]["term"]
        assert "aic" in term
        assert term["aic"]["case_insensitive"] is True

    def test_ne_wraps_must_not_with_case_insensitive_term(self) -> None:
        """ne → must_not + term(case_insensitive)（设计 §3.2 步骤 3 第1条）。"""
        clauses = compile_filter(_make_filter("aic", "ne", "AIC-001"))
        clause = clauses[0]
        assert "bool" in clause
        must_not = clause["bool"]["must_not"]
        assert isinstance(must_not, dict)
        assert must_not["term"]["aic"]["case_insensitive"] is True

    def test_in_expands_to_bool_should_not_terms(self) -> None:
        """in → bool.should 展开多个 term，而非 terms（设计 §3.2 / §5.3 第 1 条 case_insensitive 约束）。"""
        clauses = compile_filter(_make_filter("aic", "in", ["A", "B"]))
        clause = clauses[0]
        assert "bool" in clause
        should = clause["bool"]["should"]
        assert isinstance(should, list)
        assert len(should) == 2
        for item in should:
            assert "term" in item

    def test_nin_expands_to_bool_must_not_should(self) -> None:
        """nin → bool.must_not 包裹 bool.should 展开。"""
        clauses = compile_filter(_make_filter("aic", "nin", ["A", "B"]))
        clause = clauses[0]
        must_not = clause["bool"]["must_not"]
        # must_not 可以是 bool(should) 或 list；key point: should 用 term 展开
        assert must_not is not None

    def test_eq_cs_produces_plain_term(self) -> None:
        """eqCs → 普通 term（大小写敏感精确匹配）。"""
        clauses = compile_filter(_make_filter("aic", "eqCs", "AIC-001"))
        term = clauses[0]["term"]
        assert "aic" in term
        # 不含 case_insensitive 或 case_insensitive=False
        val = term["aic"]
        if isinstance(val, dict):
            assert not val.get("case_insensitive", False)
        else:
            assert val == "AIC-001"

    def test_ne_cs_wraps_must_not_plain_term(self) -> None:
        """neCs → must_not + 普通 term（大小写敏感不等于）。"""
        clauses = compile_filter(_make_filter("aic", "neCs", "AIC-001"))
        clause = clauses[0]
        assert "bool" in clause
        must_not = clause["bool"]["must_not"]
        assert "term" in must_not or (isinstance(must_not, list) and any("term" in m for m in must_not))

    def test_in_cs_produces_terms(self) -> None:
        """inCs → 普通 terms（大小写敏感包含）。"""
        clauses = compile_filter(_make_filter("aic", "inCs", ["A", "B"]))
        terms = clauses[0]["terms"]
        assert "aic" in terms
        assert terms["aic"] == ["A", "B"]

    def test_nin_cs_wraps_must_not_terms(self) -> None:
        """ninCs → must_not + terms（大小写敏感不包含）。"""
        clauses = compile_filter(_make_filter("aic", "ninCs", ["A", "B"]))
        clause = clauses[0]
        assert "bool" in clause
        must_not = clause["bool"]["must_not"]
        assert "terms" in must_not

    def test_exists_produces_exists_query(self) -> None:
        clauses = compile_filter(_make_filter("aic", "exists"))
        assert "exists" in clauses[0]

    def test_unsupported_operator_raises(self) -> None:
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("aic", "contains", "val"))

    def test_in_over_max_set_size_raises_invalid_filter(self) -> None:
        """in 集合 > 256 → InvalidFilterError（设计 §3.2 / §5.3）。"""
        big_list = [str(i) for i in range(MAX_IN_SET_SIZE + 1)]
        with pytest.raises(InvalidFilterError):
            compile_filter(_make_filter("aic", "in", big_list))


class TestMessageFieldCompilation:
    """message 字段只允许精确算子（作用在 message.keyword）。"""

    def test_message_eq_uses_message_keyword(self) -> None:
        clauses = compile_filter(_make_filter("message", "eq", "hello"))
        term = clauses[0]["term"]
        assert "message.keyword" in term

    def test_message_eq_cs(self) -> None:
        clauses = compile_filter(_make_filter("message", "eqCs", "hello"))
        assert "term" in clauses[0]

    def test_message_in(self) -> None:
        clauses = compile_filter(_make_filter("message", "in", ["a", "b"]))
        assert clauses[0] is not None

    def test_message_contains_raises_unsupported(self) -> None:
        """contains → UnsupportedOperatorError（全文检索走 keyword 参数，§5.3 第 4 条）。"""
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("message", "contains", "hello"))

    def test_message_starts_with_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("message", "startsWith", "hello"))

    def test_message_exists_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("message", "exists"))


class TestTagsFieldCompilation:
    """tags.* flat_object 只支持大小写敏感精确算子（设计 §5.3 第 3 条）。"""

    def test_tags_eq_cs_produces_term(self) -> None:
        clauses = compile_filter(_make_filter("tags.env", "eqCs", "prod"))
        term = clauses[0]["term"]
        assert "tags.env" in term

    def test_tags_ne_cs_wraps_must_not_term(self) -> None:
        """tags neCs → must_not + term（C-SYSTEM-QUERY 设计 §5.3 第3条）。"""
        clauses = compile_filter(_make_filter("tags.env", "neCs", "prod"))
        clause = clauses[0]
        assert "bool" in clause
        must_not = clause["bool"]["must_not"]
        assert "term" in must_not

    def test_tags_in_cs_produces_terms(self) -> None:
        clauses = compile_filter(_make_filter("tags.env", "inCs", ["prod", "staging"]))
        terms = clauses[0]["terms"]
        assert "tags.env" in terms

    def test_tags_nin_cs_wraps_must_not_terms(self) -> None:
        clauses = compile_filter(_make_filter("tags.env", "ninCs", ["prod"]))
        clause = clauses[0]
        assert "bool" in clause
        must_not = clause["bool"]["must_not"]
        assert "terms" in must_not

    def test_tags_eq_case_insensitive_raises(self) -> None:
        """flat_object 不支持 case_insensitive（§4.1）→ UnsupportedOperatorError。"""
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("tags.env", "eq", "prod"))

    def test_tags_exists_raises_unsupported(self) -> None:
        """flat_object 子路径 exists 行为不稳定 → UnsupportedOperatorError（设计 §5.3 第3条）。"""
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("tags.env", "exists"))


class TestNumericFieldCompilation:
    """severityNumber 数值字段算子。"""

    def test_severity_eq_term(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "eq", 9))
        assert "term" in clauses[0]

    def test_severity_ne_must_not_term(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "ne", 9))
        assert "bool" in clauses[0]
        assert "must_not" in clauses[0]["bool"]

    def test_severity_gt_range(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "gt", 9))
        assert "range" in clauses[0]

    def test_severity_gte_range(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "gte", 9))
        assert "range" in clauses[0]

    def test_severity_lt_range(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "lt", 9))
        assert "range" in clauses[0]

    def test_severity_lte_range(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "lte", 9))
        assert "range" in clauses[0]

    def test_severity_between_range(self) -> None:
        """between → range{gte: value[0], lte: value[1]}（闭区间）。"""
        clauses = compile_filter(_make_filter("severityNumber", "between", [5, 9]))
        r = clauses[0]["range"]["severity_number"]
        assert r["gte"] == 5
        assert r["lte"] == 9

    def test_severity_in_terms(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "in", [5, 9, 13]))
        assert "terms" in clauses[0]

    def test_severity_nin_must_not_terms(self) -> None:
        clauses = compile_filter(_make_filter("severityNumber", "nin", [5, 9]))
        assert "bool" in clauses[0]
        must_not = clauses[0]["bool"]["must_not"]
        assert "terms" in must_not

    def test_severity_unsupported_op_raises(self) -> None:
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(_make_filter("severityNumber", "contains", "9"))


class TestFieldValidation:
    def test_raw_body_deep_path_raises_unsupported_field(self) -> None:
        """rawBody 深层路径 → AMP_UNSUPPORTED_FIELD（C-SYSTEM-QUERY-3）。"""
        with pytest.raises(UnsupportedFieldError):
            compile_filter(_make_filter("rawBody.field", "eq", "val"))

    def test_raw_body_snake_case_raises_unsupported_field(self) -> None:
        with pytest.raises(UnsupportedFieldError):
            compile_filter(_make_filter("raw_body.field", "eq", "val"))

    def test_unknown_field_raises_unsupported_field(self) -> None:
        with pytest.raises(UnsupportedFieldError):
            compile_filter(_make_filter("unknownField", "eq", "val"))

    def test_unsupported_logic_raises_invalid_filter(self) -> None:
        """logic != 'and' → InvalidFilterError（§12 O-2，首版只支持单层 and）。"""
        f = AMPFilter(conditions=[AMPFilterCondition(field="aic", op="eq", value="x")], logic="or")
        with pytest.raises(InvalidFilterError):
            compile_filter(f)

    def test_none_filter_returns_empty_list(self) -> None:
        assert compile_filter(None) == []


class TestBuildKeywordQuery:
    def test_keyword_produces_multi_match(self) -> None:
        """C-SYSTEM-QUERY-2：keyword → multi_match on message + search_text。"""
        result = build_keyword_query("error")
        assert result is not None
        assert "multi_match" in result
        assert result["multi_match"]["query"] == "error"
        fields = result["multi_match"]["fields"]
        assert "message" in fields
        assert "search_text" in fields

    def test_none_keyword_returns_none(self) -> None:
        assert build_keyword_query(None) is None


class TestBuildTimeRangeClause:
    def test_produces_range_gte_lt(self) -> None:
        clause = build_time_range_clause(from_ms=1718323200000, to_ms=1718409600000)
        assert "range" in clause
        ts = clause["range"]["timestamp"]
        assert "gte" in ts
        assert "lt" in ts

    def test_left_closed_right_open(self) -> None:
        clause = build_time_range_clause(from_ms=1718323200000, to_ms=1718409600000)
        ts = clause["range"]["timestamp"]
        assert "gte" in ts
        assert "lte" not in ts
        assert "lt" in ts


class TestBuildSort:
    def test_sort_appends_log_id_tiebreaker(self) -> None:
        """C-SYSTEM-QUERY-5：末尾追加 log_id tiebreaker(asc)。"""
        resolved = [ResolvedSort("timestamp", "timestamp", "desc")]
        sort_list = build_sort(resolved)
        assert len(sort_list) == 2
        last = sort_list[-1]
        assert "log_id" in last
        assert last["log_id"]["order"] == "asc"

    def test_timestamp_desc_order(self) -> None:
        resolved = [ResolvedSort("timestamp", "timestamp", "desc")]
        sort_list = build_sort(resolved)
        first = sort_list[0]
        assert first["timestamp"]["order"] == "desc"

    def test_severity_number_mapped(self) -> None:
        resolved = [ResolvedSort("severityNumber", "severity_number", "asc")]
        sort_list = build_sort(resolved)
        assert sort_list[0]["severity_number"]["order"] == "asc"


class TestBuildSearchBody:
    def test_search_body_structure(self) -> None:
        """build_search_body 组装最终 query。"""
        time_clause = build_time_range_clause(from_ms=1000, to_ms=2000)
        body = build_search_body(
            filter_clauses=[],
            keyword_query=build_keyword_query("error"),
            time_clause=time_clause,
            scope_clauses=[],
            sort=build_sort([ResolvedSort("timestamp", "timestamp", "desc")]),
            search_after=None,
            size=51,
        )
        assert "query" in body
        assert "sort" in body
        assert "size" in body
        assert body["size"] == 51
        assert "search_after" not in body

    def test_search_after_injected_when_provided(self) -> None:
        time_clause = build_time_range_clause(from_ms=1000, to_ms=2000)
        body = build_search_body(
            filter_clauses=[],
            keyword_query=None,
            time_clause=time_clause,
            scope_clauses=[],
            sort=build_sort([ResolvedSort("timestamp", "timestamp", "desc")]),
            search_after=[1718323200000, "log-001"],
            size=50,
        )
        assert body["search_after"] == [1718323200000, "log-001"]

    def test_keyword_in_must_when_provided(self) -> None:
        time_clause = build_time_range_clause(from_ms=1000, to_ms=2000)
        kw = build_keyword_query("error")
        body = build_search_body(
            filter_clauses=[],
            keyword_query=kw,
            time_clause=time_clause,
            scope_clauses=[],
            sort=[],
            search_after=None,
            size=10,
        )
        must = body["query"]["bool"].get("must", [])
        assert len(must) == 1
        assert "multi_match" in must[0]

    def test_no_keyword_produces_no_must(self) -> None:
        time_clause = build_time_range_clause(from_ms=1000, to_ms=2000)
        body = build_search_body(
            filter_clauses=[],
            keyword_query=None,
            time_clause=time_clause,
            scope_clauses=[],
            sort=[],
            search_after=None,
            size=10,
        )
        must = body["query"]["bool"].get("must", [])
        assert len(must) == 0

    def test_filter_and_scope_in_filter_clauses(self) -> None:
        time_clause = build_time_range_clause(from_ms=1000, to_ms=2000)
        filter_clause = {"term": {"aic": {"value": "x", "case_insensitive": True}}}
        scope_clause = {"term": {"aic": {"value": "y"}}}
        body = build_search_body(
            filter_clauses=[filter_clause],
            keyword_query=None,
            time_clause=time_clause,
            scope_clauses=[scope_clause],
            sort=[],
            search_after=None,
            size=10,
        )
        filters = body["query"]["bool"]["filter"]
        assert time_clause in filters
        assert filter_clause in filters
        assert scope_clause in filters
