"""tests/unit/test_access_store.py — ClickHouse 读写执行层测试。

TDD C-4：先写测试（红）→ 实现 store.py（绿）。
store.py 依赖 ClickHouse I/O，全部用 Mock 替代真实 CH。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_ch_mock() -> Any:
    return AsyncMock()


class TestEnsureAccessSchema:
    @pytest.mark.asyncio
    async def test_executes_all_ddl_statements(self) -> None:
        from app.access.store import ensure_access_schema

        mock_client = _make_ch_mock()
        mock_client.command = AsyncMock()

        with (
            patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.store.settings") as mock_s,
        ):
            mock_s.access_raw_retention_days = 30
            mock_s.access_topology_retention_days = 90
            mock_s.access_error_status_threshold = 500
            await ensure_access_schema()

        assert mock_client.command.call_count == 5  # 3 tables + 2 MVs

    @pytest.mark.asyncio
    async def test_idempotent_no_raise_on_if_not_exists(self) -> None:
        from app.access.store import ensure_access_schema

        mock_client = _make_ch_mock()
        mock_client.command = AsyncMock()
        with (
            patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.store.settings") as mock_s,
        ):
            mock_s.access_raw_retention_days = 30
            mock_s.access_topology_retention_days = 90
            mock_s.access_error_status_threshold = 500
            await ensure_access_schema()
            await ensure_access_schema()
        assert mock_client.command.call_count == 10  # called twice


class TestInsertEvents:
    @pytest.mark.asyncio
    async def test_inserts_to_access_events(self) -> None:
        from app.access.events import EventRow
        from app.access.store import insert_events
        from app.access.tables import ACCESS_EVENTS

        mock_client = _make_ch_mock()
        mock_client.insert = AsyncMock()

        row = EventRow(
            log_id="lid",
            timestamp_ms=0,
            observed_at_ms=0,
            aic="a",
            trace_id="",
            span_id="",
            parent_span_id="",
            correlation_id="",
            severity="",
            duration_ms=0,
            request_method="",
            request_route="",
            request_url="",
            request_size=0,
            response_status=0,
            response_size=0,
            caller_aic="",
            caller_service="",
            caller_ip="",
            callee_aic="",
            callee_service="",
            callee_ip="",
            error_code="",
            error_message="",
            service_name="",
            deployment_env="",
            request_headers={},
            response_headers={},
            attributes={},
            raw_log="",
        )

        with patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)):
            await insert_events([row])

        mock_client.insert.assert_called_once()
        call_args = mock_client.insert.call_args
        assert call_args[0][0] == ACCESS_EVENTS

    @pytest.mark.asyncio
    async def test_ch_error_raises_insert_error(self) -> None:
        from app.access.events import EventRow
        from app.access.exception import ClickHouseInsertError
        from app.access.store import insert_events

        mock_client = _make_ch_mock()
        mock_client.insert = AsyncMock(side_effect=Exception("CH unavailable"))

        row = EventRow(
            log_id="lid",
            timestamp_ms=0,
            observed_at_ms=0,
            aic="a",
            trace_id="",
            span_id="",
            parent_span_id="",
            correlation_id="",
            severity="",
            duration_ms=0,
            request_method="",
            request_route="",
            request_url="",
            request_size=0,
            response_status=0,
            response_size=0,
            caller_aic="",
            caller_service="",
            caller_ip="",
            callee_aic="",
            callee_service="",
            callee_ip="",
            error_code="",
            error_message="",
            service_name="",
            deployment_env="",
            request_headers={},
            response_headers={},
            attributes={},
            raw_log="",
        )

        with (
            patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            pytest.raises(ClickHouseInsertError),
        ):
            await insert_events([row])


class TestRunEventsQuery:
    @pytest.mark.asyncio
    async def test_returns_list_of_event_views(self) -> None:
        from app.access.store import run_events_query

        mock_client = _make_ch_mock()
        # Build a row that matches EVENT_VIEW_COLUMNS order (28 cols)
        mock_row: tuple[Any, ...] = (
            "lid1",
            "2026-01-01T00:00:00Z",
            "aic-1",
            "t1",
            "s1",
            "",
            "",
            "INFO",
            100,
            "GET",
            "/health",
            "/health",
            0,
            200,
            50,
            "",
            "",
            "",
            "aic-1",
            "svc",
            "1.2.3.4",
            "",
            "",
            "svc",
            "prod",
            {},
            {},
            {},
        )
        mock_result = MagicMock()
        mock_result.result_rows = [mock_row]
        mock_client.query = AsyncMock(return_value=mock_result)

        with (
            patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.store.settings") as mock_s,
        ):
            mock_s.access_query_timeout_seconds = 30
            rows = await run_events_query(("SELECT 1", {}), limit=50, include_raw_log=False)

        assert isinstance(rows, list)


class TestRunTopologyQuery:
    @pytest.mark.asyncio
    async def test_fills_grouped_by(self) -> None:
        from app.access.store import run_topology_query

        mock_client = _make_ch_mock()
        # Mock row: (bucket_or_null, caller_aic, caller_service, callee_aic, callee_service,
        #             call_count, error_count, avg_duration_ms, p_quantiles, last_seen_at)
        mock_row: tuple[Any, ...] = (
            None,
            "caller-a",
            "svc-a",
            "callee-b",
            "svc-b",
            100,
            5,
            50.0,
            [80.0, 95.0],
            "2026-01-01T00:00:00Z",
        )
        mock_result = MagicMock()
        mock_result.result_rows = [mock_row]
        mock_client.query = AsyncMock(return_value=mock_result)

        with (
            patch("app.access.store.get_clickhouse_client", AsyncMock(return_value=mock_client)),
            patch("app.access.store.settings") as mock_s,
        ):
            mock_s.access_query_timeout_seconds = 30
            rows = await run_topology_query(("SELECT 1", {}), group_by="aic")

        assert isinstance(rows, list)
