from __future__ import annotations

from pathlib import Path

import pytest
from click import ClickException

from acps_cli.monitor.commands import (
    append_filter_condition,
    load_json_source,
    merge_page,
    merge_time_range,
    require_request_payload,
)


def test_load_json_source_accepts_inline_object() -> None:
    payload = load_json_source(request_json='{"metric":"cpu"}', request_file=None)

    assert payload == {"metric": "cpu"}


def test_load_json_source_accepts_file_object(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text('{"metric":"cpu"}', encoding="utf-8")

    payload = load_json_source(request_json=None, request_file=request_file)

    assert payload == {"metric": "cpu"}


def test_load_json_source_rejects_multiple_sources(tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ClickException, match="cannot be used together"):
        load_json_source(request_json="{}", request_file=request_file)


def test_load_json_source_rejects_invalid_json() -> None:
    with pytest.raises(ClickException, match="Invalid request JSON"):
        load_json_source(request_json="{", request_file=None)


def test_load_json_source_rejects_non_object_json() -> None:
    with pytest.raises(ClickException, match="request JSON must be an object"):
        load_json_source(request_json="[]", request_file=None)


def test_merge_time_range_sets_payload() -> None:
    payload: dict[str, object] = {}

    merge_time_range(payload, "2026-06-25T00:00:00Z", "2026-06-25T01:00:00Z")

    assert payload["timeRange"] == {
        "startAt": "2026-06-25T00:00:00Z",
        "endAt": "2026-06-25T01:00:00Z",
    }


def test_merge_time_range_requires_start_and_end_together() -> None:
    with pytest.raises(ClickException, match="--start and --end must be used together"):
        merge_time_range({}, "2026-06-25T00:00:00Z", None)


def test_merge_page_sets_limit_and_cursor() -> None:
    payload: dict[str, object] = {}

    merge_page(payload, 50, "cursor-1")

    assert payload["page"] == {"limit": 50, "cursor": "cursor-1"}


def test_append_filter_condition_builds_simple_and_filter() -> None:
    payload: dict[str, object] = {}

    append_filter_condition(payload, "aic", "eq", "AIC-001")
    append_filter_condition(payload, "traceId", "eq", "trace-001")

    assert payload["filter"] == {
        "logic": "and",
        "conditions": [
            {"field": "aic", "op": "eq", "value": "AIC-001"},
            {"field": "traceId", "op": "eq", "value": "trace-001"},
        ],
    }


def test_append_filter_condition_rejects_grouped_filter() -> None:
    payload = {
        "filter": {
            "logic": "and",
            "conditions": [],
            "groups": [{"logic": "or", "conditions": []}],
        }
    }

    with pytest.raises(ClickException, match="logic=and and no groups"):
        append_filter_condition(payload, "aic", "eq", "AIC-001")


def test_append_filter_condition_rejects_non_and_logic() -> None:
    payload = {"filter": {"logic": "or", "conditions": []}}

    with pytest.raises(ClickException, match="logic=and and no groups"):
        append_filter_condition(payload, "aic", "eq", "AIC-001")


def test_require_request_payload_rejects_missing_request() -> None:
    with pytest.raises(ClickException, match="requires --request-json or --request-file"):
        require_request_payload(None, "access operations")
