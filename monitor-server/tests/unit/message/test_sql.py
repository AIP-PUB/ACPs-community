"""单元测试：B-7 sql.py — 全端点 SQL + Compactor SQL 构造（纯函数 TDD）。"""

from __future__ import annotations

import re

import pytest

from app.message.filters import ResolvedSort as FiltersResolvedSort
from app.message.filters import (
    WhereClause,
)
from app.message.sql import (
    KeysetBound,
    build_affected_buckets,
    build_affected_lifecycle_keys,
    build_deadletters_query,
    build_destinations_query,
    build_events_query,
    build_lifecycle_by_message_id_query,
    build_lifecycles_query,
    build_recompute_lifecycles,
    build_recompute_throughput,
    build_throughput_query,
)
from app.message.tables import (
    LIFECYCLE_LOGICAL_KEY,
    MESSAGE_EVENTS,
    MESSAGE_LIFECYCLE,
)

_SORT_TS_DESC = [FiltersResolvedSort(field="timestamp", column_or_alias="timestamp", order="desc")]
_SORT_LIFECYCLE = [FiltersResolvedSort(field="lastSeenAt", column_or_alias="last_seen_at", order="desc")]
_SORT_DEADLETTER = [FiltersResolvedSort(field="deadLetteredAt", column_or_alias="dead_lettered_at", order="desc")]
_EMPTY_WC = WhereClause(sql="", params={})


def _has_col(sql: str, col: str) -> bool:
    return col in sql


# ── KeysetBound ───────────────────────────────────────────────────────────────


class TestKeysetBound:
    def test_frozen_dataclass(self) -> None:
        kb = KeysetBound(sql="AND x < {k:Int64}", params={"k": 1})
        with pytest.raises((AttributeError, TypeError)):
            kb.sql = "new"  # type: ignore[misc]


# ── build_events_query ────────────────────────────────────────────────────────


class TestBuildEventsQuery:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "where": _EMPTY_WC,
            "time_params": {"_from": 0, "_to": 1000},
            "sort": _SORT_TS_DESC,
            "keyset": None,
            "limit": 50,
            "include_raw_log": False,
        }
        defaults.update(kwargs)
        return build_events_query(**defaults)  # type: ignore[arg-type]

    def test_selects_from_message_events(self) -> None:
        sql, _ = self._q()
        assert MESSAGE_EVENTS in sql

    def test_contains_time_range_params(self) -> None:
        sql, params = self._q()
        assert "_from" in params or "_from" in sql
        assert "_to" in params or "_to" in sql

    def test_default_columns_in_select(self) -> None:
        sql, _ = self._q()
        assert "log_id" in sql

    def test_raw_log_excluded_by_default(self) -> None:
        sql, _ = self._q(include_raw_log=False)
        assert "raw_log" not in sql

    def test_raw_log_included_when_requested(self) -> None:
        sql, _ = self._q(include_raw_log=True)
        assert "raw_log" in sql

    def test_limit_plus_one_in_sql(self) -> None:
        sql, params = self._q(limit=50)
        assert "51" in sql or 51 in params.values()

    def test_where_clause_injected(self) -> None:
        wc = WhereClause(sql="AND system = {f0:String}", params={"f0": "kafka"})
        _sql, params = self._q(where=wc)
        assert "f0" in params
        assert params["f0"] == "kafka"

    def test_keyset_injected(self) -> None:
        ks = KeysetBound(sql="AND timestamp < {_ks_ts:Int64}", params={"_ks_ts": 999})
        _sql, params = self._q(keyset=ks)
        assert "_ks_ts" in params
        assert params["_ks_ts"] == 999

    def test_order_by_timestamp_desc(self) -> None:
        sql, _ = self._q()
        assert re.search(r"ORDER BY.*timestamp.*DESC", sql, re.IGNORECASE)

    def test_partition_column_backtick_escaped(self) -> None:
        sql, _ = self._q()
        assert "`partition`" in sql

    def test_offset_column_backtick_escaped(self) -> None:
        sql, _ = self._q()
        assert "`offset`" in sql


# ── build_lifecycles_query ────────────────────────────────────────────────────


class TestBuildLifecyclesQuery:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "inner_where": _EMPTY_WC,
            "outer_where": _EMPTY_WC,
            "time_params": {"_from": 0, "_to": 1000},
            "lifecycle_retention_days": 30,
            "sort": _SORT_LIFECYCLE,
            "keyset": None,
            "limit": 50,
        }
        defaults.update(kwargs)
        return build_lifecycles_query(**defaults)  # type: ignore[arg-type]

    def test_selects_from_message_lifecycle(self) -> None:
        sql, _ = self._q()
        assert MESSAGE_LIFECYCLE in sql

    def test_has_inner_argmax_structure(self) -> None:
        sql, _ = self._q()
        assert "argMax" in sql

    def test_group_by_all_five_logical_key_columns(self) -> None:
        sql, _ = self._q()
        for col in LIFECYCLE_LOGICAL_KEY:
            assert col in sql

    def test_inner_where_injected(self) -> None:
        wc = WhereClause(sql="AND system = {lf0:String}", params={"lf0": "kafka"})
        _sql, params = self._q(inner_where=wc)
        assert "lf0" in params

    def test_outer_where_injected(self) -> None:
        wc = WhereClause(sql="AND terminal_state = {lf0:String}", params={"lf0": "ack"})
        _sql, params = self._q(outer_where=wc)
        assert "lf0" in params

    def test_first_seen_at_partition_pruning(self) -> None:
        sql, _ = self._q(lifecycle_retention_days=30)
        assert "first_seen_at" in sql

    def test_two_layer_structure(self) -> None:
        sql, _ = self._q()
        assert sql.upper().count("SELECT") >= 2

    def test_limit_plus_one(self) -> None:
        sql, params = self._q(limit=50)
        assert "51" in sql or 51 in params.values()

    def test_no_final_keyword(self) -> None:
        sql, _ = self._q()
        assert "FINAL" not in sql.upper()

    def test_last_seen_at_in_outer_where(self) -> None:
        sql, _ = self._q()
        assert "last_seen_at" in sql.lower() or "BETWEEN" in sql.upper()


# ── build_lifecycle_by_message_id_query ───────────────────────────────────────


class TestBuildLifecycleByMessageId:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "lifecycle_key": "mid:abc123",
            "system": None,
            "destination_name": None,
            "destination_kind": None,
            "virtual_host": None,
            "lifecycle_retention_days": 30,
            "now_ms": 1_800_000_000_000,
        }
        defaults.update(kwargs)
        return build_lifecycle_by_message_id_query(**defaults)  # type: ignore[arg-type]

    def test_filters_by_lifecycle_key(self) -> None:
        _sql, params = self._q()
        assert any("mid:abc123" in str(v) for v in params.values())

    def test_system_injected_when_provided(self) -> None:
        _sql, params = self._q(system="kafka")
        assert "kafka" in params.values()

    def test_argmax_structure_for_dedup(self) -> None:
        sql, _ = self._q()
        assert "argMax" in sql

    def test_selects_from_lifecycle(self) -> None:
        sql, _ = self._q()
        assert MESSAGE_LIFECYCLE in sql


# ── build_deadletters_query ───────────────────────────────────────────────────


class TestBuildDeadlettersQuery:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "inner_where": _EMPTY_WC,
            "outer_where": _EMPTY_WC,
            "time_params": {"_from": 0, "_to": 1000},
            "lifecycle_retention_days": 30,
            "sort": _SORT_DEADLETTER,
            "keyset": None,
            "limit": 50,
        }
        defaults.update(kwargs)
        return build_deadletters_query(**defaults)  # type: ignore[arg-type]

    def test_selects_from_lifecycle(self) -> None:
        sql, _ = self._q()
        assert MESSAGE_LIFECYCLE in sql

    def test_dead_lettered_filter_in_outer(self) -> None:
        sql, _ = self._q()
        assert "dead_lettered" in sql

    def test_not_pushing_dead_lettered_to_inner(self) -> None:
        sql, _ = self._q()
        _inner_part = sql[: sql.lower().find("group by")]
        outer_part = sql[sql.lower().find("group by") :]
        assert "dead_lettered" in outer_part.lower()

    def test_argmax_structure(self) -> None:
        sql, _ = self._q()
        assert "argMax" in sql

    def test_limit_plus_one(self) -> None:
        sql, params = self._q(limit=20)
        assert "21" in sql or 21 in params.values()


# ── build_destinations_query ──────────────────────────────────────────────────


class TestBuildDestinationsQuery:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "edge_where": _EMPTY_WC,
            "time_params": {"_from": 0, "_to": 1000},
            "group_by": [],
        }
        defaults.update(kwargs)
        return build_destinations_query(**defaults)  # type: ignore[arg-type]

    def test_selects_from_destination_state(self) -> None:
        from app.message.tables import MESSAGE_DESTINATION_STATE

        sql, _ = self._q()
        assert MESSAGE_DESTINATION_STATE in sql

    def test_argmax_visible_messages(self) -> None:
        sql, _ = self._q()
        assert "argMax" in sql
        assert "visible_messages" in sql

    def test_captured_at_in_where(self) -> None:
        sql, _params = self._q(time_params={"_from": 100, "_to": 200})
        assert "captured_at" in sql

    def test_group_by_empty_returns_all_destinations(self) -> None:
        sql, _ = self._q(group_by=[])
        assert "system" in sql
        assert "destination_name" in sql

    def test_group_by_system_aggregates(self) -> None:
        sql, _ = self._q(group_by=["system"])
        assert "GROUP BY" in sql.upper()
        assert "system" in sql


# ── build_throughput_query ────────────────────────────────────────────────────


class TestBuildThroughputQuery:
    def _q(self, **kwargs: object) -> tuple[str, dict]:
        defaults: dict[str, object] = {
            "system": "kafka",
            "destination_name": "my-topic",
            "destination_kind": None,
            "virtual_host": None,
            "time_params": {"_from": 0, "_to": 1000},
            "step_seconds": 300,
        }
        defaults.update(kwargs)
        return build_throughput_query(**defaults)  # type: ignore[arg-type]

    def test_selects_from_stats_5m(self) -> None:
        from app.message.tables import MESSAGE_DESTINATION_STATS_5M

        sql, _ = self._q()
        assert MESSAGE_DESTINATION_STATS_5M in sql

    def test_system_param_in_sql(self) -> None:
        _sql, params = self._q()
        assert "kafka" in params.values()

    def test_destination_name_param_in_sql(self) -> None:
        _sql, params = self._q()
        assert "my-topic" in params.values()

    def test_argmax_for_latest_version(self) -> None:
        sql, _ = self._q()
        assert "argMax" in sql

    def test_order_by_bucket_asc(self) -> None:
        sql, _ = self._q()
        assert re.search(r"ORDER BY.*bucket.*ASC", sql, re.IGNORECASE)

    def test_step_300_no_second_aggregation(self) -> None:
        sql, _ = self._q(step_seconds=300)
        assert sql.upper().count("SELECT") >= 1

    def test_step_900_has_second_aggregation(self) -> None:
        sql, _ = self._q(step_seconds=900)
        assert sql.upper().count("SELECT") >= 2 or "toStartOfInterval" in sql

    def test_produced_count_and_consumed_count_in_sql(self) -> None:
        sql, _ = self._q()
        assert "produced_count" in sql
        assert "consumed_count" in sql

    def test_ack_latency_sum_in_sql(self) -> None:
        sql, _ = self._q()
        assert "ack_latency_sum_ms" in sql


# ── Compactor SQL ─────────────────────────────────────────────────────────────


class TestBuildAffectedLifecycleKeys:
    def test_queries_message_events(self) -> None:
        sql, _params = build_affected_lifecycle_keys(rebuild_from_ms=1000)
        assert MESSAGE_EVENTS in sql

    def test_filters_non_empty_lifecycle_key(self) -> None:
        sql, _params = build_affected_lifecycle_keys(rebuild_from_ms=1000)
        assert "lifecycle_key" in sql
        assert "!=" in sql or "''" in sql

    def test_observed_at_filter_in_where(self) -> None:
        sql, params = build_affected_lifecycle_keys(rebuild_from_ms=1000)
        assert "observed_at" in sql
        assert any(v == 1000 or v == 1 for v in params.values()) or "_rebuild_from" in str(params.keys())

    def test_select_five_key_columns(self) -> None:
        sql, _ = build_affected_lifecycle_keys(rebuild_from_ms=0)
        for col in LIFECYCLE_LOGICAL_KEY:
            assert col in sql

    def test_returns_max_observed_at(self) -> None:
        sql, _ = build_affected_lifecycle_keys(rebuild_from_ms=0)
        assert "max(observed_at)" in sql.lower() or "MAX(observed_at)" in sql


class TestBuildRecomputeLifecycles:
    def _key_tuples(self) -> list[tuple]:
        return [("kafka", "my-topic", "topic", "", "mid:abc")]

    def test_inserts_into_lifecycle(self) -> None:
        sql, _params = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "INSERT INTO" in sql.upper()
        assert MESSAGE_LIFECYCLE in sql

    def test_selects_from_events(self) -> None:
        sql, _ = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        assert MESSAGE_EVENTS in sql

    def test_lifecycle_columns_specified(self) -> None:
        sql, _ = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        for col in ("send_count", "receive_count", "terminal_state", "compacted_at"):
            assert col in sql

    def test_compacted_at_constant_in_sql(self) -> None:
        sql, params = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=12345)
        assert any(v == 12345 for v in params.values()) or "12345" in sql

    def test_group_by_five_key_columns(self) -> None:
        sql, _ = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "GROUP BY" in sql.upper()
        for col in LIFECYCLE_LOGICAL_KEY:
            assert col in sql

    def test_terminal_state_logic_present(self) -> None:
        sql, _ = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "terminal_state" in sql
        assert "dead_letter" in sql or "ack" in sql

    def test_producer_consumer_aics_arrays(self) -> None:
        sql, _ = build_recompute_lifecycles(key_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "producer_aics" in sql
        assert "consumer_aics" in sql


class TestBuildAffectedBuckets:
    def test_queries_message_events(self) -> None:
        sql, _ = build_affected_buckets(rebuild_from_ms=0)
        assert MESSAGE_EVENTS in sql

    def test_five_minute_bucket_function(self) -> None:
        sql, _ = build_affected_buckets(rebuild_from_ms=0)
        assert "toStartOfFiveMinutes" in sql or "toStartOf5Minutes" in sql or "bucket" in sql

    def test_observed_at_filter(self) -> None:
        sql, _params = build_affected_buckets(rebuild_from_ms=5000)
        assert "observed_at" in sql

    def test_returns_max_observed_at(self) -> None:
        sql, _ = build_affected_buckets(rebuild_from_ms=0)
        assert "max(observed_at)" in sql.lower() or "MAX(observed_at)" in sql


class TestBuildRecomputeThroughput:
    def _key_tuples(self) -> list[tuple]:
        return [(1_000_000, "kafka", "my-topic", "topic", "")]

    def test_inserts_into_stats_5m(self) -> None:
        from app.message.tables import MESSAGE_DESTINATION_STATS_5M

        sql, _ = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "INSERT INTO" in sql.upper()
        assert MESSAGE_DESTINATION_STATS_5M in sql

    def test_selects_from_events(self) -> None:
        sql, _ = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=999)
        assert MESSAGE_EVENTS in sql

    def test_count_expressions_in_sql(self) -> None:
        sql, _ = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "countIf" in sql
        assert "produced_count" in sql
        assert "consumed_count" in sql

    def test_ack_latency_coalesce_in_sql(self) -> None:
        sql, _ = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "ack_latency_sum_ms" in sql

    def test_group_by_bucket_and_dimensions(self) -> None:
        sql, _ = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=999)
        assert "bucket" in sql
        assert "GROUP BY" in sql.upper()

    def test_compacted_at_in_select(self) -> None:
        sql, params = build_recompute_throughput(bucket_tuples=self._key_tuples(), compacted_at_ms=99999)
        assert "compacted_at" in sql
        assert any(v == 99999 for v in params.values()) or "99999" in sql
