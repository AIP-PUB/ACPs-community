"""tests/unit/system/test_indices.py — indices.py 单元测试。"""

from __future__ import annotations

from app.system.indices import (
    EVENT_SOURCE_FIELDS,
    FIELD_INDEXED_AT,
    FIELD_RAW_BODY,
    FIELD_SEARCH_TEXT,
    INDEX_PATTERN,
    INDEX_PREFIX,
    build_index_template,
    build_ism_policy,
    index_for_timestamp,
    query_index_target,
)


class TestIndexForTimestamp:
    """index_for_timestamp 按 UTC 毫秒生成 amp-system-events-YYYYMMDD。"""

    def test_basic_utc(self) -> None:
        # 2024-06-14 00:00:00 UTC → amp-system-events-20240614
        ts_ms = 1718323200000  # 2024-06-14 00:00:00 UTC
        assert index_for_timestamp(ts_ms) == "amp-system-events-20240614"

    def test_cross_day_boundary_utc_midnight(self) -> None:
        """UTC 23:59:59.999 与 UTC 00:00:00.000 落到不同日期索引。"""
        utc_235959 = 1718409599999  # 2024-06-14 23:59:59.999 UTC
        utc_000000 = 1718409600000  # 2024-06-15 00:00:00.000 UTC
        assert index_for_timestamp(utc_235959) == "amp-system-events-20240614"
        assert index_for_timestamp(utc_000000) == "amp-system-events-20240615"

    def test_epoch(self) -> None:
        """epoch 0 → 1970-01-01 UTC。"""
        assert index_for_timestamp(0) == "amp-system-events-19700101"

    def test_prefix_format(self) -> None:
        ts_ms = 1700000000000
        result = index_for_timestamp(ts_ms)
        assert result.startswith(INDEX_PREFIX + "-")
        date_part = result[len(INDEX_PREFIX) + 1 :]
        assert len(date_part) == 8
        assert date_part.isdigit()

    def test_index_uses_event_timestamp_not_now(self) -> None:
        """同一调用时刻，传入不同事件时间戳应产生不同索引名。"""
        ts_2020 = 1577836800000  # 2020-01-01 UTC
        ts_2024 = 1704067200000  # 2024-01-01 UTC
        assert index_for_timestamp(ts_2020) != index_for_timestamp(ts_2024)


class TestBuildIndexTemplate:
    """build_index_template 产出符合设计 §4.1 的 mapping。"""

    def _tpl(self, shards: int = 3, replicas: int = 1) -> dict:
        return build_index_template(number_of_shards=shards, number_of_replicas=replicas)

    def test_index_patterns(self) -> None:
        tpl = self._tpl()
        assert INDEX_PATTERN in tpl["index_patterns"]

    def test_raw_body_enabled_false(self) -> None:
        """raw_body 默认不深层索引（C-SYSTEM-WRITE-4）。"""
        props = self._tpl()["template"]["mappings"]["properties"]
        raw_body = props["raw_body"]
        assert raw_body["type"] == "object"
        assert raw_body["enabled"] is False

    def test_tags_is_flat_object(self) -> None:
        """tags → flat_object（dot-path 大小写敏感精确匹配，设计 §4.1）。"""
        props = self._tpl()["template"]["mappings"]["properties"]
        assert props["tags"]["type"] == "flat_object"

    def test_message_keyword_ignore_above_256(self) -> None:
        """message.keyword.ignore_above=256（设计 §4.1）。"""
        props = self._tpl()["template"]["mappings"]["properties"]
        msg = props["message"]
        assert msg["type"] == "text"
        assert msg["fields"]["keyword"]["ignore_above"] == 256

    def test_search_text_is_text(self) -> None:
        props = self._tpl()["template"]["mappings"]["properties"]
        assert props["search_text"]["type"] == "text"

    def test_timestamp_and_indexed_at_are_date(self) -> None:
        props = self._tpl()["template"]["mappings"]["properties"]
        assert props["timestamp"]["type"] == "date"
        assert props["indexed_at"]["type"] == "date"

    def test_severity_number_is_short(self) -> None:
        props = self._tpl()["template"]["mappings"]["properties"]
        assert props["severity_number"]["type"] == "short"

    def test_keyword_fields_are_keyword_type(self) -> None:
        props = self._tpl()["template"]["mappings"]["properties"]
        keyword_fields = (
            "log_id",
            "aic",
            "trace_id",
            "correlation_id",
            "severity_text",
            "category",
            "component",
            "module",
        )
        for field in keyword_fields:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_no_deprecated_policy_id_in_settings(self) -> None:
        """settings 不含废弃的 policy_id（ISM 靠 ism_template 自动挂载，设计 §3.3）。"""
        settings_dict = self._tpl()["template"].get("settings", {})
        settings_str = str(settings_dict)
        assert "policy_id" not in settings_str
        assert "index_state_management" not in settings_str

    def test_shards_and_replicas_from_params(self) -> None:
        tpl = self._tpl(shards=5, replicas=2)
        settings_dict = tpl["template"]["settings"]
        assert settings_dict["number_of_shards"] == 5
        assert settings_dict["number_of_replicas"] == 2


class TestBuildIsmPolicy:
    """build_ism_policy 产出符合设计 §3.3 的 ISM 策略。"""

    def _policy(self, hot_days: int = 3, warm_days: int = 14, archive_days: int = 30) -> dict:
        return build_ism_policy(hot_days=hot_days, warm_days=warm_days, archive_days=archive_days)

    def test_ism_template_index_patterns(self) -> None:
        policy = self._policy()
        ism_tpl = policy["policy"]["ism_template"][0]
        assert INDEX_PATTERN in ism_tpl["index_patterns"]

    def test_ism_template_priority_100(self) -> None:
        policy = self._policy()
        ism_tpl = policy["policy"]["ism_template"][0]
        assert ism_tpl["priority"] == 100

    def test_hot_transition_uses_hot_days(self) -> None:
        policy = self._policy(hot_days=7)
        states = {s["name"]: s for s in policy["policy"]["states"]}
        hot_state = states["hot"]
        transition = hot_state["transitions"][0]
        assert transition["conditions"]["min_index_age"] == "7d"

    def test_warm_transition_uses_warm_days(self) -> None:
        policy = self._policy(warm_days=21)
        states = {s["name"]: s for s in policy["policy"]["states"]}
        warm_state = states["warm"]
        transition = warm_state["transitions"][0]
        assert transition["conditions"]["min_index_age"] == "21d"

    def test_cold_transition_uses_archive_days(self) -> None:
        policy = self._policy(archive_days=90)
        states = {s["name"]: s for s in policy["policy"]["states"]}
        cold_state = states["cold"]
        transition = cold_state["transitions"][0]
        assert transition["conditions"]["min_index_age"] == "90d"

    def test_four_states_exist(self) -> None:
        policy = self._policy()
        state_names = {s["name"] for s in policy["policy"]["states"]}
        assert state_names == {"hot", "warm", "cold", "delete"}

    def test_delete_state_has_delete_action(self) -> None:
        policy = self._policy()
        states = {s["name"]: s for s in policy["policy"]["states"]}
        delete_state = states["delete"]
        action_types = [next(iter(a.keys())) for a in delete_state["actions"]]
        assert "delete" in action_types

    def test_default_state_is_hot(self) -> None:
        policy = self._policy()
        assert policy["policy"]["default_state"] == "hot"


class TestEventSourceFields:
    """EVENT_SOURCE_FIELDS 不含内部字段（C-SYSTEM-QUERY-3 边界）。"""

    def test_not_contains_search_text(self) -> None:
        assert FIELD_SEARCH_TEXT not in EVENT_SOURCE_FIELDS

    def test_not_contains_indexed_at(self) -> None:
        assert FIELD_INDEXED_AT not in EVENT_SOURCE_FIELDS

    def test_not_contains_raw_body(self) -> None:
        """raw_body 由 includeRawLog 门控追加，不在基础投影中。"""
        assert FIELD_RAW_BODY not in EVENT_SOURCE_FIELDS

    def test_contains_core_fields(self) -> None:
        for field in ("log_id", "timestamp", "aic", "message", "severity_number"):
            assert field in EVENT_SOURCE_FIELDS, f"{field} should be in EVENT_SOURCE_FIELDS"

    def test_is_tuple(self) -> None:
        assert isinstance(EVENT_SOURCE_FIELDS, tuple)


class TestQueryIndexTarget:
    def test_returns_index_pattern(self) -> None:
        assert query_index_target() == INDEX_PATTERN

    def test_is_pure_function_no_io(self) -> None:
        """多次调用结果一致（无 I/O，纯函数）。"""
        assert query_index_target() == query_index_target()
