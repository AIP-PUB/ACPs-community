"""tests/unit/test_heartbeat_redis_keys.py — Redis key 构造器单元测试（C-SHARD-1）。"""

from __future__ import annotations

import pytest

from app.heartbeat.redis_keys import (
    WATERMARKS_KEY,
    delta_outbox_key,
    delta_seq_key,
    latest_key,
    liveness_zset_key,
    published_seq_key,
    relay_epoch_key,
    relay_lock_key,
    scan_lock_key,
)

SHARD = "hb-000"
AIC = "agent-001"

# 带分片参数的 key 函数（不含 latest_key，latest_key 需要额外的 aic 参数）
SHARD_KEY_FUNCS = [
    liveness_zset_key,
    delta_seq_key,
    delta_outbox_key,
    published_seq_key,
    scan_lock_key,
    relay_lock_key,
    relay_epoch_key,
]


class TestKeyHashTag:
    @pytest.mark.parametrize("fn", SHARD_KEY_FUNCS)
    def test_contains_hash_tag(self, fn) -> None:  # type: ignore[no-untyped-def]
        """所有带 shard 参数的 key 函数返回值必须含 {hb-N} hash tag（C-SHARD-1）。"""
        key = fn(SHARD)
        assert f"{{{SHARD}}}" in key, f"key '{key}' missing hash tag {{{SHARD}}}"

    def test_latest_key_contains_hash_tag(self) -> None:
        key = latest_key(SHARD, AIC)
        assert f"{{{SHARD}}}" in key, f"latest_key '{key}' missing hash tag"

    def test_latest_key_contains_aic(self) -> None:
        key = latest_key(SHARD, AIC)
        assert AIC in key

    def test_watermarks_key_no_hash_tag(self) -> None:
        """WATERMARKS_KEY 是跨 shard 聚合键，无 hash tag（§4.2）。"""
        assert "{" not in WATERMARKS_KEY, f"WATERMARKS_KEY should not contain hash tag: {WATERMARKS_KEY}"


class TestKeyUniqueness:
    def test_different_shards_different_keys(self) -> None:
        assert liveness_zset_key("hb-000") != liveness_zset_key("hb-001")

    def test_latest_key_different_aics_different_keys(self) -> None:
        assert latest_key(SHARD, "aic-1") != latest_key(SHARD, "aic-2")

    def test_all_key_functions_produce_different_keys_for_same_shard(self) -> None:
        keys = [fn(SHARD) for fn in SHARD_KEY_FUNCS]
        assert len(set(keys)) == len(keys), "Key functions produced duplicate keys"


class TestKeyFormat:
    def test_latest_key_format(self) -> None:
        assert latest_key("hb-000", "agent-001") == "amp:hb:{hb-000}:latest:agent-001"

    def test_liveness_zset_key_format(self) -> None:
        assert liveness_zset_key("hb-000") == "amp:hb:{hb-000}:liveness_zset"

    def test_delta_seq_key_format(self) -> None:
        assert delta_seq_key("hb-000") == "amp:hb:{hb-000}:delta_seq"

    def test_delta_outbox_key_format(self) -> None:
        assert delta_outbox_key("hb-000") == "amp:hb:{hb-000}:delta_outbox"

    def test_published_seq_key_format(self) -> None:
        assert published_seq_key("hb-000") == "amp:hb:{hb-000}:published_seq"

    def test_scan_lock_key_format(self) -> None:
        assert scan_lock_key("hb-000") == "amp:hb:{hb-000}:scan_lock"

    def test_relay_lock_key_format(self) -> None:
        assert relay_lock_key("hb-000") == "amp:hb:{hb-000}:relay_lock"

    def test_relay_epoch_key_format(self) -> None:
        assert relay_epoch_key("hb-000") == "amp:hb:{hb-000}:relay_epoch"

    def test_watermarks_key_format(self) -> None:
        assert WATERMARKS_KEY == "amp:hb:writer_watermarks"
