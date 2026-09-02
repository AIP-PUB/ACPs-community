"""tests: passes_seq_gate / is_gap / passes_version / classify_delta 纯函数。"""
import pytest

from acps_sdk.amp.alive_sync.engine import (
    DeltaDecision,
    classify_delta,
    is_gap,
    passes_seq_gate,
    passes_version,
)
from acps_sdk.amp.heartbeat_sync import (
    AliveDeltaEnvelope,
    AliveSetEntry,
)


# ── 纯函数 ──────────────────────────────────────────────────────────────────

class TestSeqGate:
    def test_seq_greater_passes(self) -> None:
        assert passes_seq_gate(5, 4) is True

    def test_seq_equal_blocked(self) -> None:
        assert passes_seq_gate(4, 4) is False

    def test_seq_less_blocked(self) -> None:
        assert passes_seq_gate(3, 5) is False


class TestIsGap:
    def test_gap_detected(self) -> None:
        assert is_gap(7, 4) is True  # 7 > 4+1

    def test_no_gap_consecutive(self) -> None:
        assert is_gap(5, 4) is False

    def test_no_gap_first_event(self) -> None:
        # last_seen_seq=0 (初始化默认值) + seq=1 = 连续
        assert is_gap(1, 0) is False


class TestPassesVersion:
    def test_no_local_version_passes(self) -> None:
        assert passes_version(1, None) is True

    def test_greater_version_passes(self) -> None:
        assert passes_version(5, 3) is True

    def test_equal_version_blocked(self) -> None:
        assert passes_version(3, 3) is False

    def test_older_version_blocked(self) -> None:
        assert passes_version(2, 5) is False


# ── classify_delta ───────────────────────────────────────────────────────────

def _make_envelope(
    seq: int,
    op: str = "upsert",
    kind: str = "enter_alive",
    aic: str = "AIC-001",
    shard: str = "hb-000",
) -> AliveDeltaEnvelope:
    return AliveDeltaEnvelope(
        shard=shard,
        seq=str(seq),
        type="amp-alive-delta",
        id=f"urn:amp:alive:{aic}",
        version=str(seq),
        op=op,
        kind=kind,
        payload=AliveSetEntry(aic=aic, lastSeenAt="2026-06-13T01:20:00Z"),
    )


class TestClassifyDelta:
    def test_skip_seq_gate_on_duplicate(self) -> None:
        env = _make_envelope(seq=3)
        assert classify_delta(env, last_seen_seq=5, local_version=None) is DeltaDecision.SKIP_SEQ_GATE

    def test_gap_detected(self) -> None:
        env = _make_envelope(seq=10)
        assert classify_delta(env, last_seen_seq=4, local_version=None) is DeltaDecision.GAP

    def test_skip_version_on_stale(self) -> None:
        env = _make_envelope(seq=5)
        assert classify_delta(env, last_seen_seq=4, local_version=6) is DeltaDecision.SKIP_VERSION

    def test_apply_upsert_on_fresh_enter(self) -> None:
        env = _make_envelope(seq=5, op="upsert")
        assert classify_delta(env, last_seen_seq=4, local_version=None) is DeltaDecision.APPLY_UPSERT

    def test_apply_delete_on_leave(self) -> None:
        env = _make_envelope(seq=6, op="delete", kind="leave_alive")
        assert classify_delta(env, last_seen_seq=5, local_version=5) is DeltaDecision.APPLY_DELETE

    def test_first_event_seq1(self) -> None:
        """seq=1, last_seen_seq=0 (初始化默认) → APPLY_UPSERT。"""
        env = _make_envelope(seq=1, op="upsert")
        assert classify_delta(env, last_seen_seq=0, local_version=None) is DeltaDecision.APPLY_UPSERT

    def test_refresh_alive_passes_version(self) -> None:
        """refresh_alive: seq=7 > local_version=5 → APPLY_UPSERT。"""
        env = _make_envelope(seq=7, op="upsert", kind="refresh_alive")
        assert classify_delta(env, last_seen_seq=6, local_version=5) is DeltaDecision.APPLY_UPSERT
