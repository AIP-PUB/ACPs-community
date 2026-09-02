"""tests/unit/test_heartbeat_schema.py — Heartbeat 请求/响应模型单元测试。"""

from __future__ import annotations


class TestHeartbeatResponseMetaExt:
    def test_camel_case_aliases(self) -> None:
        from app.heartbeat.schema import HeartbeatResponseMetaExt

        meta = HeartbeatResponseMetaExt(
            data_freshness_at="2026-01-01T00:00:00Z",
            evaluated_at="2026-01-01T00:00:01Z",
            silence_threshold_seconds=90,
            evict_after_seconds=3600,
        )
        data = meta.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "evaluatedAt" in data
        assert "silenceThresholdSeconds" in data
        assert "evictAfterSeconds" in data
        assert "dataFreshnessAt" in data

    def test_roundtrip_via_camel_case(self) -> None:
        from app.heartbeat.schema import HeartbeatResponseMetaExt

        meta = HeartbeatResponseMetaExt.model_validate(
            {
                "dataFreshnessAt": "2026-01-01T00:00:00Z",
                "evaluatedAt": "2026-01-01T00:00:01Z",
                "silenceThresholdSeconds": 90,
                "evictAfterSeconds": 3600,
            }
        )
        assert meta.evaluated_at == "2026-01-01T00:00:01Z"
        assert meta.silence_threshold_seconds == 90


class TestHeartbeatLivenessView:
    def test_camel_case_serialization(self) -> None:
        from app.heartbeat.schema import HeartbeatLivenessView

        view = HeartbeatLivenessView(
            aic="agent-001",
            is_alive=True,
            liveness_state="alive",
            last_seen_at="2026-01-01T00:00:00Z",
            silence_duration_seconds=0,
        )
        data = view.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "isAlive" in data
        assert "livenessState" in data
        assert "lastSeenAt" in data
        assert "silenceDurationSeconds" in data

    def test_silence_duration_is_int(self) -> None:
        from app.heartbeat.schema import HeartbeatLivenessView

        view = HeartbeatLivenessView(
            aic="agent-001",
            is_alive=False,
            liveness_state="silent",
            last_seen_at="2026-01-01T00:00:00Z",
            silence_duration_seconds=120,
        )
        assert isinstance(view.silence_duration_seconds, int)

    def test_source_timestamp_optional(self) -> None:
        from app.heartbeat.schema import HeartbeatLivenessView

        view = HeartbeatLivenessView(
            aic="agent-001",
            is_alive=True,
            liveness_state="alive",
            last_seen_at="2026-01-01T00:00:00Z",
            silence_duration_seconds=0,
        )
        data = view.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "sourceTimestamp" not in data


class TestHeartbeatQueryResponse:
    def test_meta_preserves_extended_fields(self) -> None:
        """HeartbeatQueryResponse 的 meta 必须保留 evaluatedAt 等扩展字段（§D-3）。"""
        from app.heartbeat.schema import HeartbeatLivenessView, HeartbeatQueryResponse, HeartbeatResponseMetaExt

        meta = HeartbeatResponseMetaExt(
            data_freshness_at="2026-01-01T00:00:00Z",
            evaluated_at="2026-01-01T00:00:01Z",
            silence_threshold_seconds=90,
            evict_after_seconds=3600,
        )
        resp: HeartbeatQueryResponse[HeartbeatLivenessView] = HeartbeatQueryResponse(
            items=[],
            meta=meta,
        )
        data = resp.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "evaluatedAt" in data["meta"], "evaluatedAt must not be lost in HeartbeatQueryResponse.meta"

    def test_not_subclass_of_amp_query_response(self) -> None:
        """HeartbeatQueryResponse 不继承 AMPQueryResponse（§D-3）。"""
        from app.core.amp_api_schema import AMPQueryResponse
        from app.heartbeat.schema import HeartbeatQueryResponse

        assert not issubclass(HeartbeatQueryResponse, AMPQueryResponse)


class TestHeartbeatLivenessQueryRequest:
    def test_accepts_time_range_field(self) -> None:
        """time_range 字段显式声明，不被 extra=ignore 静默丢弃（修复 P2-11）。"""
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest

        req = HeartbeatLivenessQueryRequest.model_validate(
            {"timeRange": {"startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-02T00:00:00Z"}}
        )
        assert req.time_range is not None

    def test_unknown_fields_ignored(self) -> None:
        """其他未知字段静默忽略（extra=ignore）。"""
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest

        req = HeartbeatLivenessQueryRequest.model_validate({"unknownField": "value"})
        assert req is not None

    def test_optional_fields_default_to_none(self) -> None:
        from app.heartbeat.schema import HeartbeatLivenessQueryRequest

        req = HeartbeatLivenessQueryRequest()
        assert req.filter is None
        assert req.sort is None
        assert req.page is None
        assert req.time_range is None
