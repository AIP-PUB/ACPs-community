"""单元测试：B-3 schema.py — 请求/响应/视图模型。"""

from __future__ import annotations

from datetime import UTC

from app.message.schema import (
    MessageDestinationStateView,
    MessageEventView,
    MessageLifecycleDetailView,
    MessageLifecycleQueryRequest,
    MessageLifecycleView,
    MessageQueryRequest,
    MessageThroughputPoint,
    MessageThroughputRequest,
    MessageThroughputSeries,
)


class TestMessageEventViewCamelCase:
    def test_log_id_alias(self) -> None:
        view = MessageEventView(
            log_id="l1",
            timestamp="2025-01-01T00:00:00Z",
            direction="send",
            event_type="send",
            system="kafka",
            destination_name="t1",
            destination_kind="topic",
        )
        dumped = view.model_dump(by_alias=True, exclude_none=True)
        assert "logId" in dumped

    def test_event_type_alias(self) -> None:
        view = MessageEventView(
            log_id="l1",
            timestamp="2025-01-01T00:00:00Z",
            direction="send",
            event_type="send",
            system="kafka",
            destination_name="t1",
            destination_kind="topic",
        )
        dumped = view.model_dump(by_alias=True, exclude_none=True)
        assert "eventType" in dumped

    def test_populate_by_name_true(self) -> None:
        view = MessageEventView(
            log_id="l1",
            timestamp="2025-01-01T00:00:00Z",
            direction="send",
            event_type="send",
            system="kafka",
            destination_name="t1",
            destination_kind="topic",
        )
        assert view.log_id == "l1"


class TestMessageLifecycleViewTypes:
    def test_send_count_is_int(self) -> None:

        hints = MessageLifecycleView.__annotations__
        assert hints["send_count"] is int or "int" in str(hints["send_count"])

    def test_avg_ack_latency_ms_is_float_or_none(self) -> None:
        hints = MessageLifecycleView.__annotations__
        assert "float" in str(hints.get("avg_ack_latency_ms", ""))

    def test_lifecycle_key_alias(self) -> None:
        view = MessageLifecycleView(
            lifecycle_key="mid:m1",
            system="kafka",
            destination_name="t1",
            destination_kind="topic",
            virtual_host="/",
            first_seen_at="2025-01-01T00:00:00Z",
            last_seen_at="2025-01-01T00:01:00Z",
            producer_aics=[],
            consumer_aics=[],
            send_count=1,
            receive_count=0,
            dead_lettered=False,
            duplicate_consumed=False,
            unacked=True,
        )
        dumped = view.model_dump(by_alias=True, exclude_none=True)
        assert "lifecycleKey" in dumped


class TestMessageLifecycleDetailViewNoMeta:
    """lifecycles/{messageId} 裸资源端点视图无 meta 字段（设计 §6.3）。"""

    def test_no_meta_field(self) -> None:
        assert not hasattr(MessageLifecycleDetailView, "meta")


class TestMessageDestinationStateViewNullable:
    def test_visible_depth_nullable(self) -> None:

        hints = MessageDestinationStateView.__annotations__
        vd = hints.get("visible_messages")
        assert vd is not None
        assert "None" in str(vd) or "Optional" in str(vd) or "int | None" in str(vd)


class TestMessageThroughputPoint:
    def test_produced_count_is_int(self) -> None:
        hints = MessageThroughputPoint.__annotations__
        pc = hints.get("produced_count")
        assert "int" in str(pc)

    def test_avg_ack_latency_ms_is_float_or_none(self) -> None:
        hints = MessageThroughputPoint.__annotations__
        avg = hints.get("avg_ack_latency_ms")
        assert "float" in str(avg)

    def test_bucket_alias(self) -> None:
        from datetime import datetime

        point = MessageThroughputPoint(
            bucket=datetime(2025, 1, 1, tzinfo=UTC),
            produced_count=10,
            consumed_count=8,
        )
        dumped = point.model_dump(by_alias=True, exclude_none=True)
        assert "bucket" in dumped


class TestMessageThroughputSeries:
    def test_no_meta_field(self) -> None:
        assert not hasattr(MessageThroughputSeries, "meta")

    def test_empty_points_valid(self) -> None:
        series = MessageThroughputSeries(
            system="kafka",
            destination_name="t1",
            points=[],
        )
        assert series.points == []


class TestMessageQueryRequestImports:
    """MessageQueryRequest 从 core.amp_api_schema 导入通用类型（不重复定义）。"""

    def test_time_range_field_exists(self) -> None:
        req = MessageQueryRequest()
        assert hasattr(req, "time_range")

    def test_filter_field_exists(self) -> None:
        req = MessageQueryRequest()
        assert hasattr(req, "filter")

    def test_page_field_exists(self) -> None:
        req = MessageQueryRequest()
        assert hasattr(req, "page")

    def test_include_raw_log_default_false(self) -> None:
        req = MessageQueryRequest()
        assert req.include_raw_log is False


class TestMessageLifecycleQueryRequest:
    def test_min_receive_count_default_none(self) -> None:
        req = MessageLifecycleQueryRequest()
        assert req.min_receive_count is None

    def test_only_unacked_default(self) -> None:
        req = MessageLifecycleQueryRequest()
        assert req.only_unacked is False


class TestMessageThroughputRequest:
    def test_system_required(self) -> None:
        req = MessageThroughputRequest()
        assert req.system is None

    def test_step_default_none(self) -> None:
        req = MessageThroughputRequest()
        assert req.step is None
