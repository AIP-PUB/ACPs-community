"""tests/unit/test_heartbeat_service.py — HeartbeatService 业务逻辑单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

NOW_MS = 1_700_000_000_000
NOW_ISO = "2023-11-14T22:13:20+00:00"


def _make_entry(
    aic: str = "aic-001",
    last_seen_at_ms: int = NOW_MS - 1000,
    last_seen_at: str = "2023-11-14T22:13:19+00:00",
    source_timestamp_ms: int | None = None,
    alive_membership_state: str = "alive",
) -> MagicMock:
    e = MagicMock()
    e.aic = aic
    e.last_seen_at_ms = last_seen_at_ms
    e.last_seen_at = last_seen_at
    e.source_timestamp_ms = source_timestamp_ms
    e.alive_membership_state = alive_membership_state
    return e


def _make_fresh_view(
    min_watermark_ms: int = NOW_MS - 500,
    lagging: int = 0,
    all_unknown: bool = False,
) -> MagicMock:
    v = MagicMock()
    v.min_watermark_ms = min_watermark_ms
    v.lagging_partition_count = lagging
    v.all_unknown = all_unknown
    return v


# ── _build_view ───────────────────────────────────────────────────────────────


class TestBuildView:
    def test_alive_within_threshold(self) -> None:
        """silence_ms <= silence_threshold_ms（含界）→ is_alive=True（7-1）。"""
        from app.core.config import settings
        from app.heartbeat.service import _build_view

        threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000
        entry = _make_entry(last_seen_at_ms=NOW_MS - threshold_ms)  # 含界
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.is_alive is True

    def test_silent_beyond_threshold(self) -> None:
        """silence_ms > silence_threshold_ms → is_alive=False。"""
        from app.core.config import settings
        from app.heartbeat.service import _build_view

        threshold_ms = settings.heartbeat_silence_threshold_seconds * 1000
        entry = _make_entry(last_seen_at_ms=NOW_MS - threshold_ms - 1)
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.is_alive is False
        assert view.liveness_state == "silent"

    def test_alive_sets_liveness_state(self) -> None:
        """is_alive=True 时 liveness_state="alive"。"""
        from app.heartbeat.service import _build_view

        entry = _make_entry(last_seen_at_ms=NOW_MS - 500)  # fresh
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.liveness_state == "alive"

    def test_silence_duration_zero_when_fresh(self) -> None:
        """刚刚心跳的 AIC，silence_duration_seconds=0。"""
        from app.heartbeat.service import _build_view

        entry = _make_entry(last_seen_at_ms=NOW_MS)
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.silence_duration_seconds == 0

    def test_silence_duration_floor_division(self) -> None:
        """silence_duration_seconds 向下取整（§schema 约定）。"""
        from app.heartbeat.service import _build_view

        entry = _make_entry(last_seen_at_ms=NOW_MS - 5999)  # 5.999s → 5
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.silence_duration_seconds == 5

    def test_source_timestamp_none_when_missing(self) -> None:
        """entry.source_timestamp_ms=None → view.source_timestamp=None。"""
        from app.heartbeat.service import _build_view

        entry = _make_entry(source_timestamp_ms=None)
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.source_timestamp is None

    def test_source_timestamp_formatted_as_iso(self) -> None:
        """entry.source_timestamp_ms 非 None → 格式化为 ISO 字符串。"""
        from app.heartbeat.service import _build_view

        entry = _make_entry(source_timestamp_ms=NOW_MS - 100)
        view = _build_view(entry, now_ms=NOW_MS)
        assert view.source_timestamp is not None
        assert "T" in view.source_timestamp


# ── _build_meta ───────────────────────────────────────────────────────────────


class TestBuildMeta:
    def test_evaluated_at_iso(self) -> None:
        """evaluated_at 是 now_ms 对应的 ISO 字符串。"""
        from app.heartbeat.service import _build_meta

        fresh = _make_fresh_view()
        meta = _build_meta(now_ms=NOW_MS, fresh=fresh)
        assert "T" in meta.evaluated_at

    def test_data_freshness_at_iso(self) -> None:
        """data_freshness_at 是 min_watermark_ms 对应的 ISO 字符串。"""
        from app.heartbeat.service import _build_meta

        fresh = _make_fresh_view(min_watermark_ms=NOW_MS - 2000)
        meta = _build_meta(now_ms=NOW_MS, fresh=fresh)
        assert meta.data_freshness_at is not None
        assert "T" in meta.data_freshness_at

    def test_partial_true_when_lagging(self) -> None:
        """有 lagging 分区时，meta.partial=True。"""
        from app.heartbeat.service import _build_meta

        fresh = _make_fresh_view(lagging=1)
        meta = _build_meta(now_ms=NOW_MS, fresh=fresh)
        assert meta.partial is True

    def test_partial_false_when_no_lagging(self) -> None:
        """无 lagging 时，meta.partial=False。"""
        from app.heartbeat.service import _build_meta

        fresh = _make_fresh_view(lagging=0)
        meta = _build_meta(now_ms=NOW_MS, fresh=fresh)
        assert meta.partial is False

    def test_ingestion_lag_ms_computed(self) -> None:
        """ingestion_lag_ms = now_ms - min_watermark_ms（>= 0）。"""
        from app.heartbeat.service import _build_meta

        fresh = _make_fresh_view(min_watermark_ms=NOW_MS - 300)
        meta = _build_meta(now_ms=NOW_MS, fresh=fresh)
        assert meta.ingestion_lag_ms == 300


# ── get_liveness ──────────────────────────────────────────────────────────────


class TestGetLiveness:
    @pytest.mark.asyncio
    async def test_returns_view_when_found(self) -> None:
        """AIC 存在时返回 (view, meta) 元组。"""
        from app.heartbeat.service import get_liveness

        entry = _make_entry(last_seen_at_ms=NOW_MS - 500)
        fresh = _make_fresh_view()

        with (
            patch("app.heartbeat.service.store.get_latest", AsyncMock(return_value=entry)),
            patch("app.heartbeat.service.store.redis_now_ms", AsyncMock(return_value=NOW_MS)),
            patch("app.heartbeat.service.evaluate_freshness", AsyncMock(return_value=fresh)),
        ):
            view, meta = await get_liveness(AsyncMock(), "aic-001")

        assert view.aic == "aic-001"
        assert meta.silence_threshold_seconds > 0

    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self) -> None:
        """AIC 不存在时抛出 HeartbeatAicUnknownError（404）。"""
        from app.heartbeat.exception import HeartbeatAicUnknownError
        from app.heartbeat.service import get_liveness

        with (
            patch("app.heartbeat.service.store.get_latest", AsyncMock(return_value=None)),
            patch("app.heartbeat.service.store.redis_now_ms", AsyncMock(return_value=NOW_MS)),
            patch("app.heartbeat.service.evaluate_freshness", AsyncMock(return_value=_make_fresh_view())),
            pytest.raises(HeartbeatAicUnknownError),
        ):
            await get_liveness(AsyncMock(), "aic-nonexistent")

    @pytest.mark.asyncio
    async def test_redis_connection_error_raises_read_model_lagging(self) -> None:
        """Redis 连接异常 → ReadModelLaggingError（P1-4）。"""
        from redis.exceptions import ConnectionError as RedisConnectionError

        from app.heartbeat.exception import ReadModelLaggingError
        from app.heartbeat.service import get_liveness

        with (
            patch("app.heartbeat.service.store.redis_now_ms", AsyncMock(side_effect=RedisConnectionError)),
            pytest.raises(ReadModelLaggingError),
        ):
            await get_liveness(AsyncMock(), "aic-001")


# ── get_summary ───────────────────────────────────────────────────────────────


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_alive_and_silent_counts_are_complementary(self) -> None:
        """alive_count + silent_count <= total_known（可能存在 left_alive 未计入 silent）。"""
        from app.heartbeat.service import get_summary

        fresh = _make_fresh_view()

        with (
            patch("app.heartbeat.service.store.redis_now_ms", AsyncMock(return_value=NOW_MS)),
            patch("app.heartbeat.service.store.zcard", AsyncMock(return_value=5)),
            patch("app.heartbeat.service.store.zcount_score_at_least", AsyncMock(return_value=3)),
            patch("app.heartbeat.service.evaluate_freshness", AsyncMock(return_value=fresh)),
        ):
            summary, _meta = await get_summary(AsyncMock())

        assert summary.alive_count == 3
        assert summary.total_known == 5
        assert summary.silent_count == 2

    @pytest.mark.asyncio
    async def test_redis_connection_error_raises_read_model_lagging(self) -> None:
        """Redis 连接异常 → ReadModelLaggingError（P1-4）。"""
        from redis.exceptions import ConnectionError as RedisConnectionError

        from app.heartbeat.exception import ReadModelLaggingError
        from app.heartbeat.service import get_summary

        with (
            patch("app.heartbeat.service.store.redis_now_ms", AsyncMock(side_effect=RedisConnectionError)),
            pytest.raises(ReadModelLaggingError),
        ):
            await get_summary(AsyncMock())


# ── _plan_liveness_query ─────────────────────────────────────────────────────


class TestPlanLivenessQuery:
    def test_rejects_time_range(self) -> None:
        """time_range 非 None → 422（P2-11）。"""
        from app.heartbeat.exception import UnsupportedFieldError
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest
        from app.heartbeat.service import _plan_liveness_query

        request = HeartbeatLivenessQueryRequest.model_validate(
            {"timeRange": {"startAt": "2024-01-01T00:00:00Z", "endAt": "2024-01-02T00:00:00Z"}}
        )
        with pytest.raises(UnsupportedFieldError):
            _plan_liveness_query(request, now_ms=NOW_MS)

    def test_rejects_empty_filter(self) -> None:
        """无有效 filter 且无 cursor → 400（C-QUERY-1）。"""
        from app.heartbeat.exception import QueryRequiresSelectiveFilterError
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest
        from app.heartbeat.service import _plan_liveness_query

        request = HeartbeatLivenessQueryRequest()
        with pytest.raises(QueryRequiresSelectiveFilterError):
            _plan_liveness_query(request, now_ms=NOW_MS)

    def test_accepts_aic_in_filter(self) -> None:
        """aic in [...] 过滤器通过 planner 验证，返回 plan。"""
        from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest
        from app.heartbeat.service import LivenessQueryPlan, _plan_liveness_query

        f = AMPFilter(conditions=[AMPFilterCondition(field="aic", op="in", value=["a", "b"])])
        request = HeartbeatLivenessQueryRequest(filter=f)
        plan = _plan_liveness_query(request, now_ms=NOW_MS)
        assert isinstance(plan, LivenessQueryPlan)
