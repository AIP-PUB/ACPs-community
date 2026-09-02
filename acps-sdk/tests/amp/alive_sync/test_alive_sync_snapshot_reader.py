"""tests: parse_meta_line / parse_snapshot_row / snapshot_row_to_record。"""
import json

import pytest

from acps_sdk.amp.alive_sync.errors import SnapshotProtocolError
from acps_sdk.amp.alive_sync.snapshot import (
    parse_meta_line,
    parse_snapshot_row,
    snapshot_row_to_record,
)

# ── 测试 fixture ─────────────────────────────────────────────────────────────

VALID_META = {
    "recordType": "snapshot-meta",
    "type": "amp-alive-delta",
    "cutoverSeqByShard": {"hb-000": "42"},
    "generatedAt": "2026-06-13T01:20:00Z",
}

VALID_ROW = {
    "shard": "hb-000",
    "seq": "43",
    "type": "amp-alive-delta",
    "id": "urn:amp:alive:AIC-001",
    "version": "43",
    "op": "upsert",
    "kind": "snapshot",
    "payload": {"aic": "AIC-001", "lastSeenAt": "2026-06-13T01:19:50Z"},
}


class TestParseMetaLine:
    def test_valid_meta_dict(self) -> None:
        meta = parse_meta_line(json.dumps(VALID_META))
        assert meta.generated_at == "2026-06-13T01:20:00Z"
        assert meta.cutover_seq_by_shard == {"hb-000": "42"}

    def test_valid_meta_bytes(self) -> None:
        meta = parse_meta_line(json.dumps(VALID_META).encode())
        assert meta.generated_at == "2026-06-13T01:20:00Z"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(SnapshotProtocolError, match="JSON 解析失败"):
            parse_meta_line("not-json")

    def test_wrong_record_type_raises(self) -> None:
        bad = {**VALID_META, "recordType": "wrong"}
        with pytest.raises(SnapshotProtocolError, match="recordType"):
            parse_meta_line(json.dumps(bad))

    def test_wrong_type_raises(self) -> None:
        bad = {**VALID_META, "type": "other"}
        with pytest.raises(SnapshotProtocolError, match="type"):
            parse_meta_line(json.dumps(bad))


class TestParseSnapshotRow:
    def test_valid_row(self) -> None:
        env = parse_snapshot_row(json.dumps(VALID_ROW))
        assert env.shard == "hb-000"
        assert env.seq == "43"

    def test_valid_row_bytes(self) -> None:
        env = parse_snapshot_row(json.dumps(VALID_ROW).encode())
        assert env.shard == "hb-000"

    def test_wrong_kind_raises(self) -> None:
        bad = {**VALID_ROW, "kind": "enter_alive"}
        with pytest.raises(SnapshotProtocolError, match="kind"):
            parse_snapshot_row(json.dumps(bad))

    def test_wrong_op_raises(self) -> None:
        bad = {**VALID_ROW, "op": "delete"}
        with pytest.raises(SnapshotProtocolError, match="op"):
            parse_snapshot_row(json.dumps(bad))

    def test_missing_payload_raises(self) -> None:
        bad = {k: v for k, v in VALID_ROW.items() if k != "payload"}
        with pytest.raises(SnapshotProtocolError, match="payload"):
            parse_snapshot_row(json.dumps(bad))


class TestSnapshotRowToRecord:
    def test_converts_correctly(self) -> None:
        env = parse_snapshot_row(json.dumps(VALID_ROW))
        record = snapshot_row_to_record(env)
        assert record.aic == "AIC-001"
        assert record.alive is True
        assert record.version == 43
        assert record.shard == "hb-000"
        assert record.last_seen_at == "2026-06-13T01:19:50Z"
