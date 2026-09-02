"""单元测试：E-2 throughput_compactor.py — ThroughputCompactor（mock IO）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def compactor() -> object:
    from app.message.throughput_compactor import ThroughputCompactor

    redis = MagicMock()
    return ThroughputCompactor(redis)


class TestImportAndInit:
    def test_importable(self) -> None:
        from app.message.throughput_compactor import ThroughputCompactor  # noqa: F401


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_no_affected_buckets_returns_zero(self, compactor: object) -> None:
        with (
            patch("app.message.throughput_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch("app.message.throughput_compactor.store.fetch_affected_buckets", AsyncMock(return_value=([], None))),
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        assert result.affected == 0
        assert result.written == 0

    @pytest.mark.asyncio
    async def test_affected_buckets_recomputed(self, compactor: object) -> None:
        buckets = [(1_000_000, "kafka", "my-topic", "topic", "")]
        with (
            patch("app.message.throughput_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.throughput_compactor.store.fetch_affected_buckets",
                AsyncMock(return_value=(buckets, 5_000_000)),
            ),
            patch(
                "app.message.throughput_compactor.store.recompute_throughput_buckets", AsyncMock(return_value=1)
            ) as mock_recompute,
            patch("app.message.throughput_compactor.freshness.set_compaction_watermark", AsyncMock()),
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        mock_recompute.assert_awaited_once()
        assert result.written == 1

    @pytest.mark.asyncio
    async def test_watermark_advanced_on_success(self, compactor: object) -> None:
        buckets = [(1_000_000, "kafka", "my-topic", "topic", "")]
        with (
            patch("app.message.throughput_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.throughput_compactor.store.fetch_affected_buckets",
                AsyncMock(return_value=(buckets, 7_000_000)),
            ),
            patch("app.message.throughput_compactor.store.recompute_throughput_buckets", AsyncMock(return_value=1)),
            patch("app.message.throughput_compactor.freshness.set_compaction_watermark", AsyncMock()) as mock_set_wm,
        ):
            await compactor.run_once()  # type: ignore[attr-defined]
        mock_set_wm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watermark_not_advanced_on_failure(self, compactor: object) -> None:
        from app.message.exception import MessageCompactionError

        buckets = [(1_000_000, "kafka", "my-topic", "topic", "")]
        with (
            patch("app.message.throughput_compactor.freshness.read_compaction_watermark", AsyncMock(return_value=None)),
            patch(
                "app.message.throughput_compactor.store.fetch_affected_buckets",
                AsyncMock(return_value=(buckets, 7_000_000)),
            ),
            patch(
                "app.message.throughput_compactor.store.recompute_throughput_buckets",
                AsyncMock(side_effect=MessageCompactionError("fail")),
            ),
            patch("app.message.throughput_compactor.freshness.set_compaction_watermark", AsyncMock()) as mock_set_wm,
        ):
            result = await compactor.run_once()  # type: ignore[attr-defined]
        mock_set_wm.assert_not_awaited()
        assert result.watermark_ms is None
