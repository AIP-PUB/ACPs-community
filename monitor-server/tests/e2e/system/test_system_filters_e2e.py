"""E2E — System 过滤器 DSL（H-1 filters 场景）。

验收项：
- eq 大小写不敏感 / eqCs 大小写敏感
- tags.* eqCs 命中；tags.* eq → UNSUPPORTED_OPERATOR
- severityNumber range
- message contains → UNSUPPORTED_OPERATOR
- rawBody 深层 → UNSUPPORTED_FIELD
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.e2e.system.helpers import system_time_range_body
from tests.support.factory import make_system_log_record
from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_eq_case_insensitive_aic(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """eq 对 keyword 字段大小写不敏感命中。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic = f"Aic-Eq-{tag}"
    log_id = await produce_system_event(aic=aic, message=f"eq-case-{tag}")
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "aic", "op": "eq", "value": aic.lower()}],
        ),
    )
    assert resp.status_code == 200
    assert any(item.get("logId") == log_id for item in resp.json().get("items", []))


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_eqcs_case_sensitive_category(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """eqCs 大小写敏感：错误大小写不命中，正确大小写命中。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    category = "ErrorType"
    record = make_system_log_record(message=f"eqcs-{tag}", category=category)
    log_id = await produce_system_event(record=record)
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    miss = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "category", "op": "eqCs", "value": category.lower()}],
        ),
    )
    assert miss.status_code == 200
    assert not any(item.get("logId") == log_id for item in miss.json().get("items", []))

    hit = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "category", "op": "eqCs", "value": category}],
        ),
    )
    assert hit.status_code == 200
    assert any(item.get("logId") == log_id for item in hit.json().get("items", []))


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_tags_eqcs_hits_and_eq_unsupported(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """tags.* eqCs 命中；tags.* eq → AMP_UNSUPPORTED_OPERATOR。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    tag = uuid.uuid4().hex[:8]
    env_val = f"prod-{tag}"
    record = make_system_log_record(message=f"tags-{tag}", tags={"env": env_val})
    log_id = await produce_system_event(record=record)
    await wait_for_system_event_ingested(log_id, timeout_s=30)

    hit = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "tags.env", "op": "eqCs", "value": env_val}],
        ),
    )
    assert hit.status_code == 200
    assert any(item.get("logId") == log_id for item in hit.json().get("items", []))

    bad = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "tags.env", "op": "eq", "value": env_val}],
        ),
    )
    assert bad.status_code == 422
    assert bad.json().get("error_code") == SystemErrorCode.UNSUPPORTED_OPERATOR


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_severity_number_range(
    e2e_system_runtime: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """severityNumber gte 过滤数值范围。"""
    from app.core.config import settings

    tag = uuid.uuid4().hex[:8]
    aic = f"aic-sev-{tag}"
    low_id = await produce_system_event(aic=aic, message="sev-low", severity_number=5)
    high_id = await produce_system_event(aic=aic, message="sev-high", severity_number=15)
    await wait_for_system_event_ingested(high_id, timeout_s=30)

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            aic=aic,
            filter_conditions=[{"field": "severityNumber", "op": "gte", "value": 10}],
        ),
    )
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    log_ids = {item.get("logId") for item in items}
    assert high_id in log_ids
    assert low_id not in log_ids


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_message_contains_unsupported_operator(
    e2e_http_client: AsyncClient,
) -> None:
    """message contains → AMP_UNSUPPORTED_OPERATOR（全文走 keyword 参数）。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "message", "op": "contains", "value": "error"}],
        ),
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == SystemErrorCode.UNSUPPORTED_OPERATOR


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_rawbody_deep_path_unsupported_field(
    e2e_http_client: AsyncClient,
) -> None:
    """rawBody 深层路径 → AMP_UNSUPPORTED_FIELD（C-SYSTEM-QUERY-3）。"""
    from app.core.config import settings
    from app.system.exception import SystemErrorCode

    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/system/events/query",
        json=system_time_range_body(
            filter_conditions=[{"field": "rawBody.nested.key", "op": "eq", "value": "x"}],
        ),
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == SystemErrorCode.UNSUPPORTED_FIELD
