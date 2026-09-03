"""单元测试：F-3 destination_service.py — Destination Profile 编排（mock IO）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.message.freshness import FreshnessView
from app.message.schema import MessageDestinationStateView, MessageThroughputPoint, MessageThroughputSeries


def _make_redis() -> MagicMock:
    return MagicMock()


def _recent_time_range() -> object:
    from datetime import UTC, datetime, timedelta

    from app.core.amp_api_schema import AMPTimeRange

    now = datetime.now(UTC)
    return AMPTimeRange(
        start_at=(now - timedelta(hours=2)).isoformat(),
        end_at=(now - timedelta(minutes=5)).isoformat(),
    )


def _make_dest_view(**kwargs: object) -> MessageDestinationStateView:
    base: dict[str, object] = {"capturedAt": "2026-06-01T00:00:00+00:00"}
    base.update(kwargs)
    return MessageDestinationStateView(**base)


class TestQueryDestinationStates:
    @pytest.mark.asyncio
    async def test_returns_views_and_meta(self) -> None:
        from app.message.destination_service import query_destination_states
        from app.message.schema import MessageDestinationStateQueryRequest

        redis = _make_redis()
        req = MessageDestinationStateQueryRequest(timeRange=_recent_time_range())
        views = [_make_dest_view()]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch(
                "app.message.destination_service.store.run_destinations_query", AsyncMock(return_value=(views, [], {}))
            ),
            patch("app.message.destination_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            items, _meta = await query_destination_states(redis, req)
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_empty_result_raises_state_snapshot_unavailable(self) -> None:
        from app.message.destination_service import query_destination_states
        from app.message.exception import StateSnapshotUnavailableError
        from app.message.schema import MessageDestinationStateQueryRequest

        redis = _make_redis()
        req = MessageDestinationStateQueryRequest(timeRange=_recent_time_range())
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with (
            patch("app.message.destination_service.store.run_destinations_query", AsyncMock(return_value=([], [], {}))),
            patch("app.message.destination_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
            pytest.raises(StateSnapshotUnavailableError),
        ):
            await query_destination_states(redis, req)

    @pytest.mark.asyncio
    async def test_partial_data_fields_in_meta(self) -> None:
        from app.message.destination_service import query_destination_states
        from app.message.schema import MessageDestinationStateQueryRequest

        redis = _make_redis()
        req = MessageDestinationStateQueryRequest(timeRange=_recent_time_range())
        views = [_make_dest_view()]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch(
                "app.message.destination_service.store.run_destinations_query",
                AsyncMock(return_value=(views, ["visible_messages"], {})),
            ),
            patch("app.message.destination_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            _items, meta = await query_destination_states(redis, req)
        assert meta.partial_data_fields == ["visible_messages"]


class TestGetThroughput:
    @pytest.mark.asyncio
    async def test_returns_series_and_headers(self) -> None:
        from datetime import UTC, datetime

        from app.message.destination_service import get_throughput
        from app.message.schema import MessageThroughputRequest

        redis = _make_redis()
        req = MessageThroughputRequest(
            timeRange=_recent_time_range(),
            system="kafka",
            destinationName="my-topic",
        )
        point = MessageThroughputPoint(
            bucket=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
            produced_count=10,
            consumed_count=8,
        )
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch("app.message.destination_service.store.run_throughput_query", AsyncMock(return_value=[point])),
            patch("app.message.destination_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            series, _headers = await get_throughput(redis, req)
        assert isinstance(series, MessageThroughputSeries)
        assert len(series.points) == 1

    @pytest.mark.asyncio
    async def test_missing_destination_raises(self) -> None:
        from app.message.destination_service import get_throughput
        from app.message.exception import MessageDestinationRequiredError
        from app.message.schema import MessageThroughputRequest

        redis = _make_redis()
        req = MessageThroughputRequest(timeRange=_recent_time_range())
        with pytest.raises(MessageDestinationRequiredError):
            await get_throughput(redis, req)
