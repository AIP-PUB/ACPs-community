"""tests: AliveDeltaKafkaConsumer unit tests（无真实 Kafka，mock AIOKafkaConsumer）。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.amp.alive_sync.errors import GapDetectedError

from app.heartbeat_sync.kafka_consumer import AliveDeltaKafkaConsumer

# ── 工具 ─────────────────────────────────────────────────────────────────────


def _make_msg(aic: str = "AIC-001", seq: int = 1, shard: str = "hb-000", offset: int = 0) -> MagicMock:
    """构造伪 Kafka 消息。"""
    msg = MagicMock()
    msg.offset = offset
    msg.value = json.dumps(
        {
            "shard": shard,
            "seq": str(seq),
            "type": "amp-alive-delta",
            "id": f"urn:amp:alive:{aic}",
            "version": str(seq),
            "op": "upsert",
            "kind": "enter_alive",
            "payload": {"aic": aic, "lastSeenAt": "2026-06-13T01:00:00Z"},
        }
    ).encode()
    return msg


def _make_consumer(msgs: list[MagicMock]) -> MagicMock:
    """构造伪 AIOKafkaConsumer，消费 msgs 后停止。"""

    async def _aiter(_self: object = None) -> object:
        for msg in msgs:
            yield msg

    consumer_mock = MagicMock()
    consumer_mock.subscribe = MagicMock()
    consumer_mock.start = AsyncMock()
    consumer_mock.stop = AsyncMock()
    consumer_mock.__aiter__ = _aiter
    return consumer_mock


class TestSeekOnAssignListener:
    def test_is_consumer_rebalance_listener(self) -> None:
        from aiokafka.abc import ConsumerRebalanceListener

        from app.heartbeat_sync.kafka_consumer import AliveDeltaKafkaConsumer, _SeekOnAssign

        consumer = AliveDeltaKafkaConsumer("localhost:19092", "test-group", "test-topic")
        assert isinstance(consumer._listener, ConsumerRebalanceListener)
        assert isinstance(consumer._listener, _SeekOnAssign)


class TestSetSeekPlan:
    def test_stores_plan(self) -> None:
        c = AliveDeltaKafkaConsumer("localhost:19092", "test-group", "test-topic")
        c.set_seek_plan(
            cutover_by_shard={"hb-000": 10},
            generated_at="2026-06-13T01:00:00Z",
            lookback_seconds=300,
            checkpoints_by_shard={"hb-000": 100},
        )
        assert c._seek_plan is not None
        assert c._seek_plan["lookback_seconds"] == 300
        assert c._seek_plan["checkpoints_by_shard"]["hb-000"] == 100


# ── poll_apply ────────────────────────────────────────────────────────────────


class TestPollApply:
    @pytest.mark.asyncio
    async def test_applies_messages_to_engine(self) -> None:
        consumer = AliveDeltaKafkaConsumer("localhost:19092", "test-group", "test-topic")
        msgs = [_make_msg(seq=1, offset=0), _make_msg(seq=2, offset=1)]
        consumer._consumer = _make_consumer(msgs)

        apply_calls: list[int | None] = []

        async def fake_apply_delta(env: object, *, kafka_next_offset: int | None = None) -> object:
            from acps_sdk.amp.alive_sync.engine import DeltaDecision

            apply_calls.append(kafka_next_offset)
            return DeltaDecision.APPLY_UPSERT

        engine = MagicMock()
        engine.apply_delta = fake_apply_delta

        await consumer.poll_apply(engine, on_gap=AsyncMock())
        assert apply_calls == [1, 2]  # offset + 1

    @pytest.mark.asyncio
    async def test_gap_calls_on_gap_and_returns(self) -> None:
        consumer = AliveDeltaKafkaConsumer("localhost:19092", "test-group", "test-topic")
        msgs = [_make_msg(seq=1, offset=0)]
        consumer._consumer = _make_consumer(msgs)

        async def fake_apply_delta(env: object, *, kafka_next_offset: int | None = None) -> None:
            raise GapDetectedError(shard="hb-000", expected_seq=2, got_seq=5)

        engine = MagicMock()
        engine.apply_delta = fake_apply_delta

        on_gap_called: list[str] = []

        async def on_gap(shard: str) -> None:
            on_gap_called.append(shard)

        await consumer.poll_apply(engine, on_gap=on_gap)
        assert "hb-000" in on_gap_called
