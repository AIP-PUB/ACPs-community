"""单元测试：B-1 tables.py — 四表 DDL 与列常量。"""

from __future__ import annotations

from app.message.tables import (
    INSERT_COLUMNS,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_LOGICAL_KEY,
    MESSAGE_DESTINATION_STATE,
    MESSAGE_DESTINATION_STATS_5M,
    MESSAGE_EVENTS,
    MESSAGE_LIFECYCLE,
    STATE_SNAPSHOT_COLUMNS,
    STATS_5M_COLUMNS,
    all_ddl_statements,
    ddl_message_destination_state_snapshot,
    ddl_message_destination_stats_5m,
    ddl_message_events,
    ddl_message_lifecycle,
)

# ── 表名常量 ──────────────────────────────────────────────────────────────────


class TestTableNameConstants:
    def test_message_events_name(self) -> None:
        assert MESSAGE_EVENTS == "message_events"

    def test_message_lifecycle_name(self) -> None:
        assert MESSAGE_LIFECYCLE == "message_lifecycle"

    def test_message_destination_state_name(self) -> None:
        assert MESSAGE_DESTINATION_STATE == "message_destination_state_snapshot"

    def test_message_destination_stats_5m_name(self) -> None:
        assert MESSAGE_DESTINATION_STATS_5M == "message_destination_stats_5m"


# ── all_ddl_statements 返回 4 条 ─────────────────────────────────────────────


class TestAllDdlStatements:
    def test_returns_four_statements(self) -> None:
        stmts = all_ddl_statements(
            raw_retention_days=7,
            lifecycle_retention_days=30,
            destination_state_retention_days=30,
            destination_stats_retention_days=30,
        )
        assert len(stmts) == 4

    def test_no_mv_ddl(self) -> None:
        stmts = all_ddl_statements(
            raw_retention_days=7,
            lifecycle_retention_days=30,
            destination_state_retention_days=30,
            destination_stats_retention_days=30,
        )
        for stmt in stmts:
            assert "MATERIALIZED VIEW" not in stmt.upper()
            assert "CREATE VIEW" not in stmt.upper()

    def test_order_events_first(self) -> None:
        stmts = all_ddl_statements(
            raw_retention_days=7,
            lifecycle_retention_days=30,
            destination_state_retention_days=30,
            destination_stats_retention_days=30,
        )
        assert "message_events" in stmts[0]
        assert "message_lifecycle" in stmts[1]
        assert "message_destination_state_snapshot" in stmts[2]
        assert "message_destination_stats_5m" in stmts[3]


# ── message_events DDL ────────────────────────────────────────────────────────


class TestMessageEventsDdl:
    def test_if_not_exists(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "IF NOT EXISTS" in ddl

    def test_merge_tree_not_replacing(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "MergeTree" in ddl
        assert "ReplacingMergeTree" not in ddl

    def test_table_name(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "message_events" in ddl

    def test_partition_offset_backtick_escaped(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "`partition`" in ddl
        assert "`offset`" in ddl

    def test_ttl_parameterized(self) -> None:
        ddl7 = ddl_message_events(raw_retention_days=7)
        ddl30 = ddl_message_events(raw_retention_days=30)
        assert "7" in ddl7
        assert "30" in ddl30

    def test_settlement_latency_nullable(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "Nullable" in ddl
        assert "settlement_latency_ms" in ddl

    def test_bloom_filter_indexes(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "bloom_filter" in ddl.lower()

    def test_observed_at_minmax_index(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        assert "observed_at" in ddl
        assert "minmax" in ddl.lower()


# ── message_lifecycle DDL ─────────────────────────────────────────────────────


class TestMessageLifecycleDdl:
    def test_if_not_exists(self) -> None:
        ddl = ddl_message_lifecycle(lifecycle_retention_days=30)
        assert "IF NOT EXISTS" in ddl

    def test_replacing_merge_tree(self) -> None:
        ddl = ddl_message_lifecycle(lifecycle_retention_days=30)
        assert "ReplacingMergeTree" in ddl
        assert "compacted_at" in ddl

    def test_table_name(self) -> None:
        ddl = ddl_message_lifecycle(lifecycle_retention_days=30)
        assert "message_lifecycle" in ddl

    def test_partition_by_first_seen_at(self) -> None:
        ddl = ddl_message_lifecycle(lifecycle_retention_days=30)
        assert "first_seen_at" in ddl
        assert "toYYYYMM" in ddl

    def test_order_by_five_tuple(self) -> None:
        ddl = ddl_message_lifecycle(lifecycle_retention_days=30)
        assert "lifecycle_key" in ddl
        assert "destination_name" in ddl
        assert "destination_kind" in ddl
        assert "virtual_host" in ddl
        assert "system" in ddl


# ── message_destination_state_snapshot DDL ───────────────────────────────────


class TestMessageDestinationStateDdl:
    def test_if_not_exists(self) -> None:
        ddl = ddl_message_destination_state_snapshot(destination_state_retention_days=30)
        assert "IF NOT EXISTS" in ddl

    def test_replacing_merge_tree_captured_at(self) -> None:
        ddl = ddl_message_destination_state_snapshot(destination_state_retention_days=30)
        assert "ReplacingMergeTree" in ddl
        assert "captured_at" in ddl

    def test_table_name(self) -> None:
        ddl = ddl_message_destination_state_snapshot(destination_state_retention_days=30)
        assert "message_destination_state_snapshot" in ddl


# ── message_destination_stats_5m DDL ─────────────────────────────────────────


class TestMessageDestinationStats5mDdl:
    def test_if_not_exists(self) -> None:
        ddl = ddl_message_destination_stats_5m(destination_stats_retention_days=30)
        assert "IF NOT EXISTS" in ddl

    def test_replacing_merge_tree_compacted_at(self) -> None:
        ddl = ddl_message_destination_stats_5m(destination_stats_retention_days=30)
        assert "ReplacingMergeTree" in ddl
        assert "compacted_at" in ddl

    def test_table_name(self) -> None:
        ddl = ddl_message_destination_stats_5m(destination_stats_retention_days=30)
        assert "message_destination_stats_5m" in ddl

    def test_order_by_bucket(self) -> None:
        ddl = ddl_message_destination_stats_5m(destination_stats_retention_days=30)
        assert "bucket" in ddl


# ── 列序常量 ──────────────────────────────────────────────────────────────────


class TestColumnConstants:
    def test_insert_columns_contains_key_fields(self) -> None:
        assert "log_id" in INSERT_COLUMNS
        assert "timestamp" in INSERT_COLUMNS
        assert "observed_at" in INSERT_COLUMNS
        assert "lifecycle_key" in INSERT_COLUMNS
        assert "raw_log" in INSERT_COLUMNS
        assert "partition" in INSERT_COLUMNS
        assert "offset" in INSERT_COLUMNS

    def test_insert_columns_consistent_with_events_ddl(self) -> None:
        ddl = ddl_message_events(raw_retention_days=7)
        for col in INSERT_COLUMNS:
            assert col in ddl, f"Column '{col}' in INSERT_COLUMNS but missing from DDL"

    def test_lifecycle_columns_has_compacted_at(self) -> None:
        assert "compacted_at" in LIFECYCLE_COLUMNS

    def test_lifecycle_read_columns_no_compacted_at(self) -> None:
        from app.message.tables import LIFECYCLE_READ_COLUMNS

        assert "compacted_at" not in LIFECYCLE_READ_COLUMNS

    def test_lifecycle_logical_key_five_tuple(self) -> None:
        assert len(LIFECYCLE_LOGICAL_KEY) == 5
        assert "lifecycle_key" in LIFECYCLE_LOGICAL_KEY
        assert "system" in LIFECYCLE_LOGICAL_KEY
        assert "destination_name" in LIFECYCLE_LOGICAL_KEY
        assert "destination_kind" in LIFECYCLE_LOGICAL_KEY
        assert "virtual_host" in LIFECYCLE_LOGICAL_KEY

    def test_state_snapshot_columns_has_captured_at(self) -> None:
        assert "captured_at" in STATE_SNAPSHOT_COLUMNS

    def test_stats_5m_columns_has_bucket(self) -> None:
        assert "bucket" in STATS_5M_COLUMNS
        assert "compacted_at" in STATS_5M_COLUMNS
        assert "ack_latency_sum_ms" in STATS_5M_COLUMNS
        assert "ack_sample_count" in STATS_5M_COLUMNS
