"""tests/unit/system/test_service.py — service.py 单元测试（mock store/freshness）。"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.amp_api_schema import AMPPaginationRequest, AMPTimeRange
from app.system.exception import (
    CursorInvalidError,
    ReadModelLaggingError,
    SystemKeywordTooBroadError,
)
from app.system.freshness import FreshnessView
from app.system.schema import SystemEventQueryRequest, SystemEventView
from app.system.service import query_events
from app.system.store import SystemEventHit


def _make_redis() -> AsyncMock:
    return AsyncMock()


def _make_view(log_id: str = "log-001") -> SystemEventView:
    return SystemEventView(
        log_id=log_id,
        timestamp="2024-06-14T12:00:00Z",
        aic="aic-001",
        severity_number=0,
        message="test",
    )


def _make_hit(log_id: str = "log-001") -> SystemEventHit:
    return SystemEventHit(view=_make_view(log_id), sort_values=[1718323200000, log_id])


def _make_fresh_view() -> FreshnessView:
    return FreshnessView(
        data_freshness_at_ms=int(time.time() * 1000) - 5000,
        ingestion_lag_ms=5000,
        lagging=False,
    )


def _make_lagging_view() -> FreshnessView:
    return FreshnessView(data_freshness_at_ms=None, ingestion_lag_ms=None, lagging=True)


def _patch_settings(**kwargs: Any) -> dict[str, Any]:
    defaults = {
        "system_archive_retention_days": 90,
        "system_keyword_min_length": 2,
        "system_keyword_only_max_window_seconds": 3600,
        "system_lagging_threshold_ms": 300000,
        "system_lagging_response_mode": "partial",
        "system_pit_keep_alive": "5m",
    }
    defaults.update(kwargs)
    return defaults


@pytest.fixture
def mock_settings() -> Generator[MagicMock]:
    vals = _patch_settings()
    with patch("app.core.config.settings") as m:
        for k, v in vals.items():
            setattr(m, k, v)
        yield m


@pytest.fixture
def now_ms_fixed() -> int:
    return int(time.time() * 1000)


def _make_req(
    start_at: str | None = None,
    end_at: str | None = None,
    keyword: str | None = None,
    cursor: str | None = None,
    limit: int = 10,
) -> SystemEventQueryRequest:
    if start_at is None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        start_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return SystemEventQueryRequest(
        time_range=AMPTimeRange(start_at=start_at, end_at=end_at),
        keyword=keyword,
        page=AMPPaginationRequest(limit=limit, cursor=cursor),
    )


class TestQueryEventsOrchestration:
    @pytest.mark.asyncio
    async def test_basic_query_returns_rows_and_meta(self, mock_settings: MagicMock) -> None:
        redis = _make_redis()
        req = _make_req()

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", return_value=[_make_hit()]),
            patch("app.system.store.close_pit"),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
            patch("app.system.freshness.read_watermark", return_value=None),
        ):
            rows, meta = await query_events(redis, req)

        assert isinstance(rows, list)
        assert meta is not None

    @pytest.mark.asyncio
    async def test_first_page_opens_pit(self, mock_settings: MagicMock) -> None:
        """首页（cursor=None）→ open_pit 被调用。"""
        redis = _make_redis()
        req = _make_req()
        mock_open_pit = AsyncMock(return_value="pit-001")

        with (
            patch("app.system.store.open_pit", mock_open_pit),
            patch("app.system.store.search_events", return_value=[]),
            patch("app.system.store.close_pit", AsyncMock()),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            await query_events(redis, req)

        mock_open_pit.assert_called_once()

    @pytest.mark.asyncio
    async def test_last_page_closes_pit(self, mock_settings: MagicMock) -> None:
        """末页（无更多结果）→ close_pit 被调用。"""
        redis = _make_redis()
        req = _make_req(limit=10)
        mock_close_pit = AsyncMock()

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", return_value=[_make_hit()]),  # < limit
            patch("app.system.store.close_pit", mock_close_pit),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            await query_events(redis, req)

        mock_close_pit.assert_called_once()

    @pytest.mark.asyncio
    async def test_has_more_generates_next_cursor(self, mock_settings: MagicMock) -> None:
        """结果数 > limit → has_more=True，nextCursor 生成。"""
        redis = _make_redis()
        req = _make_req(limit=1)
        # 返回 2 条（>limit=1）
        hits = [_make_hit("log-001"), _make_hit("log-002")]

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", return_value=hits),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            rows, meta = await query_events(redis, req)

        assert len(rows) == 1
        assert meta.next_cursor is not None

    @pytest.mark.asyncio
    async def test_pit_expired_raises_cursor_invalid(self, mock_settings: MagicMock) -> None:
        """PIT 失效 → CursorInvalidError（C-SYSTEM-QUERY-5）。"""
        redis = _make_redis()
        req = _make_req()
        from app.system.exception import OpenSearchQueryError

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", side_effect=OpenSearchQueryError("pit expired")),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            with pytest.raises(CursorInvalidError):
                await query_events(redis, req)

    @pytest.mark.asyncio
    async def test_search_timeout_raises_read_model_lagging(self, mock_settings: MagicMock) -> None:
        """搜索超时 → ReadModelLaggingError(503)。"""
        redis = _make_redis()
        req = _make_req()
        from app.system.exception import OpenSearchQueryError

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", side_effect=OpenSearchQueryError("connection timeout")),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            with pytest.raises(ReadModelLaggingError):
                await query_events(redis, req)

    @pytest.mark.asyncio
    async def test_keyword_too_broad_raises_before_search(self, mock_settings: MagicMock) -> None:
        """keyword 过宽 → 在搜索前拦截（护栏先于 open_pit）。"""
        redis = _make_redis()
        # 单字符 keyword，min_length=2 → 触发护栏
        req = _make_req(keyword="x")
        mock_open_pit = AsyncMock(return_value="pit-001")

        with patch("app.system.store.open_pit", mock_open_pit), pytest.raises(SystemKeywordTooBroadError):
            await query_events(redis, req)

        mock_open_pit.assert_not_called()

    @pytest.mark.asyncio
    async def test_include_raw_log_passed_to_search_events(self, mock_settings: MagicMock) -> None:
        """include_raw_log 透传到 store.search_events。"""
        redis = _make_redis()
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        start_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        req = SystemEventQueryRequest(
            time_range=AMPTimeRange(start_at=start_at, end_at=end_at),
            include_raw_log=True,
        )
        mock_search = AsyncMock(return_value=[])

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", mock_search),
            patch("app.system.store.close_pit", AsyncMock()),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_fresh_view()),
        ):
            await query_events(redis, req)

        call_kwargs = mock_search.call_args
        assert call_kwargs.kwargs.get("include_raw_log") is True

    @pytest.mark.asyncio
    async def test_lagging_partial_mode_sets_meta_partial(self, mock_settings: MagicMock) -> None:
        """滞后 + partial 模式 → meta.partial=True（不 503）。"""
        mock_settings.system_lagging_response_mode = "partial"
        redis = _make_redis()
        req = _make_req()

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", return_value=[]),
            patch("app.system.store.close_pit", AsyncMock()),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_lagging_view()),
        ):
            _, meta = await query_events(redis, req)

        assert meta.partial is True

    @pytest.mark.asyncio
    async def test_lagging_strict_503_mode_raises(self, mock_settings: MagicMock) -> None:
        """滞后 + 503 模式 → ReadModelLaggingError(503)。"""
        mock_settings.system_lagging_response_mode = "503"
        redis = _make_redis()
        req = _make_req()

        with (
            patch("app.system.store.open_pit", return_value="pit-001"),
            patch("app.system.store.search_events", return_value=[]),
            patch("app.system.store.close_pit", AsyncMock()),
            patch("app.system.freshness.evaluate_freshness", return_value=_make_lagging_view()),
        ):
            with pytest.raises(ReadModelLaggingError):
                await query_events(redis, req)
