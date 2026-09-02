"""tests/unit/test_heartbeat_relay_unit.py — HeartbeatRelay 单元测试（Step 9）。

覆盖不依赖 Kafka / Redis 基础设施的纯逻辑：
- _build_envelope: 字段映射（upsert / delete）、leave_alive 无 payload 且无 reason（9-11）
- _detect_truncation: seq 跳跃 → _truncated_total 递增
"""

from __future__ import annotations

import pytest


class TestBuildEnvelope:
    """_build_envelope 字段映射（§3.6 第 2 条 / 9-11）。"""

    def _make_relay(self) -> object:
        from unittest.mock import MagicMock

        from app.heartbeat.relay import HeartbeatRelay

        return HeartbeatRelay(redis=MagicMock())

    def test_upsert_all_fields(self) -> None:
        """upsert 条目：id / version / type / payload 均正确填充。"""
        from acps_sdk.amp.heartbeat_sync import ALIVE_DELTA_TYPE, alive_object_id

        relay = self._make_relay()
        fields = {
            "seq": "42",
            "kind": "enter_alive",
            "op": "upsert",
            "aic": "agent-001",
            "last_seen_at_ms": "1700000000000",
            "source_timestamp_ms": "1699999990000",
        }
        envelope = relay._build_envelope("hb-000", fields)  # type: ignore[attr-defined]

        assert envelope.id == alive_object_id("agent-001")
        assert envelope.version == "42"
        assert envelope.seq == "42"
        assert envelope.type == ALIVE_DELTA_TYPE
        assert envelope.shard == "hb-000"
        assert envelope.op == "upsert"
        assert envelope.kind == "enter_alive"
        assert envelope.payload is not None
        assert envelope.payload.aic == "agent-001"
        assert "T" in envelope.payload.last_seen_at
        assert envelope.payload.source_timestamp is not None

    def test_upsert_without_source_timestamp(self) -> None:
        """upsert 无 source_timestamp_ms 时 payload.sourceTimestamp 为 None。"""
        relay = self._make_relay()
        fields = {
            "seq": "5",
            "kind": "refresh_alive",
            "op": "upsert",
            "aic": "agent-002",
            "last_seen_at_ms": "1700000000000",
        }
        envelope = relay._build_envelope("hb-000", fields)  # type: ignore[attr-defined]

        assert envelope.payload is not None
        assert envelope.payload.source_timestamp is None

    def test_delete_no_payload(self) -> None:
        """leave_alive（op=delete）：payload=None（9-11）。"""
        relay = self._make_relay()
        fields = {
            "seq": "99",
            "kind": "leave_alive",
            "op": "delete",
            "aic": "agent-003",
            "reason": "silent",
        }
        envelope = relay._build_envelope("hb-000", fields)  # type: ignore[attr-defined]

        assert envelope.payload is None
        assert envelope.op == "delete"
        assert envelope.kind == "leave_alive"

    def test_delete_reason_not_in_serialized_output(self) -> None:
        """reason 字段不进信封序列化输出（9-11）。"""
        relay = self._make_relay()
        fields = {
            "seq": "10",
            "kind": "leave_alive",
            "op": "delete",
            "aic": "agent-004",
            "reason": "evict_repair",
        }
        envelope = relay._build_envelope("hb-000", fields)  # type: ignore[attr-defined]

        serialized = envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "reason" not in serialized

    def test_iso_format_millisecond_precision(self) -> None:
        """last_seen_at 应是 ISO 8601 UTC 格式（Z 后缀，毫秒精度）。"""
        relay = self._make_relay()
        fields = {
            "seq": "1",
            "kind": "enter_alive",
            "op": "upsert",
            "aic": "agent-005",
            "last_seen_at_ms": "1700000000123",
        }
        envelope = relay._build_envelope("hb-000", fields)  # type: ignore[attr-defined]

        assert envelope.payload is not None
        assert envelope.payload.last_seen_at.endswith("Z")
        assert ".123Z" in envelope.payload.last_seen_at


class TestDetectTruncation:
    """_detect_truncation: seq 跳跃 → _truncated_total 递增。"""

    def _make_relay(self) -> object:
        from unittest.mock import MagicMock

        from app.heartbeat.relay import HeartbeatRelay

        relay = HeartbeatRelay(redis=MagicMock())
        relay._truncated_total = 0
        return relay

    def test_first_seq_no_gap(self) -> None:
        """首次调用（无 expected）不计为截断。"""
        relay = self._make_relay()
        relay._detect_truncation("hb-000", 1)  # type: ignore[attr-defined]
        assert relay._truncated_total == 0  # type: ignore[attr-defined]

    def test_consecutive_no_gap(self) -> None:
        """连续 seq 不触发截断计数。"""
        relay = self._make_relay()
        relay._detect_truncation("hb-000", 1)  # type: ignore[attr-defined]
        relay._detect_truncation("hb-000", 2)  # type: ignore[attr-defined]
        relay._detect_truncation("hb-000", 3)  # type: ignore[attr-defined]
        assert relay._truncated_total == 0  # type: ignore[attr-defined]

    def test_seq_gap_increments_total(self) -> None:
        """seq 跳跃（5 → 7）时 _truncated_total 递增。"""
        relay = self._make_relay()
        relay._detect_truncation("hb-000", 5)  # type: ignore[attr-defined]
        relay._detect_truncation("hb-000", 7)  # type: ignore[attr-defined]
        assert relay._truncated_total == 1  # type: ignore[attr-defined]

    def test_multiple_gaps_accumulate(self) -> None:
        """多次跳跃累积计数。"""
        relay = self._make_relay()
        relay._detect_truncation("hb-000", 1)  # type: ignore[attr-defined]
        relay._detect_truncation("hb-000", 3)  # type: ignore[attr-defined]  # gap
        relay._detect_truncation("hb-000", 4)  # type: ignore[attr-defined]  # ok
        relay._detect_truncation("hb-000", 10)  # type: ignore[attr-defined]  # gap
        assert relay._truncated_total == 2  # type: ignore[attr-defined]

    def test_expected_seq_updated_after_gap(self) -> None:
        """截断后 _expected_seq 更新为 seq+1（继续追踪后续）。"""
        relay = self._make_relay()
        relay._detect_truncation("hb-000", 5)  # type: ignore[attr-defined]
        relay._detect_truncation("hb-000", 8)  # type: ignore[attr-defined]  # gap
        relay._detect_truncation("hb-000", 9)  # type: ignore[attr-defined]  # no gap from 8
        assert relay._truncated_total == 1  # type: ignore[attr-defined]


class TestMsToIso:
    """_ms_to_iso 毫秒转 ISO 工具函数。"""

    def test_round_seconds(self) -> None:
        from app.heartbeat.relay import _ms_to_iso

        result = _ms_to_iso(1700000000000)
        assert result.endswith("Z")
        assert "T" in result

    def test_millisecond_precision(self) -> None:
        from app.heartbeat.relay import _ms_to_iso

        result = _ms_to_iso(1700000000123)
        assert ".123Z" in result

    def test_zero_ms(self) -> None:
        from app.heartbeat.relay import _ms_to_iso

        result = _ms_to_iso(0)
        assert result == "1970-01-01T00:00:00.000Z"

    @pytest.mark.parametrize(
        "ms",
        [1_700_000_000_000, 1_700_000_000_999, 1_700_000_001_001],
    )
    def test_always_has_z_suffix(self, ms: int) -> None:
        from app.heartbeat.relay import _ms_to_iso

        assert _ms_to_iso(ms).endswith("Z")


class TestRelayLagHeal:
    """积压超阈 + 连续空读 → 重置 amp-hb-relay 消费组。"""

    def _make_relay(self) -> object:
        from unittest.mock import AsyncMock, MagicMock

        from app.heartbeat.relay import HeartbeatRelay

        redis = MagicMock()
        redis.xgroup_destroy = AsyncMock()
        redis.xgroup_create = AsyncMock()
        redis.xinfo_consumers = AsyncMock(return_value=[])
        redis.xgroup_delconsumer = AsyncMock()
        return HeartbeatRelay(redis=redis)

    async def test_empty_lag_below_threshold_resets_poll_counter(self) -> None:
        """lag 未超阈时清零空读计数，不重置消费组。"""
        from unittest.mock import AsyncMock, patch

        relay = self._make_relay()
        relay._empty_lag_polls["hb-000"] = 5  # type: ignore[attr-defined]
        with (
            patch(
                "app.heartbeat.relay.outbox_publish_lag_ms",
                new=AsyncMock(return_value=1_000),
            ),
            patch("app.heartbeat.relay.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30
            await relay._maybe_heal_undelivered_lag("hb-000")  # type: ignore[attr-defined]

        assert relay._empty_lag_polls["hb-000"] == 0  # type: ignore[attr-defined]
        relay._redis.xgroup_destroy.assert_not_called()  # type: ignore[attr-defined]

    async def test_empty_lag_accumulates_until_heal_threshold(self) -> None:
        """连续空读达到阈值后 DESTROY+CREATE 消费组。"""
        from unittest.mock import AsyncMock, patch

        from app.heartbeat.relay import _HEAL_EMPTY_POLLS

        relay = self._make_relay()
        with (
            patch(
                "app.heartbeat.relay.outbox_publish_lag_ms",
                new=AsyncMock(return_value=60_000),
            ),
            patch("app.heartbeat.relay.settings") as mock_settings,
        ):
            mock_settings.heartbeat_relay_max_publish_lag_seconds = 30
            for _ in range(_HEAL_EMPTY_POLLS - 1):
                await relay._maybe_heal_undelivered_lag("hb-000")  # type: ignore[attr-defined]
            relay._redis.xgroup_destroy.assert_not_called()  # type: ignore[attr-defined]

            await relay._maybe_heal_undelivered_lag("hb-000")  # type: ignore[attr-defined]

        relay._redis.xgroup_destroy.assert_awaited_once()  # type: ignore[attr-defined]
        relay._redis.xgroup_create.assert_awaited_once()  # type: ignore[attr-defined]
        assert relay._empty_lag_polls["hb-000"] == 0  # type: ignore[attr-defined]

    async def test_prune_idle_foreign_consumers(self) -> None:
        """删除空闲异名 consumer，保留本节点 consumer。"""
        from unittest.mock import AsyncMock

        from app.heartbeat.relay import _IDLE_CONSUMER_MS

        relay = self._make_relay()
        own = relay._consumer_name  # type: ignore[attr-defined]
        relay._redis.xinfo_consumers = AsyncMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": own, "idle": _IDLE_CONSUMER_MS + 1},
                {"name": "relay-stale", "idle": _IDLE_CONSUMER_MS + 1},
                {"name": "relay-fresh", "idle": 100},
            ]
        )
        await relay._prune_idle_consumers("hb-000")  # type: ignore[attr-defined]
        relay._redis.xgroup_delconsumer.assert_awaited_once()  # type: ignore[attr-defined]
        args = relay._redis.xgroup_delconsumer.await_args.args  # type: ignore[attr-defined]
        assert args[-1] == "relay-stale"
