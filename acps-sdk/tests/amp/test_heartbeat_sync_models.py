"""AMP Heartbeat Sync Profile 线缆模型与编码工具的单元测试（设计文档 §4.4）。"""

from __future__ import annotations

import pytest

from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_TYPE,
    ALIVE_OBJECT_ID_PREFIX,
    AliveDeltaEnvelope,
    AliveSetEntry,
    AliveSnapshotMeta,
    HeartbeatSyncInfo,
    aic_from_object_id,
    alive_object_id,
    seq_from_str,
    seq_to_str,
    shard_id,
    shard_index_from_id,
)


# ── alive_object_id / aic_from_object_id ─────────────────────────────────────


class TestAliveObjectId:
    def test_encode_roundtrip(self) -> None:
        aic = "agent-001"
        assert aic_from_object_id(alive_object_id(aic)) == aic

    def test_encode_format(self) -> None:
        assert alive_object_id("foo") == f"{ALIVE_OBJECT_ID_PREFIX}foo"

    def test_decode_valid(self) -> None:
        assert aic_from_object_id("urn:amp:alive:foo") == "foo"

    def test_decode_invalid_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="前缀不匹配"):
            aic_from_object_id("urn:other:foo")

    def test_decode_empty_aic(self) -> None:
        assert aic_from_object_id("urn:amp:alive:") == ""


# ── shard_id / shard_index_from_id ───────────────────────────────────────────


class TestShardId:
    def test_zero_index_three_digits(self) -> None:
        assert shard_id(0, 1) == "hb-000"

    def test_padding_three_digits(self) -> None:
        assert shard_id(7, 10) == "hb-007"

    def test_exactly_1000_shards_still_3_digits(self) -> None:
        # shard_count=1000 → len(str(999)) = 3 → 宽度 = 3
        assert shard_id(0, 1000) == "hb-000"
        assert shard_id(999, 1000) == "hb-999"

    def test_shard_count_1001_widens_to_4(self) -> None:
        # shard_count=1001 → len(str(1000)) = 4 → 宽度 = 4
        assert shard_id(0, 1001) == "hb-0000"
        assert shard_id(1000, 1001) == "hb-1000"

    def test_shard_count_10000_widens_to_4(self) -> None:
        assert shard_id(0, 10000) == "hb-0000"

    def test_roundtrip(self) -> None:
        for idx in [0, 5, 42, 999]:
            assert shard_index_from_id(shard_id(idx, 1000)) == idx

    def test_decode_valid(self) -> None:
        assert shard_index_from_id("hb-007") == 7
        assert shard_index_from_id("hb-000") == 0

    def test_decode_no_prefix_raises(self) -> None:
        with pytest.raises(ValueError):
            shard_index_from_id("007")

    def test_decode_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            shard_index_from_id("hb-abc")

    def test_decode_empty_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            shard_index_from_id("hb-")


# ── seq_to_str / seq_from_str ─────────────────────────────────────────────────


class TestSeq:
    def test_roundtrip(self) -> None:
        assert seq_from_str(seq_to_str(42)) == 42
        assert seq_from_str(seq_to_str(0)) == 0

    def test_numeric_comparison_not_lexicographic(self) -> None:
        # "1000" 字典序 < "999"，但数值 1000 > 999
        assert seq_from_str("1000") > seq_from_str("999")

    def test_large_value(self) -> None:
        large = 10**15
        assert seq_from_str(seq_to_str(large)) == large

    def test_zero_is_valid(self) -> None:
        assert seq_from_str("0") == 0

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            seq_from_str("")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError):
            seq_from_str("abc")

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="不能为负数"):
            seq_from_str("-1")

    def test_float_string_raises(self) -> None:
        with pytest.raises(ValueError):
            seq_from_str("1.5")


# ── AliveSetEntry ─────────────────────────────────────────────────────────────


class TestAliveSetEntry:
    def test_camel_case_serialization(self) -> None:
        entry = AliveSetEntry(aic="a1", last_seen_at="2026-01-01T00:00:00Z")
        data = entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "lastSeenAt" in data
        assert "last_seen_at" not in data
        assert "aic" in data

    def test_source_timestamp_excluded_when_none(self) -> None:
        entry = AliveSetEntry(aic="a1", last_seen_at="2026-01-01T00:00:00Z")
        data = entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "sourceTimestamp" not in data

    def test_source_timestamp_included_when_set(self) -> None:
        entry = AliveSetEntry(aic="a1", last_seen_at="2026-01-01T00:00:00Z", source_timestamp="2026-01-01T00:00:00Z")
        data = entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "sourceTimestamp" in data

    def test_deserialization_via_camel_case(self) -> None:
        entry = AliveSetEntry.model_validate({"aic": "a1", "lastSeenAt": "2026-01-01T00:00:00Z"})
        assert entry.aic == "a1"
        assert entry.last_seen_at == "2026-01-01T00:00:00Z"


# ── AliveDeltaEnvelope ────────────────────────────────────────────────────────


class TestAliveDeltaEnvelope:
    def _make_upsert(self) -> AliveDeltaEnvelope:
        return AliveDeltaEnvelope(
            shard="hb-000",
            seq="1",
            type=ALIVE_DELTA_TYPE,
            id="urn:amp:alive:agent-001",
            version="1",
            op="upsert",
            kind="enter_alive",
            payload=AliveSetEntry(aic="agent-001", last_seen_at="2026-01-01T00:00:00Z"),
        )

    def _make_delete(self) -> AliveDeltaEnvelope:
        return AliveDeltaEnvelope(
            shard="hb-000",
            seq="2",
            type=ALIVE_DELTA_TYPE,
            id="urn:amp:alive:agent-001",
            version="2",
            op="delete",
            kind="leave_alive",
            payload=None,
        )

    def test_upsert_serialization(self) -> None:
        data = self._make_upsert().model_dump(mode="json", by_alias=True, exclude_none=True)
        assert data["op"] == "upsert"
        assert data["kind"] == "enter_alive"
        assert "payload" in data

    def test_delete_no_payload_in_output(self) -> None:
        data = self._make_delete().model_dump(mode="json", by_alias=True, exclude_none=True)
        assert data["op"] == "delete"
        assert "payload" not in data

    def test_roundtrip_deserialization(self) -> None:
        original = self._make_upsert()
        data = original.model_dump(mode="json", by_alias=True, exclude_none=True)
        restored = AliveDeltaEnvelope.model_validate(data)
        assert restored.shard == original.shard
        assert restored.seq == original.seq
        assert restored.kind == original.kind

    def test_type_field_is_literal(self) -> None:
        env = self._make_upsert()
        assert env.type == ALIVE_DELTA_TYPE


# ── HeartbeatSyncInfo ─────────────────────────────────────────────────────────


class TestHeartbeatSyncInfo:
    def test_camel_case_serialization(self) -> None:
        info = HeartbeatSyncInfo(
            type=ALIVE_DELTA_TYPE,
            schema_version="1",
            snapshot_content_type="application/x-ndjson",
            kafka_topic="amp.heartbeat.alive-delta",
            shard_count=2,
            refresh_emit_interval_seconds=30,
            delta_retention_hours=168,
            current_published_seq_by_shard={"hb-000": "10", "hb-001": "20"},
        )
        data = info.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "schemaVersion" in data
        assert "snapshotContentType" in data
        assert "kafkaTopic" in data
        assert "shardCount" in data
        assert "currentPublishedSeqByShard" in data

    def test_seq_by_shard_values_are_strings(self) -> None:
        info = HeartbeatSyncInfo(
            type=ALIVE_DELTA_TYPE,
            schema_version="1",
            snapshot_content_type="application/x-ndjson",
            kafka_topic="amp.heartbeat.alive-delta",
            shard_count=1,
            refresh_emit_interval_seconds=30,
            delta_retention_hours=168,
            current_published_seq_by_shard={"hb-000": "42"},
        )
        data = info.model_dump(mode="json", by_alias=True, exclude_none=True)
        # 值必须是字符串，Consumer 用 seq_from_str() 转换后数值比较
        assert isinstance(data["currentPublishedSeqByShard"]["hb-000"], str)


# ── AliveSnapshotMeta ─────────────────────────────────────────────────────────


class TestAliveSnapshotMeta:
    def test_camel_case_serialization(self) -> None:
        meta = AliveSnapshotMeta(
            record_type="snapshot-meta",
            type=ALIVE_DELTA_TYPE,
            cutover_seq_by_shard={"hb-000": "5"},
            generated_at="2026-01-01T00:00:00Z",
        )
        data = meta.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "recordType" in data
        assert "cutoverSeqByShard" in data
        assert "generatedAt" in data
        assert data["recordType"] == "snapshot-meta"

    def test_roundtrip_deserialization(self) -> None:
        meta = AliveSnapshotMeta(
            record_type="snapshot-meta",
            type=ALIVE_DELTA_TYPE,
            cutover_seq_by_shard={"hb-000": "5"},
            generated_at="2026-01-01T00:00:00Z",
        )
        data = meta.model_dump(mode="json", by_alias=True, exclude_none=True)
        restored = AliveSnapshotMeta.model_validate(data)
        assert restored.cutover_seq_by_shard == {"hb-000": "5"}
        assert restored.generated_at == "2026-01-01T00:00:00Z"
