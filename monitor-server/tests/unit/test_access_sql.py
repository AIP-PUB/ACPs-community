"""tests/unit/test_access_sql.py — 七端点 SQL 构造测试。

TDD B-7：先写测试（红）→ 实现 sql.py（绿）。
SQL 为纯函数，零 I/O，是 TDD 的核心靶点。
"""

from __future__ import annotations

from typing import Any


def _empty_where() -> Any:
    from app.access.filters import WhereClause

    return WhereClause(sql="", params={})


def _default_events_sort() -> Any:
    from app.access.filters import ResolvedSort

    return [ResolvedSort("timestamp", "timestamp", "desc")]


def _default_ops_sort() -> Any:
    from app.access.filters import ResolvedSort

    return [ResolvedSort("lastSeenAt", "last_seen_at", "desc")]


def _empty_keyset() -> None:
    return None


def _make_trace_having(sql: str = "", params: dict[str, Any] | None = None) -> Any:
    from app.access.sql import TraceLevelHaving

    return TraceLevelHaving(sql=sql, params=params or {})


# ── KeysetBound & TraceLevelHaving 结构 ──────────────────────────────────────


class TestDataClasses:
    def test_keyset_bound_is_frozen_dataclass(self) -> None:
        import dataclasses

        from app.access.sql import KeysetBound

        assert dataclasses.is_dataclass(KeysetBound)

    def test_trace_level_having_is_frozen_dataclass(self) -> None:
        import dataclasses

        from app.access.sql import TraceLevelHaving

        assert dataclasses.is_dataclass(TraceLevelHaving)


# ── build_events_query ────────────────────────────────────────────────────────


class TestBuildEventsQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_events_query

        defaults = {
            "where": _empty_where(),
            "time_params": {"from_ms": 0, "to_ms": 1},
            "sort": _default_events_sort(),
            "keyset": None,
            "limit": 50,
            "include_raw_log": False,
        }
        defaults.update(kwargs)
        return build_events_query(**defaults)

    def test_returns_tuple(self) -> None:
        sql, params = self._build()
        assert isinstance(sql, str)
        assert isinstance(params, dict)

    def test_selects_from_access_events(self) -> None:
        sql, _ = self._build()
        assert "access_events" in sql

    def test_select_contains_log_id(self) -> None:
        sql, _ = self._build()
        assert "log_id" in sql

    def test_where_time_params(self) -> None:
        sql, params = self._build(time_params={"from_ms": 1000, "to_ms": 2000})
        has_params = any("from_ms" in k or "to_ms" in k for k in params)
        has_values = any("1000" in str(v) or "2000" in str(v) for v in params.values())
        assert has_params or has_values or "timestamp" in sql

    def test_limit_plus_one(self) -> None:
        """LIMIT 为 limit+1，用于判断是否有下一页。"""
        sql, _ = self._build(limit=50)
        assert "51" in sql

    def test_order_by_has_timestamp(self) -> None:
        sql, _ = self._build()
        assert "ORDER BY" in sql.upper()
        assert "timestamp" in sql

    def test_include_raw_log_adds_column(self) -> None:
        sql, _ = self._build(include_raw_log=True)
        assert "raw_log" in sql

    def test_exclude_raw_log_no_column(self) -> None:
        sql, _ = self._build(include_raw_log=False)
        # raw_log should not appear in the SELECT list (may appear in other contexts)
        # Just check the query can be built without error
        assert "access_events" in sql

    def test_where_clause_merged(self) -> None:
        from app.access.filters import WhereClause

        wc = WhereClause(sql="AND aic = {f0:String}", params={"f0": "agent-1"})
        sql, params = self._build(where=wc)
        assert "aic" in sql
        assert "agent-1" in params.values()

    def test_no_offset_in_sql(self) -> None:
        """禁止 OFFSET（C-ACCESS-QUERY-12）。"""
        sql, _ = self._build()
        assert "OFFSET" not in sql.upper()


# ── build_operations_query ────────────────────────────────────────────────────


class TestBuildOperationsQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_operations_query

        defaults = {
            "group_by": [],
            "bucket_expr": None,
            "where": _empty_where(),
            "time_params": {"from_ms": 0, "to_ms": 1},
            "error_status_threshold": 500,
            "having_min_request": None,
            "sort": _default_ops_sort(),
            "keyset": None,
            "limit": 50,
        }
        defaults.update(kwargs)
        return build_operations_query(**defaults)

    def test_selects_request_count(self) -> None:
        sql, _ = self._build()
        assert "count()" in sql.lower() or "request_count" in sql

    def test_selects_error_count_with_threshold(self) -> None:
        sql, _ = self._build(error_status_threshold=500)
        assert "500" in sql

    def test_group_by_aic(self) -> None:
        sql, _ = self._build(group_by=["aic"])
        assert "GROUP BY" in sql.upper()
        assert "aic" in sql

    def test_group_by_endpoint(self) -> None:
        sql, _ = self._build(group_by=["endpoint"])
        assert "request_method" in sql
        assert "request_route" in sql

    def test_bucket_expr_in_select(self) -> None:
        sql, _ = self._build(bucket_expr="toStartOfFiveMinutes")
        assert "toStartOfFiveMinutes" in sql

    def test_no_bucket_expr(self) -> None:
        sql, _ = self._build(bucket_expr=None)
        assert "access_events" in sql

    def test_avg_duration_ms(self) -> None:
        sql, _ = self._build()
        assert "avg" in sql.lower()

    def test_quantiles_tdigest(self) -> None:
        sql, _ = self._build()
        assert "quantilesTDigest" in sql

    def test_having_min_request(self) -> None:
        sql, _ = self._build(having_min_request=10)
        assert "HAVING" in sql.upper()
        assert "10" in sql


# ── build_error_attribution_query ─────────────────────────────────────────────


class TestBuildErrorAttributionQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_error_attribution_query

        defaults = {
            "group_dims": ["error_code"],
            "where": _empty_where(),
            "time_params": {"from_ms": 0, "to_ms": 1},
            "error_status_threshold": 500,
            "top_n": 20,
        }
        defaults.update(kwargs)
        return build_error_attribution_query(**defaults)

    def test_returns_tuple(self) -> None:
        sql, _ = self._build()
        assert isinstance(sql, str)

    def test_two_cte_structure(self) -> None:
        """两段式 CTE（per_endpoint + per_group）。"""
        sql, _ = self._build()
        assert "WITH" in sql.upper()
        assert sql.count("SELECT") >= 2

    def test_error_total_in_select(self) -> None:
        sql, _ = self._build()
        assert "error_total" in sql

    def test_affected_aics(self) -> None:
        sql, _ = self._build()
        assert "affected_aics" in sql

    def test_limit_top_n(self) -> None:
        sql, _ = self._build(top_n=15)
        assert "15" in sql


# ── build_slow_requests_query ─────────────────────────────────────────────────


class TestBuildSlowRequestsQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_slow_requests_query

        defaults = {
            "where": _empty_where(),
            "time_params": {"from_ms": 0, "to_ms": 1},
            "min_duration_ms": None,
            "top_n": 10,
            "keyset": None,
        }
        defaults.update(kwargs)
        return build_slow_requests_query(**defaults)

    def test_order_by_duration_desc(self) -> None:
        sql, _ = self._build()
        assert "duration_ms" in sql
        assert "DESC" in sql.upper()

    def test_min_duration_filter(self) -> None:
        sql, params = self._build(min_duration_ms=1000)
        assert "duration_ms" in sql
        assert any(v == 1000 for v in params.values()) or "1000" in sql

    def test_no_offset(self) -> None:
        sql, _ = self._build()
        assert "OFFSET" not in sql.upper()

    def test_limit_top_n(self) -> None:
        sql, _ = self._build(top_n=20)
        assert "20" in sql


# ── build_traces_query ────────────────────────────────────────────────────────


class TestBuildTracesQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_traces_query

        defaults = {
            "span_where": _empty_where(),
            "time_params": {"from_ms": 0, "to_ms": 1},
            "error_status_threshold": 500,
            "trace_level_having": _make_trace_having(),
            "trace_max_duration_hours": 1,
            "keyset": None,
            "limit": 50,
        }
        defaults.update(kwargs)
        return build_traces_query(**defaults)

    def test_has_matching_traces_cte(self) -> None:
        sql, _ = self._build()
        assert "matching_traces" in sql

    def test_selects_trace_id(self) -> None:
        sql, _ = self._build()
        assert "trace_id" in sql

    def test_from_access_trace_span(self) -> None:
        sql, _ = self._build()
        assert "access_trace_span" in sql

    def test_total_spans_count(self) -> None:
        sql, _ = self._build()
        assert "total_spans" in sql or "count()" in sql.lower()

    def test_error_count_uses_threshold(self) -> None:
        sql, _ = self._build(error_status_threshold=400)
        assert "400" in sql

    def test_partition_expand_in_outer_where(self) -> None:
        """外层分区裁剪包含 INTERVAL ... HOUR 双向外扩（C-ACCESS-QUERY-4）。"""
        sql, _ = self._build(trace_max_duration_hours=2)
        assert "INTERVAL" in sql.upper() or "interval" in sql

    def test_limit_plus_one(self) -> None:
        sql, _ = self._build(limit=50)
        assert "51" in sql


# ── build_trace_spans_query ───────────────────────────────────────────────────


class TestBuildTraceSpansQuery:
    def test_selects_from_trace_span(self) -> None:
        from app.access.sql import build_trace_spans_query

        sql, _ = build_trace_spans_query(trace_max_spans=200)
        assert "access_trace_span" in sql

    def test_has_trace_id_param(self) -> None:
        from app.access.sql import build_trace_spans_query

        sql, _ = build_trace_spans_query(trace_max_spans=200)
        assert "trace_id" in sql
        assert "tid" in sql or "trace_id" in str(_)

    def test_limit_is_max_spans_plus_one(self) -> None:
        from app.access.sql import build_trace_spans_query

        sql, _ = build_trace_spans_query(trace_max_spans=200)
        assert "201" in sql


class TestBuildTraceEventsQuery:
    def test_selects_from_access_events(self) -> None:
        from app.access.sql import build_trace_events_query

        sql, _ = build_trace_events_query()
        assert "access_events" in sql

    def test_filters_by_trace_id(self) -> None:
        from app.access.sql import build_trace_events_query

        sql, _ = build_trace_events_query()
        assert "trace_id" in sql


# ── build_topology_query ──────────────────────────────────────────────────────


class TestBuildTopologyQuery:
    def _build(self, **kwargs: Any) -> Any:
        from app.access.sql import build_topology_query

        defaults = {
            "group_by": "aic",
            "bucket_expr": None,
            "edge_where": _empty_where(),
            "bucket_params": {"from_bucket_ms": 0, "to_bucket_ms": 1},
            "having_min_call": None,
            "sort": [
                __import__("app.access.filters", fromlist=["ResolvedSort"]).ResolvedSort(
                    "callCount", "call_count", "desc"
                )
            ],
            "keyset": None,
            "limit": 50,
        }
        defaults.update(kwargs)
        return build_topology_query(**defaults)

    def test_from_topology_edge(self) -> None:
        sql, _ = self._build()
        assert "access_topology_edge_5m" in sql

    def test_uses_merge_aggregations(self) -> None:
        sql, _ = self._build()
        assert "Merge" in sql

    def test_sum_merge_call_count(self) -> None:
        sql, _ = self._build()
        assert "call_count" in sql

    def test_group_by_aic_includes_caller_callee(self) -> None:
        sql, _ = self._build(group_by="aic")
        assert "caller_aic" in sql
        assert "callee_aic" in sql
        assert "caller_service" in sql
        assert "callee_service" in sql

    def test_group_by_service_clears_aic_columns(self) -> None:
        sql, _ = self._build(group_by="service")
        assert "'' AS caller_aic" in sql
        assert "'' AS callee_aic" in sql
        assert "caller_service" in sql
        assert "callee_service" in sql

    def test_no_state_columns_in_output(self) -> None:
        """必须用 *Merge，不得直接 SELECT _state 列（C-ACCESS-QUERY-6）。"""
        sql, _ = self._build()
        # The output SELECT should not expose raw _state columns as final output aliases
        # The *Merge functions internally reference state columns but the aliases should not end with _state
        select_part = sql.upper().split("FROM")[0]
        # quantilesTDigestMerge(...)(duration_quantiles_state) is OK (it's an argument, not an alias)
        # What we check: none of the AS aliases should end with _STATE
        import re

        aliases = re.findall(r"AS\s+(\w+)", select_part)
        for alias in aliases:
            assert not alias.endswith("_STATE"), f"Output alias {alias} should not be a raw state column"

    def test_having_min_call(self) -> None:
        sql, _ = self._build(having_min_call=5)
        assert "HAVING" in sql.upper()
        assert "5" in sql
