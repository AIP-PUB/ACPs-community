"""tests/unit/test_access_service.py — Query Provider 服务层测试（D-2）。

TDD D-2：先写测试（红）→ 实现 service.py / trace_service.py / topology_service.py（绿）。
全部 Mock store / freshness；不做实际 I/O。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


def _make_redis() -> Any:
    return AsyncMock()


def _make_freshness_view(lagging: bool = False) -> Any:
    from app.access.freshness import FreshnessView

    return FreshnessView(
        data_freshness_at_ms=1_700_000_000_000,
        ingestion_lag_ms=1000,
        lagging=lagging,
    )


def _make_time_range(start: str | None = None, end: str | None = None) -> Any:
    from datetime import datetime, timedelta

    from app.core.amp_api_schema import AMPTimeRange

    if start is None:
        now = datetime.now(UTC)
        start = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        end = now.isoformat().replace("+00:00", "Z")
    return AMPTimeRange(start_at=start, end_at=end)


# ── service.query_events ──────────────────────────────────────────────────────


class TestQueryEvents:
    @pytest.mark.asyncio
    async def test_returns_events_and_meta(self) -> None:
        from app.access.schema import AccessQueryRequest
        from app.access.service import query_events
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessQueryRequest(time_range=_make_time_range())

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_events_query = AsyncMock(return_value=[])
            rows, meta = await query_events(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)

    @pytest.mark.asyncio
    async def test_missing_time_range_raises(self) -> None:
        from app.access.exception import InvalidTimeRangeError
        from app.access.schema import AccessQueryRequest
        from app.access.service import query_events

        redis = _make_redis()
        req = AccessQueryRequest()  # no time_range

        with pytest.raises(InvalidTimeRangeError):
            await query_events(redis, req)

    @pytest.mark.asyncio
    async def test_next_cursor_set_when_more_rows(self) -> None:
        from app.access.schema import AccessQueryRequest
        from app.access.service import query_events

        redis = _make_redis()
        req = AccessQueryRequest(time_range=_make_time_range())

        # Build minimal AccessEventView objects for testing
        minimal_kwargs = {
            "log_id": "lid",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "a",
            "trace_id": "",
            "span_id": "",
            "parent_span_id": "",
            "correlation_id": "",
            "severity": "",
            "duration_ms": 0,
            "request_method": "",
            "request_route": "",
            "request_url": "",
            "request_size": 0,
            "response_status": 0,
            "response_size": 0,
            "caller_aic": "",
            "caller_service": "",
            "caller_ip": "",
            "callee_aic": "",
            "callee_service": "",
            "callee_ip": "",
            "error_code": "",
            "error_message": "",
            "service_name": "",
            "deployment_env": "",
            "request_headers": {},
            "response_headers": {},
            "attributes": {},
        }
        from app.access.schema import AccessEventView

        # Return limit+1 rows to trigger next_cursor
        mock_rows = [AccessEventView(**{**minimal_kwargs, "log_id": f"lid-{i}"}) for i in range(51)]

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_events_query = AsyncMock(return_value=mock_rows)
            _, meta = await query_events(redis, req)

        assert meta.next_cursor is not None


# ── service.query_operations ──────────────────────────────────────────────────


class TestQueryOperations:
    @pytest.mark.asyncio
    async def test_returns_summaries_and_meta(self) -> None:
        from app.access.schema import AccessOperationQueryRequest
        from app.access.service import query_operations
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessOperationQueryRequest(time_range=_make_time_range())

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_operations_query = AsyncMock(return_value=[])
            rows, meta = await query_operations(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)

    @pytest.mark.asyncio
    async def test_next_cursor_set_when_more_rows(self) -> None:
        """store 返回 limit+1 行时，meta.next_cursor 应不为 None（游标分页 §5.3）。"""
        from app.access.schema import AccessOperationQueryRequest, AccessOperationSummary
        from app.access.service import query_operations

        redis = _make_redis()
        req = AccessOperationQueryRequest(time_range=_make_time_range())

        def _make_op_row(i: int) -> AccessOperationSummary:
            return AccessOperationSummary(
                dimensions={},
                request_count=i,
                error_count=0,
                error_rate=0.0,
                avg_duration_ms=0.0,
                p95_duration_ms=0.0,
                p99_duration_ms=0.0,
                last_seen_at="2026-01-15T10:00:00Z",
            )

        limit = req.page.limit if req.page else 50
        mock_rows = [_make_op_row(i) for i in range(limit + 1)]

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_operations_query = AsyncMock(return_value=mock_rows)
            rows, meta = await query_operations(redis, req)

        assert len(rows) == limit
        assert meta.next_cursor is not None, "store 返回超额行时 next_cursor 应被设置"


# ── service.query_error_attribution ──────────────────────────────────────────


class TestQueryErrorAttribution:
    @pytest.mark.asyncio
    async def test_returns_attributions_and_meta(self) -> None:
        from app.access.schema import AccessErrorAttributionRequest
        from app.access.service import query_error_attribution
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessErrorAttributionRequest(time_range=_make_time_range())

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_error_attribution = AsyncMock(return_value=[])
            rows, meta = await query_error_attribution(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)


# ── service.query_slow_requests ───────────────────────────────────────────────


class TestQuerySlowRequests:
    @pytest.mark.asyncio
    async def test_returns_items_and_meta(self) -> None:
        from app.access.schema import AccessSlowRequestRequest
        from app.access.service import query_slow_requests
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessSlowRequestRequest(time_range=_make_time_range())

        with (
            patch("app.access.service.store") as mock_store,
            patch("app.access.service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_slow_requests = AsyncMock(return_value=[])
            rows, meta = await query_slow_requests(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)


# ── trace_service.query_traces ────────────────────────────────────────────────


class TestQueryTraces:
    @pytest.mark.asyncio
    async def test_returns_trace_summaries_and_meta(self) -> None:
        from app.access.schema import AccessTraceQueryRequest
        from app.access.trace_service import query_traces
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessTraceQueryRequest(time_range=_make_time_range())

        with (
            patch("app.access.trace_service.store") as mock_store,
            patch("app.access.trace_service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_traces_query = AsyncMock(return_value=[])
            rows, meta = await query_traces(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)


class TestGetTrace:
    @pytest.mark.asyncio
    async def test_not_found_raises(self) -> None:
        from app.access.exception import TraceNotFoundError
        from app.access.trace_service import get_trace

        redis = _make_redis()
        with (
            patch("app.access.trace_service.store") as mock_store,
            patch("app.access.trace_service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.fetch_trace_spans = AsyncMock(return_value=([], False))
            with pytest.raises(TraceNotFoundError):
                await get_trace(redis, "nonexistent-trace", include_events=False)

    @pytest.mark.asyncio
    async def test_returns_trace_view_and_headers(self) -> None:
        from app.access.schema import AccessTraceSpan, AccessTraceView
        from app.access.trace_service import get_trace

        redis = _make_redis()
        span = AccessTraceSpan(
            log_id="lid",
            timestamp="2026-01-01T00:00:00Z",
            aic="a",
            trace_id="t1",
            span_id="s1",
            parent_span_id="",
            duration_ms=100,
            request_method="GET",
            request_route="/h",
            response_status=200,
            caller_aic="",
            callee_aic="a",
            error_code="",
            service_name="svc",
        )
        with (
            patch("app.access.trace_service.store") as mock_store,
            patch("app.access.trace_service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.fetch_trace_spans = AsyncMock(return_value=([span], False))
            mock_store.fetch_trace_events = AsyncMock(return_value=[])
            view, headers = await get_trace(redis, "t1", include_events=False)

        assert isinstance(view, AccessTraceView)
        assert view.trace_id == "t1"
        assert isinstance(headers, dict)

    @pytest.mark.asyncio
    async def test_truncated_sets_header(self) -> None:
        from app.access.schema import AccessTraceSpan
        from app.access.trace_service import get_trace

        redis = _make_redis()
        span = AccessTraceSpan(
            log_id="lid",
            timestamp="2026-01-01T00:00:00Z",
            aic="a",
            trace_id="t1",
            span_id="s1",
            parent_span_id="",
            duration_ms=100,
            request_method="GET",
            request_route="/h",
            response_status=200,
            caller_aic="",
            callee_aic="a",
            error_code="",
            service_name="svc",
        )
        with (
            patch("app.access.trace_service.store") as mock_store,
            patch("app.access.trace_service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.fetch_trace_spans = AsyncMock(return_value=([span], True))  # truncated=True
            _, headers = await get_trace(redis, "t1", include_events=False)

        assert headers.get("AMP-Trace-Truncated") == "true"


# ── topology_service.query_topology ──────────────────────────────────────────


class TestQueryTopology:
    @pytest.mark.asyncio
    async def test_returns_edges_and_meta(self) -> None:
        from app.access.schema import AccessTopologyQueryRequest
        from app.access.topology_service import query_topology
        from app.core.amp_api_schema import AMPResponseMeta

        redis = _make_redis()
        req = AccessTopologyQueryRequest(time_range=_make_time_range())

        with (
            patch("app.access.topology_service.store") as mock_store,
            patch("app.access.topology_service.evaluate_freshness", AsyncMock(return_value=_make_freshness_view())),
        ):
            mock_store.run_topology_query = AsyncMock(return_value=[])
            rows, meta = await query_topology(redis, req)

        assert isinstance(rows, list)
        assert isinstance(meta, AMPResponseMeta)

    @pytest.mark.asyncio
    async def test_missing_time_range_raises(self) -> None:
        from app.access.exception import InvalidTimeRangeError
        from app.access.schema import AccessTopologyQueryRequest
        from app.access.topology_service import query_topology

        redis = _make_redis()
        req = AccessTopologyQueryRequest()  # no time_range

        with pytest.raises(InvalidTimeRangeError):
            await query_topology(redis, req)
