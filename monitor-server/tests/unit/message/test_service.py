"""单元测试：F-1 service.py — query_events 编排（mock IO）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.message.freshness import FreshnessView
from app.message.schema import MessageEventView


def _make_redis() -> MagicMock:
    return MagicMock()


def _recent_time_range() -> object:
    """生成当前时间前 30min 至前 5min 的时间范围（落在 raw_retention_days=1 内）。"""
    from datetime import UTC, datetime, timedelta

    from app.core.amp_api_schema import AMPTimeRange

    now = datetime.now(UTC)
    return AMPTimeRange(
        start_at=(now - timedelta(minutes=30)).isoformat(),
        end_at=(now - timedelta(minutes=5)).isoformat(),
    )


def _make_event_view(**kwargs: object) -> MessageEventView:
    base: dict[str, object] = {
        "logId": "log-001",
        "timestamp": "2026-06-01T00:00:00+00:00",
        "direction": "send",
        "eventType": "send",
        "system": "kafka",
        "destinationName": "my-topic",
        "destinationKind": "topic",
    }
    base.update(kwargs)
    return MessageEventView(**base)


class TestQueryEvents:
    @pytest.mark.asyncio
    async def test_returns_views_and_meta(self) -> None:
        from app.message.schema import MessageQueryRequest
        from app.message.service import query_events

        redis = _make_redis()
        req = MessageQueryRequest(timeRange=_recent_time_range())
        views = [_make_event_view()]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch("app.message.service.store.run_events_query", AsyncMock(return_value=views)),
            patch("app.message.service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            items, meta = await query_events(redis, req)
        assert len(items) == 1
        assert meta.data_freshness_at is not None

    @pytest.mark.asyncio
    async def test_store_error_raises_read_model_lagging(self) -> None:
        from app.message.exception import ReadModelLaggingError
        from app.message.schema import MessageQueryRequest
        from app.message.service import query_events

        redis = _make_redis()
        req = MessageQueryRequest(timeRange=_recent_time_range())
        with (
            patch("app.message.service.store.run_events_query", AsyncMock(side_effect=Exception("CH down"))),
            pytest.raises(ReadModelLaggingError),
        ):
            await query_events(redis, req)

    @pytest.mark.asyncio
    async def test_next_cursor_set_when_has_more(self) -> None:
        from app.message.schema import MessageQueryRequest
        from app.message.service import query_events

        redis = _make_redis()
        req = MessageQueryRequest(timeRange=_recent_time_range())
        # Return limit+1 rows to trigger has_more
        views = [_make_event_view(logId=f"log-{i:03d}") for i in range(51)]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch("app.message.service.store.run_events_query", AsyncMock(return_value=views)),
            patch("app.message.service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            items, meta = await query_events(redis, req)
        assert len(items) == 50
        assert meta.next_cursor is not None

    @pytest.mark.asyncio
    async def test_out_of_retention_raises(self) -> None:
        from app.core.amp_api_schema import AMPTimeRange
        from app.message.exception import OutOfRetentionError
        from app.message.schema import MessageQueryRequest
        from app.message.service import query_events

        redis = _make_redis()
        req = MessageQueryRequest(
            timeRange=AMPTimeRange(start_at="2000-01-01T00:00:00Z", end_at="2000-01-02T00:00:00Z")
        )
        with pytest.raises(OutOfRetentionError):
            await query_events(redis, req)
