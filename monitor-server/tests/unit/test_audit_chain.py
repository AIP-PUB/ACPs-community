"""tests/unit/test_audit_chain.py — Audit 链哈希协议纯函数单元测试。"""

import hashlib

import jcs
import pytest

from app.audit.chain import (
    compute_chain_id,
    compute_current_hash,
    compute_raw_log_hash,
)


class TestComputeRawLogHash:
    def test_deterministic_for_same_input(self) -> None:
        raw = {"schema_version": "1.0", "log_id": "abc", "timestamp": "2026-01-01T00:00:00Z"}
        h1 = compute_raw_log_hash(raw)
        h2 = compute_raw_log_hash(raw)
        assert h1 == h2

    def test_returns_hex_string_of_sha256_length(self) -> None:
        h = compute_raw_log_hash({"a": 1})
        assert len(h) == 64
        int(h, 16)  # must be valid hex

    def test_hash_changes_if_value_changes(self) -> None:
        raw1 = {"log_id": "abc", "ts": "2026-01-01T00:00:00.000Z"}
        raw2 = {"log_id": "abc", "ts": "2026-01-01T00:00:00.001Z"}
        assert compute_raw_log_hash(raw1) != compute_raw_log_hash(raw2)

    def test_timestamp_byte_difference_changes_hash(self) -> None:
        """timestamp 字符串差一个字节时 hash 必须不同。"""
        raw1 = {"timestamp": "2026-06-01T12:00:00Z"}
        raw2 = {"timestamp": "2026-06-01T12:00:01Z"}
        assert compute_raw_log_hash(raw1) != compute_raw_log_hash(raw2)

    def test_matches_manual_jcs_sha256(self) -> None:
        raw = {"z": 2, "a": 1}
        canonical = jcs.canonicalize(raw)
        expected = hashlib.sha256(canonical).hexdigest()
        assert compute_raw_log_hash(raw) == expected

    def test_key_order_independent(self) -> None:
        """JCS 规范化后键顺序不影响结果。"""
        raw_a = {"z": 2, "a": 1}
        raw_b = {"a": 1, "z": 2}
        assert compute_raw_log_hash(raw_a) == compute_raw_log_hash(raw_b)


class TestComputeCurrentHash:
    def _make_hash(
        self,
        audit_id: str = "aud-001",
        log_id: str = "log-001",
        timestamp_str: str = "2026-01-01T00:00:00Z",
        aic: str = "aic-x",
        chain_id: str = "audit-chain-000",
        chain_seq: int = 0,
        raw_log_hash: str = "abc" * 21 + "de",
        previous_hash: str | None = None,
    ) -> str:
        return compute_current_hash(
            audit_id=audit_id,
            log_id=log_id,
            timestamp_str=timestamp_str,
            aic=aic,
            chain_id=chain_id,
            chain_seq=chain_seq,
            raw_log_hash=raw_log_hash,
            previous_hash=previous_hash,
        )

    def test_deterministic(self) -> None:
        assert self._make_hash() == self._make_hash()

    def test_genesis_accepts_none_previous_hash(self) -> None:
        """genesis 记录 previous_hash=None 不应抛出异常。"""
        h = self._make_hash(previous_hash=None, chain_seq=0)
        assert len(h) == 64

    def test_non_genesis_includes_previous_hash(self) -> None:
        prev = "x" * 64
        h_with = self._make_hash(previous_hash=prev, chain_seq=1)
        h_without = self._make_hash(previous_hash=None, chain_seq=1)
        assert h_with != h_without

    def test_preimage_uses_version_1(self) -> None:
        """链前像必须含 "v": 1 字段。"""
        raw_log_hash = "a" * 64
        preimage = {
            "v": 1,
            "auditId": "aud-001",
            "logId": "log-001",
            "timestamp": "2026-01-01T00:00:00Z",
            "aic": "aic-x",
            "chainId": "audit-chain-000",
            "chainSeq": 0,
            "rawLogHash": raw_log_hash,
            "previousHash": None,
        }
        canonical = jcs.canonicalize(preimage)
        expected = hashlib.sha256(canonical).hexdigest()
        result = compute_current_hash(
            audit_id="aud-001",
            log_id="log-001",
            timestamp_str="2026-01-01T00:00:00Z",
            aic="aic-x",
            chain_id="audit-chain-000",
            chain_seq=0,
            raw_log_hash=raw_log_hash,
            previous_hash=None,
        )
        assert result == expected


class TestComputeChainId:
    def test_stable_for_same_aic(self) -> None:
        cid = compute_chain_id("aic-alice", 256)
        assert cid == compute_chain_id("aic-alice", 256)

    def test_format_has_zero_padding(self) -> None:
        cid = compute_chain_id("aic-test", 256)
        assert cid.startswith("audit-chain-")
        num_part = cid.removeprefix("audit-chain-")
        assert len(num_part) == 3  # width = len("255") = 3

    def test_result_within_bounds(self) -> None:
        for count in [16, 64, 256]:
            cid = compute_chain_id("aic-test", count)
            num = int(cid.removeprefix("audit-chain-"))
            assert 0 <= num < count

    @pytest.mark.parametrize("count", [1, 2, 4, 8, 16])
    def test_various_chain_counts(self, count: int) -> None:
        cid = compute_chain_id("aic-x", count)
        assert cid.startswith("audit-chain-")
