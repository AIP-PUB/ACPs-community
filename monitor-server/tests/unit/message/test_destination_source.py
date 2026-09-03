"""单元测试：E-3 destination_source.py + state_collector.py。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestNullDestinationStateSource:
    @pytest.mark.asyncio
    async def test_sample_returns_empty(self) -> None:
        from app.message.destination_source import NullDestinationStateSource

        src = NullDestinationStateSource()
        result = await src.sample()
        assert result == []


class TestBuildDestinationSource:
    def test_null_kind_returns_null_source(self) -> None:
        from app.message.destination_source import NullDestinationStateSource, build_destination_source

        with patch("app.message.destination_source.settings") as mock_settings:
            mock_settings.message_destination_source_kind = "null"
            src = build_destination_source()
        assert isinstance(src, NullDestinationStateSource)

    def test_unknown_kind_falls_back_to_null(self) -> None:
        from app.message.destination_source import NullDestinationStateSource, build_destination_source

        with patch("app.message.destination_source.settings") as mock_settings:
            mock_settings.message_destination_source_kind = "kafka"
            src = build_destination_source()
        assert isinstance(src, NullDestinationStateSource)


class TestDestinationSample:
    def test_frozen(self) -> None:
        from app.message.destination_source import DestinationSample

        s = DestinationSample(
            system="kafka",
            destination_name="t",
            destination_kind="topic",
            virtual_host="/",
            visible_messages=10,
            inflight_messages=None,
            delayed_messages=None,
            dead_letter_messages=0,
            oldest_message_age_seconds=None,
            active_consumers=1,
            size_bytes=None,
            captured_at_ms=1000,
        )
        with pytest.raises((AttributeError, TypeError)):
            s.system = "new"  # type: ignore[misc]


class TestStateCollector:
    @pytest.mark.asyncio
    async def test_empty_samples_returns_zero(self) -> None:
        from app.message.state_collector import DestinationStateCollector

        redis = MagicMock()
        source = MagicMock()
        source.sample = AsyncMock(return_value=[])
        collector = DestinationStateCollector(redis, source)
        result = await collector.run_once()
        assert result == 0

    @pytest.mark.asyncio
    async def test_samples_written_and_watermark_advanced(self) -> None:
        from app.message.destination_source import DestinationSample
        from app.message.state_collector import DestinationStateCollector

        redis = MagicMock()
        sample = DestinationSample(
            system="kafka",
            destination_name="t",
            destination_kind="topic",
            virtual_host="/",
            visible_messages=5,
            inflight_messages=None,
            delayed_messages=None,
            dead_letter_messages=0,
            oldest_message_age_seconds=None,
            active_consumers=1,
            size_bytes=None,
            captured_at_ms=1_000_000,
        )
        source = MagicMock()
        source.sample = AsyncMock(return_value=[sample])
        collector = DestinationStateCollector(redis, source)

        with (
            patch("app.message.state_collector.store.insert_destination_snapshot", AsyncMock()) as mock_insert,
            patch("app.message.state_collector.freshness.set_state_watermark", AsyncMock()) as mock_wm,
        ):
            result = await collector.run_once()

        assert result == 1
        mock_insert.assert_awaited_once()
        mock_wm.assert_awaited_once()
