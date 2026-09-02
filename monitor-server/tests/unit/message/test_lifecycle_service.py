"""单元测试：F-2 lifecycle_service.py — Reliability Profile 编排（mock IO）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.message.freshness import FreshnessView
from app.message.schema import MessageDeadLetterView, MessageLifecycleView


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


def _make_lifecycle_view(**kwargs: object) -> MessageLifecycleView:
    base: dict[str, object] = {
        "lifecycleKey": "mid:abc",
        "system": "kafka",
        "destinationName": "my-topic",
        "destinationKind": "topic",
        "firstSeenAt": "2026-06-01T00:00:00+00:00",
        "lastSeenAt": "2026-06-01T00:01:00+00:00",
        "producerAics": ["svc-a"],
        "consumerAics": ["svc-b"],
        "sendCount": 1,
        "receiveCount": 1,
        "deadLettered": False,
        "duplicateConsumed": False,
        "unacked": False,
    }
    base.update(kwargs)
    return MessageLifecycleView(**base)


def _make_dl_view(**kwargs: object) -> MessageDeadLetterView:
    base: dict[str, object] = {
        "lifecycleKey": "mid:abc",
        "system": "kafka",
        "destinationName": "my-topic",
        "destinationKind": "topic",
        "receiveCount": 3,
        "producerAics": [],
        "consumerAics": [],
    }
    base.update(kwargs)
    return MessageDeadLetterView(**base)


class TestQueryLifecycles:
    @pytest.mark.asyncio
    async def test_returns_views_and_meta(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.message.lifecycle_service import query_lifecycles
        from app.message.schema import MessageLifecycleQueryRequest

        redis = _make_redis()
        req = MessageLifecycleQueryRequest(
            timeRange=_recent_time_range(),
            filter=AMPFilter(
                conditions=[AMPFilterCondition(field="messageId", op="eq", value="abc")],
                logic="and",
            ),
        )
        views = [_make_lifecycle_view()]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch("app.message.lifecycle_service.store.run_lifecycles_query", AsyncMock(return_value=views)),
            patch("app.message.lifecycle_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            items, _meta = await query_lifecycles(redis, req)
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_missing_selectivity_raises(self) -> None:
        from app.message.exception import LifecycleKeyRequiredError
        from app.message.lifecycle_service import query_lifecycles
        from app.message.schema import MessageLifecycleQueryRequest

        redis = _make_redis()
        req = MessageLifecycleQueryRequest(timeRange=_recent_time_range())
        with pytest.raises(LifecycleKeyRequiredError):
            await query_lifecycles(redis, req)

    @pytest.mark.asyncio
    async def test_store_error_raises_read_model_lagging(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.message.exception import ReadModelLaggingError
        from app.message.lifecycle_service import query_lifecycles
        from app.message.schema import MessageLifecycleQueryRequest

        redis = _make_redis()
        req = MessageLifecycleQueryRequest(
            timeRange=_recent_time_range(),
            filter=AMPFilter(
                conditions=[AMPFilterCondition(field="messageId", op="eq", value="abc")],
                logic="and",
            ),
        )
        with (
            patch("app.message.lifecycle_service.store.run_lifecycles_query", AsyncMock(side_effect=Exception("CH"))),
            pytest.raises(ReadModelLaggingError),
        ):
            await query_lifecycles(redis, req)


class TestGetLifecycleByMessageId:
    @pytest.mark.asyncio
    async def test_returns_view_and_headers(self) -> None:
        from app.message.lifecycle_service import get_lifecycle_by_message_id
        from app.message.schema import MessageLifecycleDetailView

        redis = _make_redis()
        detail = MessageLifecycleDetailView(
            lifecycle_key="mid:abc",
            system="kafka",
            destination_name="my-topic",
            destination_kind="topic",
            first_seen_at="2026-06-01T00:00:00+00:00",
            last_seen_at="2026-06-01T00:01:00+00:00",
            producer_aics=[],
            consumer_aics=[],
            send_count=1,
            receive_count=1,
            dead_lettered=False,
            duplicate_consumed=False,
            unacked=False,
        )
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch(
                "app.message.lifecycle_service.store.fetch_lifecycle_by_message_id", AsyncMock(return_value=[detail])
            ),
            patch("app.message.lifecycle_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            view, _headers = await get_lifecycle_by_message_id(redis, "abc")
        assert view.lifecycle_key == "mid:abc"

    @pytest.mark.asyncio
    async def test_not_found_raises(self) -> None:
        from app.message.exception import MessageNotFoundError
        from app.message.lifecycle_service import get_lifecycle_by_message_id

        redis = _make_redis()
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with (
            patch("app.message.lifecycle_service.store.fetch_lifecycle_by_message_id", AsyncMock(return_value=[])),
            patch("app.message.lifecycle_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
            pytest.raises(MessageNotFoundError),
        ):
            await get_lifecycle_by_message_id(redis, "not-found")

    @pytest.mark.asyncio
    async def test_ambiguous_raises(self) -> None:
        from app.message.exception import LifecycleAmbiguousError
        from app.message.lifecycle_service import get_lifecycle_by_message_id
        from app.message.schema import MessageLifecycleDetailView

        redis = _make_redis()
        detail = MessageLifecycleDetailView(
            lifecycle_key="mid:abc",
            system="kafka",
            destination_name="my-topic",
            destination_kind="topic",
            first_seen_at="2026-06-01T00:00:00+00:00",
            last_seen_at="2026-06-01T00:01:00+00:00",
            producer_aics=[],
            consumer_aics=[],
            send_count=1,
            receive_count=1,
            dead_lettered=False,
            duplicate_consumed=False,
            unacked=False,
        )
        fv = FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)
        with (
            patch(
                "app.message.lifecycle_service.store.fetch_lifecycle_by_message_id",
                AsyncMock(return_value=[detail, detail]),
            ),
            patch("app.message.lifecycle_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
            pytest.raises(LifecycleAmbiguousError),
        ):
            await get_lifecycle_by_message_id(redis, "abc")


class TestQueryDeadletters:
    @pytest.mark.asyncio
    async def test_returns_views_and_meta(self) -> None:
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.message.lifecycle_service import query_deadletters
        from app.message.schema import MessageDeadletterQueryRequest

        redis = _make_redis()
        req = MessageDeadletterQueryRequest(
            timeRange=_recent_time_range(),
            filter=AMPFilter(
                conditions=[AMPFilterCondition(field="messageId", op="eq", value="abc")],
                logic="and",
            ),
        )
        views = [_make_dl_view()]
        fv = FreshnessView(data_freshness_at_ms=1_000_000, ingestion_lag_ms=100, lagging=False)
        with (
            patch("app.message.lifecycle_service.store.run_deadletters_query", AsyncMock(return_value=views)),
            patch("app.message.lifecycle_service.freshness.evaluate_freshness", AsyncMock(return_value=fv)),
        ):
            items, _meta = await query_deadletters(redis, req)
        assert len(items) == 1
