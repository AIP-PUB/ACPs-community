"""单元测试：E-1 lifecycle_compactor.py — LifecycleCompactor（mock IO）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_redis() -> MagicMock:
    return MagicMock()


@pytest.fixture
def compactor() -> object:
    from app.message.lifecycle_compactor import LifecycleCompactor

    redis = _make_redis()
    return LifecycleCompactor(redis)


class TestImportAndInit:
    def test_importable(self) -> None:
        from app.message.lifecycle_compactor import CompactionResult, LifecycleCompactor  # noqa: F401

    def test_compaction_result_fields(self) -> None:
        from app.message.lifecycle_compactor import CompactionResult

        r = CompactionResult(affected=5, written=5, skipped=0, watermark_ms=1000)
        assert r.affected == 5
        assert r.written == 5
        assert r.skipped == 0
        assert r.watermark_ms == 1000


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_no_affected_keys_returns_zero_result(self, compactor: object) -> None:
        with (
            patch("app.message.lifecycle_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.lifecycle_compactor.store.fetch_affected_lifecycle_keys",
                AsyncMock(return_value=([], None)),
            ),
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        assert result.affected == 0
        assert result.written == 0
        assert result.watermark_ms is None

    @pytest.mark.asyncio
    async def test_affected_keys_recomputed(self, compactor: object) -> None:
        keys = [("kafka", "my-topic", "topic", "", "mid:abc")]
        with (
            patch("app.message.lifecycle_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.lifecycle_compactor.store.fetch_affected_lifecycle_keys",
                AsyncMock(return_value=(keys, 5_000_000)),
            ),
            patch(
                "app.message.lifecycle_compactor.store.recompute_lifecycles", AsyncMock(return_value=1)
            ) as mock_recompute,
            patch("app.message.lifecycle_compactor.freshness.set_compaction_watermark", AsyncMock()),
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        assert result.affected == 1
        assert result.written == 1
        mock_recompute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watermark_advanced_on_success(self, compactor: object) -> None:
        keys = [("kafka", "my-topic", "topic", "", "mid:abc")]
        with (
            patch("app.message.lifecycle_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.lifecycle_compactor.store.fetch_affected_lifecycle_keys",
                AsyncMock(return_value=(keys, 9_000_000)),
            ),
            patch("app.message.lifecycle_compactor.store.recompute_lifecycles", AsyncMock(return_value=1)),
            patch("app.message.lifecycle_compactor.freshness.set_compaction_watermark", AsyncMock()) as mock_set_wm,
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        mock_set_wm.assert_awaited_once()
        assert result.watermark_ms == 9_000_000

    @pytest.mark.asyncio
    async def test_watermark_not_advanced_on_failure(self, compactor: object) -> None:
        keys = [("kafka", "my-topic", "topic", "", "mid:abc")]
        from app.message.exception import MessageCompactionError

        with (
            patch("app.message.lifecycle_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.lifecycle_compactor.store.fetch_affected_lifecycle_keys",
                AsyncMock(return_value=(keys, 9_000_000)),
            ),
            patch(
                "app.message.lifecycle_compactor.store.recompute_lifecycles",
                AsyncMock(side_effect=MessageCompactionError("fail")),
            ),
            patch("app.message.lifecycle_compactor.freshness.set_compaction_watermark", AsyncMock()) as mock_set_wm,
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        mock_set_wm.assert_not_awaited()
        assert result.watermark_ms is None

    @pytest.mark.asyncio
    async def test_overlap_applied_to_rebuild_from(self, compactor: object) -> None:
        keys: list = []
        with (
            patch(
                "app.message.lifecycle_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=5_000_000)
            ),
            patch(
                "app.message.lifecycle_compactor.store.fetch_affected_lifecycle_keys",
                AsyncMock(return_value=(keys, None)),
            ) as mock_fetch,
        ):
            await compactor.run_once()  # type: ignore[attr-defined]
        # rebuild_from = watermark - overlap_seconds * 1000 <= 5_000_000
        call_kwargs = mock_fetch.call_args
        rebuild_from = call_kwargs[1].get("rebuild_from_ms") or call_kwargs[0][0]
        assert rebuild_from <= 5_000_000
