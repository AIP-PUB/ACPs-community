"""tests: AliveSyncError 异常层级与字段。"""
import pytest

from acps_sdk.amp.alive_sync.errors import (
    AliveSyncError,
    GapDetectedError,
    ResyncRequired,
    SnapshotProtocolError,
)


class TestExceptionHierarchy:
    def test_gap_detected_is_alive_sync_error(self) -> None:
        err = GapDetectedError(shard="hb-000", expected_seq=5, got_seq=7)
        assert isinstance(err, AliveSyncError)

    def test_resync_required_is_alive_sync_error(self) -> None:
        err = ResyncRequired(reason="checkpoint_lost")
        assert isinstance(err, AliveSyncError)

    def test_snapshot_protocol_error_is_alive_sync_error(self) -> None:
        err = SnapshotProtocolError("bad meta")
        assert isinstance(err, AliveSyncError)


class TestGapDetectedError:
    def test_fields(self) -> None:
        err = GapDetectedError(shard="hb-000", expected_seq=5, got_seq=7)
        assert err.shard == "hb-000"
        assert err.expected_seq == 5
        assert err.got_seq == 7

    def test_str_contains_info(self) -> None:
        err = GapDetectedError(shard="hb-001", expected_seq=3, got_seq=5)
        msg = str(err)
        assert "hb-001" in msg
        assert "3" in msg
        assert "5" in msg


class TestResyncRequired:
    def test_reason_field(self) -> None:
        err = ResyncRequired(reason="offset_out_of_range")
        assert err.reason == "offset_out_of_range"

    def test_raise_and_catch(self) -> None:
        with pytest.raises(ResyncRequired) as exc_info:
            raise ResyncRequired(reason="checkpoint_lost")
        assert exc_info.value.reason == "checkpoint_lost"
