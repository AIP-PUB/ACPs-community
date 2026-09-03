"""单元测试：C-1 store.py — 非 IO 辅助函数（_coerce_ch_row、行映射等）。

IO 函数（_run_query、insert_events 等）依赖 ClickHouse，属于集成测试范围，
此处只覆盖纯逻辑辅助函数，确保模块可导入且辅助函数语义正确。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


class TestModuleImport:
    def test_store_importable(self) -> None:
        import app.message.store  # noqa: F401


class TestCoerceCHRow:
    """_coerce_ch_row: ClickHouse DateTime64 → ISO 字符串。"""

    def setup_method(self) -> None:
        from app.message.store import _coerce_ch_row

        self._fn = _coerce_ch_row

    def test_datetime_converted_to_iso(self) -> None:
        dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        result = self._fn({"timestamp": dt, "log_id": "abc"})
        assert isinstance(result["timestamp"], str)
        assert "2026" in result["timestamp"]

    def test_string_passthrough(self) -> None:
        result = self._fn({"system": "kafka"})
        assert result["system"] == "kafka"

    def test_int_passthrough(self) -> None:
        result = self._fn({"payload_size_bytes": 512})
        assert result["payload_size_bytes"] == 512

    def test_none_passthrough(self) -> None:
        result = self._fn({"virtual_host": None})
        assert result["virtual_host"] is None

    def test_list_passthrough(self) -> None:
        result = self._fn({"producer_aics": ["a", "b"]})
        assert result["producer_aics"] == ["a", "b"]

    def test_empty_dict_returns_empty(self) -> None:
        result = self._fn({})
        assert result == {}


class TestLifecycleRowToView:
    """_lifecycle_row_to_view: 行字典 → MessageLifecycleView 含派生字段。"""

    def setup_method(self) -> None:
        from app.message.store import _lifecycle_row_to_view

        self._fn = _lifecycle_row_to_view

    def _base_row(self, **overrides: object) -> dict:
        base: dict = {
            "lifecycle_key": "mid:abc",
            "message_id": "abc",
            "correlation_id": None,
            "trace_id": None,
            "system": "kafka",
            "destination_name": "my-topic",
            "destination_kind": "topic",
            "virtual_host": None,
            "subscription_name": None,
            "consumer_group_name": None,
            "first_seen_at": "2026-06-01T00:00:00+00:00",
            "last_seen_at": "2026-06-01T01:00:00+00:00",
            "dead_lettered_at": None,
            "producer_aics": ["svc-a"],
            "consumer_aics": ["svc-b"],
            "send_count": 1,
            "receive_count": 1,
            "max_delivery_attempt": 1,
            "dead_lettered": False,
            "dead_letter_reason": None,
            "terminal_state": "ack",
        }
        base.update(overrides)
        return base

    def test_duplicate_consumed_true_when_receive_gt_1(self) -> None:
        row = self._base_row(receive_count=2)
        view = self._fn(row)
        assert view.duplicate_consumed is True

    def test_duplicate_consumed_false_when_receive_eq_1(self) -> None:
        row = self._base_row(receive_count=1)
        view = self._fn(row)
        assert view.duplicate_consumed is False

    def test_unacked_true_when_terminal_state_empty(self) -> None:
        row = self._base_row(terminal_state="")
        view = self._fn(row)
        assert view.unacked is True

    def test_unacked_false_when_terminal_state_ack(self) -> None:
        row = self._base_row(terminal_state="ack")
        view = self._fn(row)
        assert view.unacked is False

    def test_terminal_state_empty_mapped_to_none(self) -> None:
        row = self._base_row(terminal_state="")
        view = self._fn(row)
        assert view.terminal_state is None

    def test_terminal_state_ack_preserved(self) -> None:
        row = self._base_row(terminal_state="ack")
        view = self._fn(row)
        assert view.terminal_state == "ack"

    def test_avg_ack_latency_none_when_zero_samples(self) -> None:
        row = self._base_row()
        row["ack_latency_sum_ms"] = 0
        row["ack_sample_count"] = 0
        view = self._fn(row)
        assert view.avg_ack_latency_ms is None

    def test_avg_ack_latency_calculated_when_samples_gt_0(self) -> None:
        row = self._base_row()
        row["ack_latency_sum_ms"] = 1000
        row["ack_sample_count"] = 4
        view = self._fn(row)
        assert view.avg_ack_latency_ms == pytest.approx(250.0)


class TestThroughputRowToPoint:
    """_throughput_row_to_point: 行字典 → MessageThroughputPoint。"""

    def setup_method(self) -> None:
        from app.message.store import _throughput_row_to_point

        self._fn = _throughput_row_to_point

    def _base_row(self, **overrides: object) -> dict:
        base: dict = {
            "bucket": datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
            "produced_count": 10,
            "consumed_count": 8,
            "ack_count": 7,
            "nack_count": 0,
            "reject_count": 0,
            "timeout_count": 0,
            "dead_letter_count": 0,
            "retry_count": 1,
            "ack_latency_sum_ms": 700,
            "ack_sample_count": 7,
            "last_seen_at": "2026-06-01T00:05:00+00:00",
        }
        base.update(overrides)
        return base

    def test_avg_ack_latency_calculated(self) -> None:
        view = self._fn(self._base_row())
        assert view.avg_ack_latency_ms == pytest.approx(100.0)

    def test_avg_ack_latency_none_when_zero_samples(self) -> None:
        row = self._base_row(ack_latency_sum_ms=0, ack_sample_count=0)
        view = self._fn(row)
        assert view.avg_ack_latency_ms is None

    def test_produced_and_consumed_counts(self) -> None:
        view = self._fn(self._base_row())
        assert view.produced_count == 10
        assert view.consumed_count == 8

    def test_bucket_is_datetime(self) -> None:
        view = self._fn(self._base_row())
        assert isinstance(view.bucket, datetime)
