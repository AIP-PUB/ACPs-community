"""tests/support/kafka_helper.py — E2E 测试辅助：Kafka 生产与水位轮询。

提供：
- produce_audit_event: 向 amp.audit 主题投递单条事件（使用独立 test producer）
- produce_heartbeat: 向 amp.heartbeat 主题投递单条心跳消息（按 aic 分区）
- wait_for_watermark_advance: 轮询 audit_read_model_watermark 直到水位推进
- consume_delta_events: 从 amp.heartbeat.alive-delta 消费 N 条 AliveDeltaEnvelope
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog
from acps_sdk.amp.heartbeat_sync import AliveDeltaEnvelope
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.constants import (
    ACCESS_KAFKA_TOPIC,
    DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    HEARTBEAT_DELTA_TOPIC,
    HEARTBEAT_KAFKA_TOPIC,
    MESSAGE_KAFKA_TOPIC,
    METRICS_KAFKA_TOPIC,
)

try:
    from tests.support.constants import AUDIT_KAFKA_TOPIC
except ImportError:
    AUDIT_KAFKA_TOPIC = "amp.audit"

logger = structlog.get_logger(__name__)


async def consume_delta_events(
    n: int,
    *,
    timeout_s: float = 10.0,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = HEARTBEAT_DELTA_TOPIC,
    group_id: str = "test-consumer",
) -> list[AliveDeltaEnvelope]:
    """从 amp.heartbeat.alive-delta 主题消费 n 条 AliveDeltaEnvelope。

    使用 auto_offset_reset="earliest" 从头消费；测试前调用方需确保 topic 干净
    或 group_id 唯一（使用 uuid4 前缀）。

    Args:
        n: 期望消费的条目数量。
        timeout_s: 等待超时秒数（默认 10 秒）。
        bootstrap_servers: Kafka bootstrap servers 地址。
        topic: alive-delta 主题名称。
        group_id: Consumer group id（默认 "test-consumer"）。

    Returns:
        已消费的 AliveDeltaEnvelope 列表（长度 == n）。

    Raises:
        TimeoutError: 超时未收到足够条目。
    """
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    results: list[AliveDeltaEnvelope] = []
    deadline = asyncio.get_event_loop().time() + timeout_s
    try:
        while len(results) < n:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"consume_delta_events: 超时 {timeout_s}s，期望 {n} 条，实际收到 {len(results)} 条")
            record = await asyncio.wait_for(consumer.getone(), timeout=min(remaining, 2.0))
            if record.value:
                data = json.loads(record.value.decode("utf-8"))
                results.append(AliveDeltaEnvelope.model_validate(data))
    finally:
        await consumer.stop()
    return results


async def produce_heartbeat(
    aic: str,
    *,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = HEARTBEAT_KAFKA_TOPIC,
    partition_count: int | None = None,
) -> None:
    """向 amp.heartbeat 主题投递一条心跳消息（按 aic murmur2 分区）。

    消息格式：{"logType": "heartbeat", "aic": aic}；observed_at_ms 由 Kafka
    LogAppendTime（broker 时间戳）自动提供，与 HeartbeatWriter 优先级一致。

    Args:
        aic: Agent Identity Code。
        bootstrap_servers: Kafka bootstrap servers 地址。
        topic: 心跳 Kafka 主题名称，默认 amp.heartbeat。
        partition_count: 主题分区数（用于 aic 分区计算）。
            None（默认）时从 settings.heartbeat_input_partition_count 读取，
            确保与 HeartbeatWriter 所用分区数严格一致。
    """
    from app.core.config import settings
    from app.heartbeat.sharding import input_partition_for_aic

    if partition_count is None:
        partition_count = settings.heartbeat_input_partition_count

    partition = input_partition_for_aic(aic, partition_count)
    payload = json.dumps({"logType": "heartbeat", "aic": aic}, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=aic.encode("utf-8"), partition=partition)
    finally:
        await producer.stop()


async def produce_audit_event(
    raw_log: dict[str, Any],
    topic: str = AUDIT_KAFKA_TOPIC,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
) -> None:
    """向 Kafka amp.audit 主题投递一条 audit 事件（独立 test producer）。

    Args:
        raw_log: LogRecord dict，已包含 log_id、timestamp、body 等所有必需字段。
        topic: Kafka 主题名称，默认 amp.audit。
        bootstrap_servers: Kafka bootstrap servers 地址（Redpanda external listener）。

    Raises:
        KafkaError: 连接或发送失败时抛出。
    """
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        payload = json.dumps(raw_log, ensure_ascii=False).encode("utf-8")
        await producer.send_and_wait(topic, value=payload)
        logger.debug(
            "E2E 测试事件已投递",
            topic=topic,
            log_id=raw_log.get("log_id"),
        )
    finally:
        await producer.stop()


async def produce_metrics(
    aic: str,
    log_id: str | None = None,
    *,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = METRICS_KAFKA_TOPIC,
    uptime_seconds: float = 100.0,
    windows: list[dict[str, Any]] | None = None,
) -> None:
    """向 amp.metrics 主题投递一条 metrics 消息（E2E 测试用）。

    消息格式符合 AMP Spec §5.1.2（MetricsBody）。优先依赖 Kafka LogAppendTime；
    同时写入 observed_timestamp 作为 E2E 回退（测试 consumer 未必暴露 timestamp_type=1）。

    Args:
        aic: Agent Identity Code。
        log_id: 去重 ID（None 时自动生成 uuid4）。
        bootstrap_servers: Kafka bootstrap servers 地址。
        topic: metrics Kafka 主题名称。
        uptime_seconds: 写入的 uptime 值。
        windows: 窗口指标列表，默认 None（无窗口）。
    """
    import uuid
    from datetime import UTC, datetime

    from tests.support.factory import make_metrics_log_record

    # 测试 producer 经 aiokafka 消费时未必暴露 timestamp_type=LogAppendTime；
    # 写入 observed_timestamp 作为 §2.3 回退，保证 Writer 可解析稳定 observedAt。
    observed_ts = datetime.now(UTC).isoformat()
    payload_dict = make_metrics_log_record(
        aic=aic,
        log_id=log_id or str(uuid.uuid4()),
        uptime_seconds=uptime_seconds,
        windows=windows,
        observed_timestamp=observed_ts,
    )
    payload = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=aic.encode("utf-8"))
    finally:
        await producer.stop()


async def wait_for_watermark_advance(
    session: AsyncSession,
    after: datetime,
    timeout: int = 30,
    poll_interval: float = 0.5,
) -> datetime:
    """轮询 audit_read_model_watermark，直到全局水位推进到 after 之后（或超时）。

    全局水位 = MIN(partition_watermark) over all partitions（§2.4, C-AUDIT-QUERY-7）。
    每次轮询前 expire session 缓存以获取最新 DB 值。

    Args:
        session: 异步数据库 session。
        after: 期望水位超过的时间点（timezone-aware datetime）。
        timeout: 超时秒数，默认 30 秒。
        poll_interval: 轮询间隔秒数，默认 0.5 秒。

    Returns:
        推进后的全局水位（MIN partition_watermark）。

    Raises:
        TimeoutError: 超时未推进时抛出。
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        session.expire_all()
        min_wm = await session.scalar(
            text("SELECT MIN(partition_watermark) FROM audit_read_model_watermark WHERE stream_name = 'amp.audit'")
        )
        if min_wm is not None and min_wm >= after:
            return min_wm
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"audit_read_model_watermark 未在 {timeout}s 内推进到 {after}（当前全局水位不足）")


async def wait_for_record_ingested(
    session: AsyncSession,
    log_id: str,
    timeout: int = 30,
    poll_interval: float = 0.5,
) -> None:
    """轮询 audit_record_identity，直到指定 log_id 的记录入库（或超时）。

    用于 E2E 测试中确认 Writer 已处理特定消息，不依赖全局 MIN 水位
    （全局水位语义上是"最慢分区的水位"，不适合用作"某条消息已处理"的同步信号）。

    Args:
        session: 异步数据库 session。
        log_id: 待等待的 LogRecord.log_id。
        timeout: 超时秒数，默认 30 秒。
        poll_interval: 轮询间隔秒数，默认 0.5 秒。

    Raises:
        TimeoutError: 超时未发现对应记录时抛出。
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        session.expire_all()
        row = await session.scalar(
            text("SELECT 1 FROM audit_record_identity WHERE log_id = :log_id").bindparams(log_id=log_id)
        )
        if row is not None:
            return
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"log_id={log_id!r} 未在 {timeout}s 内出现在 audit_record_identity")


async def produce_access_event(
    record: dict[str, Any] | None = None,
    *,
    aic: str = "aic-e2e-001",
    log_id: str | None = None,
    trace_id: str = "",
    span_id: str = "",
    parent_span_id: str = "",
    method: str = "GET",
    route: str = "/api/test",
    response_status: int = 200,
    duration_ms: int = 50,
    error_code: str = "",
    caller_aic: str = "",
    callee_aic: str = "",
    caller_service: str = "",
    callee_service: str = "",
    service_name: str = "e2e-svc",
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = ACCESS_KAFKA_TOPIC,
) -> str:
    """向 amp.access 主题投递一条 access 事件，返回 log_id。

    Args:
        record: 完整的 LogRecord dict（优先使用）；若为 None，则用其余参数构造。
        aic: Agent Identity Code。
        log_id: 去重 ID（None 时自动生成 uuid4）。
        trace_id: Trace ID。
        span_id: Span ID。
        parent_span_id: Parent Span ID。
        method: HTTP 方法。
        route: 请求路由。
        response_status: HTTP 响应状态码。
        duration_ms: 请求耗时（毫秒）。
        error_code: 错误码（空串表示无错误）。
        caller_aic: 调用方 AIC。
        service_name: 服务名。
        bootstrap_servers: Kafka bootstrap servers 地址。
        topic: Access Kafka 主题名称。

    Returns:
        已投递消息的 log_id。
    """
    import uuid
    from datetime import UTC, datetime

    from tests.support.factory import make_access_log_record

    if record is None:
        record = make_access_log_record(
            aic=aic,
            log_id=log_id or str(uuid.uuid4()),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            method=method,
            route=route,
            response_status=response_status,
            duration_ms=duration_ms,
            error_code=error_code,
            caller_aic=caller_aic,
            callee_aic=callee_aic,
            caller_service=caller_service,
            callee_service=callee_service,
            service_name=service_name,
            observed_timestamp=datetime.now(UTC).isoformat(),
        )

    lid = record.get("log_id") or log_id or str(uuid.uuid4())
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=aic.encode("utf-8"))
    finally:
        await producer.stop()

    return lid


async def wait_for_access_event_ingested(
    log_id: str,
    *,
    timeout_s: float = 20.0,
    poll_interval_s: float = 1.0,
) -> None:
    """轮询 ClickHouse access_events，直到指定 log_id 出现（或超时）。

    使用场景：E2E 测试中等待 AccessWriter 完成 CH insert 后再查询 API。
    """
    from app.core.clickhouse_client import get_clickhouse_client

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = await get_clickhouse_client()
            result = await client.query(
                "SELECT count() FROM access_events WHERE log_id = {lid:String}",
                parameters={"lid": log_id},
            )
            if result.result_rows and result.result_rows[0][0] > 0:
                return
        except Exception:  # noqa: S110
            pass  # CH 尚未就绪时忽略，继续轮询
        await asyncio.sleep(poll_interval_s)

    raise TimeoutError(f"access event log_id={log_id!r} 未在 {timeout_s}s 内出现在 access_events")


async def produce_message_event(
    record: dict[str, Any] | None = None,
    *,
    aic: str = "svc-e2e-001",
    log_id: str | None = None,
    event_type: str = "send",
    system: str = "kafka",
    destination_name: str = "e2e-topic",
    destination_kind: str = "topic",
    message_id: str | None = None,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = MESSAGE_KAFKA_TOPIC,
) -> str:
    """向 amp.message 主题投递一条 message 事件，返回 log_id。"""
    import uuid
    from datetime import UTC, datetime

    from tests.support.factory import make_message_log_record

    if record is None:
        record = make_message_log_record(
            aic=aic,
            log_id=log_id or str(uuid.uuid4()),
            event_type=event_type,
            system=system,
            destination_name=destination_name,
            destination_kind=destination_kind,
            message_id=message_id or str(uuid.uuid4()),
            observed_timestamp=datetime.now(UTC).isoformat(),
        )

    lid = record.get("log_id") or log_id or str(uuid.uuid4())
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=aic.encode("utf-8"))
    finally:
        await producer.stop()

    return lid


async def wait_for_message_event_ingested(
    log_id: str,
    *,
    timeout_s: float = 25.0,
    poll_interval_s: float = 1.0,
) -> None:
    """轮询 ClickHouse message_events，直到指定 log_id 出现（或超时）。"""
    from app.core.clickhouse_client import get_clickhouse_client

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = await get_clickhouse_client()
            result = await client.query(
                "SELECT count() FROM message_events WHERE log_id = {lid:String}",
                parameters={"lid": log_id},
            )
            if result.result_rows and result.result_rows[0][0] > 0:
                return
        except Exception:  # noqa: S110
            pass
        await asyncio.sleep(poll_interval_s)

    raise TimeoutError(f"message event log_id={log_id!r} 未在 {timeout_s}s 内出现在 message_events")


async def produce_system_event(
    record: dict[str, Any] | None = None,
    *,
    aic: str = "aic-e2e-system-001",
    log_id: str | None = None,
    message: str = "test system event",
    severity_number: int = 9,
    bootstrap_servers: str = DEFAULT_KAFKA_BOOTSTRAP_SERVERS,
    topic: str = "amp.system",
) -> str:
    """向 amp.system 主题投递一条 system 事件，返回 log_id。"""
    import uuid
    from datetime import UTC, datetime

    from tests.support.factory import make_system_log_record

    if record is None:
        record = make_system_log_record(
            aic=aic,
            log_id=log_id or str(uuid.uuid4()),
            message=message,
            severity_number=severity_number,
            observed_timestamp=datetime.now(UTC).isoformat(),
        )

    lid = record.get("log_id") or log_id or str(uuid.uuid4())
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="PLAINTEXT",
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=aic.encode("utf-8"))
        logger.debug("E2E system 事件已投递", topic=topic, log_id=lid)
    finally:
        await producer.stop()

    return lid


async def wait_for_system_event_ingested(
    log_id: str,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 1.0,
) -> None:
    """轮询 OpenSearch 直到指定 log_id 的 system 事件出现（或超时）。

    直接查 OpenSearch（绕过 API），用于 E2E 测试中等待 SystemWriter 完成 Bulk Index。
    """
    from app.core.opensearch_client import get_opensearch_client
    from app.system import indices

    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            client = await get_opensearch_client()
            resp = await client.search(
                index=indices.INDEX_PATTERN,
                body={"query": {"term": {"log_id": {"value": log_id}}}, "size": 1},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
            total = resp.get("hits", {}).get("total", {}).get("value", 0)
            if total > 0:
                return
        except Exception:  # noqa: S110
            pass
        await asyncio.sleep(poll_interval_s)

    raise TimeoutError(f"system event log_id={log_id!r} 未在 {timeout_s}s 内出现在 OpenSearch")
