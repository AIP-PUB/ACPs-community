"""tests/unit/test_access_schema.py — Access 模块请求/响应模型测试。

TDD B-8：先写测试（红）→ 实现 schema.py（绿）。
"""

from __future__ import annotations


class TestAccessEventViewSchema:
    """AccessEventView 字段类型与别名测试。"""

    def test_event_view_duration_ms_is_int(self) -> None:
        """原始事件 durationMs 必须为 int（C-ACCESS-MODEL，与 CH UInt32 一致）。"""
        from app.access.schema import AccessEventView

        fields = AccessEventView.model_fields
        assert fields["duration_ms"].annotation is int or str(fields["duration_ms"].annotation) in (
            "int",
            "<class 'int'>",
        )

    def test_event_view_camelcase_aliases(self) -> None:
        """视图字段 camelCase 别名正常序列化。"""
        from app.access.schema import AccessEventView

        data = {
            "logId": "abc",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "aic-1",
            "traceId": "t1",
            "spanId": "s1",
            "parentSpanId": "",
            "correlationId": "",
            "severity": "INFO",
            "durationMs": 42,
            "requestMethod": "GET",
            "requestRoute": "/users/{id}",
            "requestUrl": "/users/123",
            "requestSize": 0,
            "responseStatus": 200,
            "responseSize": 100,
            "callerAic": "",
            "callerService": "",
            "callerIp": "",
            "calleeAic": "aic-1",
            "calleeService": "svc",
            "calleeIp": "10.0.0.1",
            "errorCode": "",
            "errorMessage": "",
            "serviceName": "svc",
            "deploymentEnv": "prod",
            "requestHeaders": {},
            "responseHeaders": {},
            "attributes": {},
        }
        view = AccessEventView.model_validate(data)
        assert view.log_id == "abc"
        assert view.duration_ms == 42
        assert view.request_method == "GET"

    def test_event_view_populate_by_name(self) -> None:
        """populate_by_name=True：snake_case 也能构造。"""
        from app.access.schema import AccessEventView

        view = AccessEventView(
            log_id="abc",
            timestamp="2026-01-01T00:00:00Z",
            aic="aic-1",
            trace_id="t1",
            span_id="s1",
            parent_span_id="",
            correlation_id="",
            severity="INFO",
            duration_ms=10,
            request_method="GET",
            request_route="/health",
            request_url="/health",
            request_size=0,
            response_status=200,
            response_size=50,
            caller_aic="",
            caller_service="",
            caller_ip="",
            callee_aic="a",
            callee_service="s",
            callee_ip="1.2.3.4",
            error_code="",
            error_message="",
            service_name="svc",
            deployment_env="dev",
            request_headers={},
            response_headers={},
            attributes={},
        )
        assert view.log_id == "abc"


class TestAccessOperationSummary:
    """AccessOperationSummary 聚合字段类型测试。"""

    def test_avg_duration_ms_is_float(self) -> None:
        """聚合字段 avgDurationMs 必须为 float（偏异 D-2 最小范围）。"""
        from app.access.schema import AccessOperationSummary

        fields = AccessOperationSummary.model_fields
        ann = fields["avg_duration_ms"].annotation
        assert ann is float or str(ann) in ("float", "<class 'float'>")

    def test_error_rate_is_float(self) -> None:
        from app.access.schema import AccessOperationSummary

        fields = AccessOperationSummary.model_fields
        ann = fields["error_rate"].annotation
        assert ann is float or str(ann) in ("float", "<class 'float'>")

    def test_dimensions_is_dict(self) -> None:
        """dimensions 字段为 dict[str, str]（endpoint/service/aic 回填值）。"""
        from app.access.schema import AccessOperationSummary

        summary = AccessOperationSummary(
            dimensions={"endpoint": "GET /users/{id}"},
            request_count=10,
            error_count=1,
            error_rate=0.1,
            avg_duration_ms=50.0,
            p95_duration_ms=100.0,
            p99_duration_ms=200.0,
            last_seen_at="2026-01-01T00:00:00Z",
        )
        assert summary.dimensions == {"endpoint": "GET /users/{id}"}


class TestAccessTraceView:
    """AccessTraceView 裸资源结构（无 meta）。"""

    def test_trace_view_has_no_meta(self) -> None:
        """AccessTraceView 不包含 meta 字段（裸资源响应）。"""
        from app.access.schema import AccessTraceView

        fields = AccessTraceView.model_fields
        assert "meta" not in fields

    def test_trace_view_has_spans(self) -> None:
        from app.access.schema import AccessTraceView

        view = AccessTraceView(trace_id="t1", spans=[], summary=None)
        assert view.trace_id == "t1"
        assert view.spans == []


class TestAccessTopologyEdge:
    """AccessTopologyEdge 聚合字段测试。"""

    def test_call_count_is_int(self) -> None:
        from app.access.schema import AccessTopologyEdge

        fields = AccessTopologyEdge.model_fields
        ann = fields["call_count"].annotation
        assert ann is int or str(ann) in ("int", "<class 'int'>")

    def test_error_rate_is_float(self) -> None:
        from app.access.schema import AccessTopologyEdge

        fields = AccessTopologyEdge.model_fields
        ann = fields["error_rate"].annotation
        assert ann is float or str(ann) in ("float", "<class 'float'>")


class TestAccessQueryRequests:
    """请求模型扩展字段测试。"""

    def test_operation_request_has_group_by(self) -> None:
        from app.access.schema import AccessOperationQueryRequest

        req = AccessOperationQueryRequest()
        assert hasattr(req, "group_by")

    def test_trace_request_has_has_error(self) -> None:
        from app.access.schema import AccessTraceQueryRequest

        req = AccessTraceQueryRequest()
        assert hasattr(req, "has_error")

    def test_topology_request_has_group_by(self) -> None:
        from app.access.schema import AccessTopologyQueryRequest

        req = AccessTopologyQueryRequest()
        assert hasattr(req, "group_by")

    def test_error_attribution_request_has_top_n(self) -> None:
        from app.access.schema import AccessErrorAttributionRequest

        req = AccessErrorAttributionRequest()
        assert hasattr(req, "top_n")

    def test_slow_request_has_min_duration_ms(self) -> None:
        from app.access.schema import AccessSlowRequestRequest

        req = AccessSlowRequestRequest()
        assert hasattr(req, "min_duration_ms")

    def test_base_request_has_include_raw_log(self) -> None:
        from app.access.schema import AccessQueryRequest

        req = AccessQueryRequest()
        assert hasattr(req, "include_raw_log")

    def test_pagination_request_uses_amp_page(self) -> None:
        from app.access.schema import AccessQueryRequest
        from app.core.amp_api_schema import AMPPaginationRequest

        req = AccessQueryRequest()
        assert isinstance(req.page, AMPPaginationRequest)
