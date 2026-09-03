"""monitor-server 联机测试数据辅助脚本。

该脚本由 acps-cli 测试通过 subprocess 调用，运行在 monitor-server 的
.venv 中，用于准备 live integration / e2e 所需的测试数据。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
MONITOR_REPO = WORKSPACE_ROOT / "monitor-server"
sys.path.insert(0, str(MONITOR_REPO))

MONITOR_BASE_URL = os.getenv("MONITOR_BASE_URL", "http://localhost:9009").rstrip("/")
API_PREFIX = "/acps-amp-v1"


def _now_window(*, lookback_minutes: int = 5, lookahead_minutes: int = 5) -> tuple[datetime, str, str]:
    now = datetime.now(UTC)
    start = (now - timedelta(minutes=lookback_minutes)).isoformat()
    end = (now + timedelta(minutes=lookahead_minutes)).isoformat()
    return now, start, end


def _now_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _load_demo_audit_signer() -> tuple[Any, str, str]:
    """从 audit_keys.json 读取一个可用的开发签名密钥。"""

    keys_path = MONITOR_REPO / "config" / "audit_keys.json"
    entries = json.loads(keys_path.read_text(encoding="utf-8"))
    demo = entries["demo_leader"]
    private_key = load_pem_private_key(demo["private_key"].encode("utf-8"), password=None)
    return private_key, str(demo["kid"]), str(demo["aic"])


async def _poll_json(
    method: str,
    path: str,
    *,
    expected: Any,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 20.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """轮询 monitor Query API，直到 expected 返回 True。"""

    import httpx

    deadline = time.monotonic() + timeout_seconds
    url = f"{MONITOR_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                if method == "GET":
                    response = await client.get(url)
                else:
                    response = await client.post(url, json=payload)
                if response.status_code == 200:
                    body = response.json()
                    if expected(body):
                        return body
            except httpx.HTTPError:
                pass
            await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError(f"轮询 monitor Query API 超时：{method} {url}")


async def _heartbeat_direct() -> dict[str, Any]:
    from app.core.redis_client import get_redis

    from tests.support.redis_helper import ensure_functions_for_tests, seed_heartbeat

    now, _start, _end = _now_window()
    aic = f"urn:acps-cli:monitor:hb:direct:{uuid.uuid4().hex[:8]}"
    redis = get_redis()
    await ensure_functions_for_tests(redis)
    await seed_heartbeat(redis, aic=aic, observed_at_ms=_now_ms(now), source_timestamp_ms=_now_ms(now))
    return {"aic": aic}


async def _metrics_direct() -> dict[str, Any]:
    from acps_sdk.amp.models import LoadMetrics
    from app.core.redis_client import get_redis
    from app.metrics.snapshot_cache import CachedSnapshot, upsert_snapshot

    from tests.support.redis_helper import seed_watermark

    now, _start, _end = _now_window()
    aic = f"urn:acps-cli:monitor:metrics:direct:{uuid.uuid4().hex[:8]}"
    observed_at_ms = _now_ms(now)
    redis = get_redis()
    await upsert_snapshot(
        redis,
        CachedSnapshot(
            aic=aic,
            observed_at_ms=observed_at_ms,
            uptime_seconds=321.0,
            load_metrics=LoadMetrics(active_tasks=2, queued_tasks=1),
            window_metrics=[],
            service_name="acps-cli-monitor-live",
            service_namespace="acps-cli",
            deployment_env="testing",
        ),
    )
    await seed_watermark(redis, observed_at_ms)
    return {"aic": aic}


async def _access_direct() -> dict[str, Any]:
    from app.access import freshness
    from app.core.redis_client import get_redis

    from tests.support.clickhouse_helper import ensure_test_schema, insert_raw_events, make_access_event_row
    from tests.support.redis_helper import reset_access_redis_state

    now, start_at, end_at = _now_window()
    observed_at_ms = _now_ms(now)
    aic = f"urn:acps-cli:monitor:access:direct:{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex

    await ensure_test_schema()
    row = make_access_event_row(
        log_id=log_id,
        aic=aic,
        trace_id=trace_id,
        timestamp_ms=observed_at_ms,
        observed_at_ms=observed_at_ms,
        request_method="POST",
        request_route="/rpc/acps-cli/live-access",
        request_url="/rpc/acps-cli/live-access",
        response_status=201,
        duration_ms=88,
        service_name="acps-cli-monitor-live",
    )
    await insert_raw_events([row])

    redis = get_redis()
    await reset_access_redis_state(redis)
    await freshness.advance_partition_watermark(
        redis,
        partition_id=0,
        batch_max_ts_ms=observed_at_ms,
        now_ms=observed_at_ms,
    )
    return {
        "aic": aic,
        "log_id": log_id,
        "trace_id": trace_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _message_direct() -> dict[str, Any]:
    from app.core.redis_client import get_redis
    from app.message import freshness

    from tests.support.clickhouse_helper import ensure_test_schema, insert_message_events, make_message_event_row
    from tests.support.redis_helper import reset_message_redis_state

    now, start_at, end_at = _now_window()
    observed_at_ms = _now_ms(now)
    log_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex

    await ensure_test_schema()
    row = make_message_event_row(
        log_id=log_id,
        message_id=message_id,
        trace_id=trace_id,
        timestamp_ms=observed_at_ms,
        observed_at_ms=observed_at_ms,
        aic=f"urn:acps-cli:monitor:message:direct:{uuid.uuid4().hex[:8]}",
        system="kafka",
        destination_name="acps-cli-monitor-live",
        destination_kind="topic",
        direction="send",
        event_type="send",
    )
    await insert_message_events([row])

    redis = get_redis()
    await reset_message_redis_state(redis)
    await freshness.advance_partition_watermark(
        redis,
        partition_id=0,
        batch_max_ts_ms=observed_at_ms,
        now_ms=observed_at_ms,
    )
    return {
        "message_id": message_id,
        "trace_id": trace_id,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _system_direct() -> dict[str, Any]:
    from acps_sdk.amp.models import LogRecord
    from app.core.config import settings
    from app.core.redis_client import get_redis
    from app.system import freshness
    from app.system.normalizer import build_document

    from tests.support.factory import make_system_log_record
    from tests.support.opensearch_helper import bulk_insert, create_test_index
    from tests.support.redis_helper import reset_system_redis_state

    now, start_at, end_at = _now_window()
    observed_at_ms = _now_ms(now)
    aic = f"urn:acps-cli:monitor:system:direct:{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    record = make_system_log_record(
        aic=aic,
        log_id=log_id,
        message="acps-cli monitor live system direct",
        timestamp=now.isoformat(),
        observed_timestamp=now.isoformat(),
        severity_number=11,
    )
    await create_test_index(timestamp_iso=now.isoformat())
    doc = build_document(
        LogRecord.model_validate(record),
        log_id=log_id,
        search_text_max_length=settings.system_search_text_max_length,
    )
    await bulk_insert([doc], indexed_at_iso=now.isoformat(), refresh=True)

    redis = get_redis()
    await reset_system_redis_state(redis)
    await freshness.advance_partition_watermark(
        redis,
        partition_id=0,
        batch_max_event_ts_ms=observed_at_ms,
        now_ms=observed_at_ms,
        reorder_margin_ms=0,
    )
    return {
        "aic": aic,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _audit_direct() -> dict[str, Any]:
    from app.audit.key_resolver import MockKeyResolver
    from app.audit.writer import AuditWriter
    from app.core.amp_schema import LogRecord
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.integration.conftest import make_signed_log_record

    now, start_at, end_at = _now_window()
    private_key = Ed25519PrivateKey.generate()
    kid = f"acps-cli-live-{uuid.uuid4().hex[:8]}"
    public_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    writer = AuditWriter(key_resolver=MockKeyResolver({kid: public_pem}))
    aic = f"urn:acps-cli:monitor:audit:direct:{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    raw = make_signed_log_record(
        private_key,
        kid=kid,
        aic=aic,
        log_id=log_id,
        timestamp=now.isoformat(),
    )
    await writer._process_audit_record(LogRecord.model_validate(raw), raw)
    return {
        "aic": aic,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _heartbeat_e2e() -> dict[str, Any]:
    from tests.support.kafka_helper import produce_heartbeat

    aic = f"urn:acps-cli:monitor:hb:e2e:{uuid.uuid4().hex[:8]}"
    await produce_heartbeat(aic)
    await _poll_json(
        "GET",
        f"{API_PREFIX}/heartbeat/liveness/{aic}",
        expected=lambda body: bool(body.get("data", {}).get("isAlive")),
        timeout_seconds=15.0,
    )
    return {"aic": aic}


async def _metrics_e2e() -> dict[str, Any]:
    from tests.support.kafka_helper import produce_metrics

    aic = f"urn:acps-cli:monitor:metrics:e2e:{uuid.uuid4().hex[:8]}"
    uptime = 456.0
    await produce_metrics(aic, uptime_seconds=uptime)
    await _poll_json(
        "POST",
        f"{API_PREFIX}/metrics/snapshots/query",
        payload={"filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}, "page": {"limit": 1}},
        expected=lambda body: bool(body.get("items")) and body["items"][0].get("uptimeSeconds") == uptime,
        timeout_seconds=20.0,
    )
    return {"aic": aic}


async def _access_e2e() -> dict[str, Any]:
    from app.core.redis_client import get_redis

    from tests.support.kafka_helper import produce_access_event, wait_for_access_event_ingested
    from tests.support.redis_helper import reset_access_redis_state

    _now, start_at, end_at = _now_window()
    aic = f"urn:acps-cli:monitor:access:e2e:{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    redis = get_redis()
    await reset_access_redis_state(redis)
    await produce_access_event(
        aic=aic,
        log_id=log_id,
        trace_id=trace_id,
        method="POST",
        route="/rpc/acps-cli/e2e-access",
        response_status=202,
        duration_ms=66,
    )
    await wait_for_access_event_ingested(log_id, timeout_s=25.0)
    return {
        "aic": aic,
        "trace_id": trace_id,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _message_e2e() -> dict[str, Any]:
    from app.core.redis_client import get_redis

    from tests.support.kafka_helper import produce_message_event, wait_for_message_event_ingested
    from tests.support.redis_helper import reset_message_redis_state

    _now, start_at, end_at = _now_window()
    log_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    redis = get_redis()
    await reset_message_redis_state(redis)
    await produce_message_event(
        aic=f"urn:acps-cli:monitor:message:e2e:{uuid.uuid4().hex[:8]}",
        log_id=log_id,
        message_id=message_id,
        event_type="send",
    )
    await wait_for_message_event_ingested(log_id, timeout_s=25.0)
    return {
        "message_id": message_id,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _system_e2e() -> dict[str, Any]:
    from app.core.redis_client import get_redis

    from tests.support.factory import make_system_log_record
    from tests.support.kafka_helper import produce_system_event, wait_for_system_event_ingested
    from tests.support.redis_helper import reset_system_redis_state

    now, start_at, end_at = _now_window()
    aic = f"urn:acps-cli:monitor:system:e2e:{uuid.uuid4().hex[:8]}"
    correlation_id = f"acps-cli-monitor-{uuid.uuid4().hex[:12]}"
    log_id = str(uuid.uuid4())
    record = make_system_log_record(
        aic=aic,
        log_id=log_id,
        correlation_id=correlation_id,
        message="acps-cli monitor live system e2e",
        observed_timestamp=now.isoformat(),
        timestamp=now.isoformat(),
    )
    redis = get_redis()
    await reset_system_redis_state(redis)
    await produce_system_event(record=record)
    await wait_for_system_event_ingested(log_id, timeout_s=30.0)
    return {
        "aic": aic,
        "correlation_id": correlation_id,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _audit_e2e() -> dict[str, Any]:
    from app.core.db_session import async_session_factory

    from tests.integration.conftest import make_signed_log_record
    from tests.support.kafka_helper import produce_audit_event, wait_for_record_ingested

    now, start_at, end_at = _now_window()
    private_key, kid, base_aic = _load_demo_audit_signer()
    aic = f"{base_aic}.{uuid.uuid4().hex[:8]}"
    log_id = str(uuid.uuid4())
    raw = make_signed_log_record(
        private_key,
        kid=kid,
        aic=aic,
        log_id=log_id,
        timestamp=now.isoformat(),
    )
    await produce_audit_event(raw)
    async with async_session_factory() as session:
        await wait_for_record_ingested(session, log_id, timeout=30)
    return {
        "aic": aic,
        "log_id": log_id,
        "start_at": start_at,
        "end_at": end_at,
    }


async def _run(action: str) -> dict[str, Any]:
    actions: dict[str, Any] = {
        "heartbeat-direct": _heartbeat_direct,
        "metrics-direct": _metrics_direct,
        "access-direct": _access_direct,
        "message-direct": _message_direct,
        "system-direct": _system_direct,
        "audit-direct": _audit_direct,
        "heartbeat-e2e": _heartbeat_e2e,
        "metrics-e2e": _metrics_e2e,
        "access-e2e": _access_e2e,
        "message-e2e": _message_e2e,
        "system-e2e": _system_e2e,
        "audit-e2e": _audit_e2e,
    }
    if action not in actions:
        raise SystemExit(f"未知动作：{action}")
    return await actions[action]()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: _monitor_fixture_driver.py <action>")
    payload = asyncio.run(_run(sys.argv[1]))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
