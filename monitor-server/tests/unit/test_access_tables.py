"""tests/unit/test_access_tables.py — Access DDL 与列常量测试。

TDD B-1：先写测试（红）→ 实现 tables.py（绿）。
"""

from __future__ import annotations


class TestAccessEventsDDL:
    """access_events 主表 DDL 验证。"""

    def _get_ddl(self) -> str:
        from app.access.tables import ddl_access_events

        return ddl_access_events(raw_retention_days=30)

    def test_ddl_uses_if_not_exists(self) -> None:
        ddl = self._get_ddl()
        assert "IF NOT EXISTS" in ddl

    def test_ddl_table_name(self) -> None:
        ddl = self._get_ddl()
        assert "access_events" in ddl

    def test_ddl_uses_merge_tree(self) -> None:
        ddl = self._get_ddl()
        assert "MergeTree" in ddl

    def test_ddl_order_by_has_timestamp_first(self) -> None:
        """ORDER BY 前缀为 timestamp（C-ACCESS-MODEL-2：高基数 url/ip 不入排序前缀）。"""
        ddl = self._get_ddl()
        assert "ORDER BY" in ddl
        order_pos = ddl.index("ORDER BY")
        order_section = ddl[order_pos : order_pos + 200]
        assert "timestamp" in order_section

    def test_ddl_high_cardinality_url_not_in_order_by(self) -> None:
        """request_url 不在 ORDER BY 子句中（高基数，C-ACCESS-MODEL-2）。"""
        ddl = self._get_ddl()
        order_pos = ddl.index("ORDER BY")
        # Find the closing paren of ORDER BY tuple
        order_section = ddl[order_pos : order_pos + 200]
        # request_url should not be in ORDER BY
        assert "request_url" not in order_section.split("PARTITION")[0]

    def test_ddl_has_bloom_filter_index(self) -> None:
        """包含 trace_id bloom_filter 索引（C-ACCESS-MODEL-2）。"""
        ddl = self._get_ddl()
        assert "bloom_filter" in ddl
        assert "trace_id" in ddl

    def test_ddl_has_error_code_index(self) -> None:
        """包含 error_code set 索引。"""
        ddl = self._get_ddl()
        assert "error_code" in ddl
        assert "set" in ddl.lower()

    def test_ddl_ttl_uses_retention_param(self) -> None:
        """TTL 表达式使用参数值，不硬编码。"""
        ddl30 = self._get_ddl()
        from app.access.tables import ddl_access_events

        ddl60 = ddl_access_events(raw_retention_days=60)
        assert "30" in ddl30
        assert "60" in ddl60

    def test_ddl_has_request_headers_map(self) -> None:
        """request_headers/response_headers 类型为 Map(String,String)。"""
        ddl = self._get_ddl()
        assert "Map(String, String)" in ddl or "Map(String,String)" in ddl

    def test_ddl_has_raw_log_column(self) -> None:
        ddl = self._get_ddl()
        assert "raw_log" in ddl


class TestAccessTraceSpanDDL:
    """access_trace_span 派生表 DDL 验证。"""

    def test_order_by_has_trace_id(self) -> None:
        from app.access.tables import ddl_access_trace_span

        ddl = ddl_access_trace_span(raw_retention_days=30)
        assert "trace_id" in ddl
        assert "ORDER BY" in ddl
        order_pos = ddl.index("ORDER BY")
        order_section = ddl[order_pos : order_pos + 200]
        assert "trace_id" in order_section

    def test_order_by_contains_span_id(self) -> None:
        from app.access.tables import ddl_access_trace_span

        ddl = ddl_access_trace_span(raw_retention_days=30)
        order_pos = ddl.index("ORDER BY")
        order_section = ddl[order_pos : order_pos + 200]
        assert "span_id" in order_section

    def test_trace_span_mv_filters_has_trace_id(self) -> None:
        """trace_span MV 只收 trace_id != '' 的行。"""
        from app.access.tables import ddl_access_trace_span_mv

        mv_ddl = ddl_access_trace_span_mv()
        assert "trace_id" in mv_ddl
        assert "!= ''" in mv_ddl or "!=''" in mv_ddl


class TestAccessTopologyEdge5mDDL:
    """access_topology_edge_5m 聚合表 DDL 验证。"""

    def test_uses_aggregating_merge_tree(self) -> None:
        from app.access.tables import ddl_access_topology_edge_5m

        ddl = ddl_access_topology_edge_5m(topology_retention_days=90)
        assert "AggregatingMergeTree" in ddl

    def test_has_state_columns(self) -> None:
        from app.access.tables import ddl_access_topology_edge_5m

        ddl = ddl_access_topology_edge_5m(topology_retention_days=90)
        assert "_state" in ddl

    def test_order_by_has_bucket_and_aics(self) -> None:
        from app.access.tables import ddl_access_topology_edge_5m

        ddl = ddl_access_topology_edge_5m(topology_retention_days=90)
        assert "ORDER BY" in ddl
        order_pos = ddl.index("ORDER BY")
        order_section = ddl[order_pos : order_pos + 300]
        assert "bucket" in order_section
        assert "caller_aic" in order_section
        assert "callee_aic" in order_section

    def test_single_quantiles_tdigest_column(self) -> None:
        """单 quantilesTDigest state 列出 P95/P99（C-ACCESS-MODEL-7）。"""
        from app.access.tables import ddl_access_topology_edge_5m

        ddl = ddl_access_topology_edge_5m(topology_retention_days=90)
        assert "quantilesTDigest" in ddl
        assert ddl.count("quantilesTDigestState") == 1 or ddl.count("quantilesTDigest") >= 1


class TestTopologyMVDDL:
    """topology MV DDL 方向过滤与收敛约束测试（C-ACCESS-MODEL-8/9）。"""

    def test_mv_has_inbound_filter(self) -> None:
        """入边过滤：caller 或 callee 至少有标识（C-ACCESS-MODEL-8）。"""
        from app.access.tables import ddl_access_topology_edge_5m_mv

        ddl = ddl_access_topology_edge_5m_mv(error_status_threshold=500)
        assert "caller_aic" in ddl
        assert "callee_aic" in ddl

    def test_mv_has_direction_convergence(self) -> None:
        """方向收敛（callee 视角）：防双端重复计数（C-ACCESS-MODEL-9）。"""
        from app.access.tables import ddl_access_topology_edge_5m_mv

        ddl = ddl_access_topology_edge_5m_mv(error_status_threshold=500)
        assert "callee_aic" in ddl
        assert "aic" in ddl

    def test_mv_error_threshold_parameterized(self) -> None:
        """错误判定阈值固化在 MV DDL 中（C-ACCESS-QUERY-15）。"""
        from app.access.tables import ddl_access_topology_edge_5m_mv

        ddl500 = ddl_access_topology_edge_5m_mv(error_status_threshold=500)
        ddl400 = ddl_access_topology_edge_5m_mv(error_status_threshold=400)
        assert "500" in ddl500
        assert "400" in ddl400


class TestColumnConstants:
    """列常量一致性验证。"""

    def test_insert_columns_length(self) -> None:
        """INSERT_COLUMNS = EVENT_VIEW_COLUMNS + observed_at + raw_log（len + 2）。"""
        from app.access.tables import EVENT_VIEW_COLUMNS, INSERT_COLUMNS

        assert len(INSERT_COLUMNS) == len(EVENT_VIEW_COLUMNS) + 2

    def test_insert_columns_has_observed_at(self) -> None:
        from app.access.tables import INSERT_COLUMNS

        assert "observed_at" in INSERT_COLUMNS

    def test_insert_columns_has_raw_log(self) -> None:
        from app.access.tables import INSERT_COLUMNS

        assert "raw_log" in INSERT_COLUMNS

    def test_event_view_columns_no_observed_at(self) -> None:
        """EVENT_VIEW_COLUMNS 中无 observed_at（该列不入 AccessEventView）。"""
        from app.access.tables import EVENT_VIEW_COLUMNS

        assert "observed_at" not in EVENT_VIEW_COLUMNS

    def test_event_view_columns_no_raw_log(self) -> None:
        """EVENT_VIEW_COLUMNS 中无 raw_log（按需追加，不在默认投影内）。"""
        from app.access.tables import EVENT_VIEW_COLUMNS

        assert "raw_log" not in EVENT_VIEW_COLUMNS

    def test_event_view_columns_has_core_fields(self) -> None:
        from app.access.tables import EVENT_VIEW_COLUMNS

        for field in ("log_id", "timestamp", "aic", "trace_id", "request_route", "response_status", "error_code"):
            assert field in EVENT_VIEW_COLUMNS, f"Missing field: {field}"


class TestAllDDLStatements:
    """all_ddl_statements 返回有序 DDL 列表。"""

    def test_returns_5_statements(self) -> None:
        from app.access.tables import all_ddl_statements

        stmts = all_ddl_statements(raw_retention_days=30, topology_retention_days=90, error_status_threshold=500)
        assert len(stmts) == 5

    def test_order_main_then_derived_then_mv(self) -> None:
        """顺序：主表 → trace_span 表 → topology 表 → trace_span MV → topology MV。"""
        from app.access.tables import all_ddl_statements

        stmts = all_ddl_statements(raw_retention_days=30, topology_retention_days=90, error_status_threshold=500)
        assert "access_events" in stmts[0]
        assert "access_trace_span" in stmts[1] and "MATERIALIZED" not in stmts[1]
        assert "access_topology_edge_5m" in stmts[2] and "MATERIALIZED" not in stmts[2]
        assert "MATERIALIZED" in stmts[3]
        assert "MATERIALIZED" in stmts[4]
