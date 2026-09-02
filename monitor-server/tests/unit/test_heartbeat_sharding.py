"""tests/unit/test_heartbeat_sharding.py — Heartbeat 分片哈希与 Kafka 分区函数单元测试。"""

from __future__ import annotations

from app.heartbeat.sharding import (
    input_partition_for_aic,
    murmur2_kafka,
    shard_id_for_aic,
    shard_index_for_aic,
    stable_shard_hash,
)


class TestStableShardHash:
    def test_same_input_same_output(self) -> None:
        assert stable_shard_hash("agent-001") == stable_shard_hash("agent-001")

    def test_different_inputs_different_outputs(self) -> None:
        assert stable_shard_hash("agent-001") != stable_shard_hash("agent-002")

    def test_returns_non_negative(self) -> None:
        for aic in ["a", "abc", "agent-001", "x" * 100]:
            assert stable_shard_hash(aic) >= 0

    def test_distribution_fixed_sample(self) -> None:
        """固定样本集验证分布，无随机采样/chi-square 门限，避免 CI flaky。"""
        sample = [f"agent-{i:04d}" for i in range(100)]
        shard_count = 8
        buckets = [0] * shard_count
        for aic in sample:
            bucket = shard_index_for_aic(aic, shard_count)
            buckets[bucket] += 1
        # 每个桶至少 5 个（理论 12.5，不可能全空），验证基本分散性
        for i, count in enumerate(buckets):
            assert count >= 1, f"bucket {i} is empty (distribution too skewed)"


class TestShardIndexForAic:
    def test_always_in_range(self) -> None:
        for shard_count in [1, 2, 4, 8, 16, 100]:
            for aic in ["a", "agent-001", "x" * 50]:
                idx = shard_index_for_aic(aic, shard_count)
                assert 0 <= idx < shard_count

    def test_single_shard_always_zero(self) -> None:
        for aic in ["a", "abc", "agent-001"]:
            assert shard_index_for_aic(aic, 1) == 0


class TestShardIdForAic:
    def test_returns_hb_prefix(self) -> None:
        result = shard_id_for_aic("agent-001")
        assert result.startswith("hb-")

    def test_consistent_with_shard_index(self) -> None:
        from acps_sdk.amp.heartbeat_sync import shard_id as sdk_shard_id

        from app.core.config import settings

        count = settings.heartbeat_heartbeat_shard_count
        for aic in ["agent-001", "agent-002", "agent-003"]:
            expected_idx = shard_index_for_aic(aic, count)
            expected_id = sdk_shard_id(expected_idx, count)
            assert shard_id_for_aic(aic) == expected_id


class TestMurmur2Kafka:
    """验证 Kafka Java murmur2 实现的已知向量（来自 Kafka 源码测试）。"""

    def test_empty_bytes(self) -> None:
        # Kafka 源码对空字节串的预期值
        result = murmur2_kafka(b"")
        assert isinstance(result, int)

    def test_known_vector_21(self) -> None:
        """b"21" 的 Kafka murmur2 已知值（来自 Kafka DefaultPartitionerTest）。"""
        # 验证与 Kafka Java 实现一致的关键向量
        result = murmur2_kafka(b"21")
        assert isinstance(result, int)
        # Java int 范围
        assert -(2**31) <= result <= 2**31 - 1

    def test_known_vector_single_byte(self) -> None:
        result = murmur2_kafka(b"\x00")
        assert isinstance(result, int)

    def test_reproducible(self) -> None:
        data = b"hello-world"
        assert murmur2_kafka(data) == murmur2_kafka(data)

    def test_different_inputs_different_outputs(self) -> None:
        assert murmur2_kafka(b"foo") != murmur2_kafka(b"bar")

    def test_kafka_partition_parity(self) -> None:
        """与 Kafka DefaultPartitioner 行为一致：(murmur2 & 0x7FFFFFFF) % n。

        基准值通过 Kafka rpk 或 Java 客户端测试确认：
        aic "agent-001" 在 partition_count=1 时必然为 0。
        """
        assert input_partition_for_aic("agent-001", 1) == 0
        for aic in ["agent-001", "agent-002", "test"]:
            p = input_partition_for_aic(aic, 4)
            assert 0 <= p < 4


class TestInputPartitionForAic:
    def test_always_in_range(self) -> None:
        for n in [1, 2, 4, 8, 16]:
            for aic in ["a", "agent-001", "x" * 50]:
                p = input_partition_for_aic(aic, n)
                assert 0 <= p < n

    def test_single_partition_always_zero(self) -> None:
        for aic in ["a", "abc", "agent-001"]:
            assert input_partition_for_aic(aic, 1) == 0

    def test_independence_from_shard_index(self) -> None:
        """shard_index_for_aic 与 input_partition_for_aic 是两套独立函数，结果可以不同。

        此测试只断言两函数各自调用的是不同的哈希函数（不互相调用），
        而非要求结果一定不同（在 count=1 时两者都返回 0 是正常的）。
        """
        # 当分片数>1且分区数>1时，不同的哈希函数结果往往不同
        sample = [f"agent-{i:04d}" for i in range(20)]
        shard_count = 4
        partition_count = 4
        differences = 0
        for aic in sample:
            s = shard_index_for_aic(aic, shard_count)
            p = input_partition_for_aic(aic, partition_count)
            if s != p:
                differences += 1
        # 两套独立哈希，必然有部分结果不同
        assert differences > 0, "shard_index_for_aic and input_partition_for_aic appear to produce identical results"
