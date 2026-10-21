"""生成 AMP 跨语言对照样例：test/fixtures/python/amp/*.json

运行：uv run --project ../../acps-sdk --python 3.12 python -I scripts/fixtures/gen_amp.py

全部输出确定性：固定 log_id / timestamp、固定随机种子。样例中不含任何私钥：
Ed25519 使用 RFC 8032 §7.1 的公开测试向量；RSA / EC 私钥是由公开种子确定性派生的一次性测试密钥，
只存在于生成进程的内存中、绝不可在别处复用，样例只写公钥、证书与签名结果（ECDSA 使用 RFC 6979 确定性签名）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import inspect
import typing
import base64
import hashlib
import json
import math
import random
import struct
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))

import jcs  # noqa: E402
from _common import write_fixture  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import acps_sdk.amp as amp_package  # noqa: E402
from acps_sdk.amp import _message_tap as tap  # noqa: E402
from acps_sdk.amp import metrics_demo  # noqa: E402
from acps_sdk.amp.access_emitter import AccessEmitter  # noqa: E402
from acps_sdk.amp.alive_sync import (  # noqa: E402
    AliveSyncEngine,
    AliveSyncError,
    next_lookback_seconds,
    parse_meta_line,
    parse_snapshot_row,
    seek_timestamp_ms,
    snapshot_row_to_record,
)
from acps_sdk.amp.alive_sync.models import AliveView  # noqa: E402
from acps_sdk.amp.alive_sync.store import AliveRecord, ShardCheckpoint  # noqa: E402
from acps_sdk.amp.emitter import AuditEmitter  # noqa: E402
from acps_sdk.amp.heartbeat_emitter import HeartbeatEmitter  # noqa: E402
from acps_sdk.amp.heartbeat_sync import (  # noqa: E402
    AliveDeltaEnvelope,
    AliveSnapshotMeta,
    HeartbeatSyncInfo,
    aic_from_object_id,
    alive_object_id,
    seq_from_str,
    seq_to_str,
    shard_id,
    shard_index_from_id,
)
from acps_sdk.amp.message_emitter import MessageEmitter  # noqa: E402
from acps_sdk.amp.metrics_catalog import (  # noqa: E402
    KNOWN_QUANTILES,
    KNOWN_WINDOWS,
    PUBLIC_METRIC_CATALOG,
    MetricFamily,
    lookup_public_metric,
)
from acps_sdk.amp.metrics_emitter import MetricsEmitter  # noqa: E402
from acps_sdk.amp.models import (  # noqa: E402
    AccessLogRecord,
    AuditLogRecord,
    HeartbeatLogRecord,
    LogRecord,
    MessageLogRecord,
    MetricsLogRecord,
    compute_log_id_fallback,
)
from acps_sdk.amp.signer import CertificateAuditSigner, Ed25519AuditSigner  # noqa: E402
from acps_sdk.amp.system_emitter import SystemEmitter  # noqa: E402
from acps_sdk.amp.trace_context import TraceContext, format_traceparent, parse_traceparent  # noqa: E402

PKG = "amp"
LOG_ID = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
TS_OFFSET = "2026-10-19T08:01:12.160000+00:00"
TS_ZULU = "2026-10-19T08:01:12.160000Z"
TRICKY_TEXT = 'quote" back\\ nl\n tab\t ctl\x01\x1f del\x7f ls  é 中文 😀 /slash'


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _write_line(emitter: Any, record: Any) -> str:
    """调用 emitter 的真实 _write_line，返回写出的那一行（不含换行）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.jsonl"
        emitter._log_file = path
        emitter._write_line(record)
        return path.read_text(encoding="utf-8").removesuffix("\n")


# JSON 无法表达 NaN / ±Infinity：样例输入里用这些占位字符串，生成与测试两侧各自还原成浮点数。
NONFINITE_SENTINELS = {"__NaN__": float("nan"), "__Infinity__": float("inf"), "__-Infinity__": float("-inf")}


def _decode_nonfinite(value: Any) -> Any:
    if isinstance(value, str) and value in NONFINITE_SENTINELS:
        return NONFINITE_SENTINELS[value]
    if isinstance(value, list):
        return [_decode_nonfinite(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_nonfinite(item) for key, item in value.items()}
    return value


def _error(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "value": fn()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "type": type(exc).__name__, "message": str(exc)}


# ─── 1. 各日志类型的 NDJSON 行 ──────────────────────────────────────────────────


def gen_records() -> None:
    placeholder = Path("unused.jsonl")
    tail = {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "parent_span_id": "",
        "correlation_id": "会话-1",
        "severity_text": "INFO",
        "severity_number": 9,
    }
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, record_input: dict[str, Any], line: str, resource: Any = None) -> None:
        case: dict[str, Any] = {"name": name, "kind": kind, "input": record_input, "line": line}
        if resource is not None:
            case["resource"] = resource
        cases.append(case)

    # heartbeat：float 字段的 repr（0 → 0.0、12 → 12.0、1e16 → 1e+16、1e-7 → 1e-07 …）
    for idx, uptime in enumerate([0, 12, 0.1, 1e16, 1e-7, 123456789.123, 1e22, -0.0, 1e15, 5e-324]):
        rec_in = {
            "log_id": LOG_ID,
            "timestamp": TS_OFFSET,
            "aic": "1.2.156.3088.1.A.B.C.0.XYZW",
            "body": {"uptimeSeconds": uptime},
        }
        if idx == 1:
            rec_in.update(tail)
        record = HeartbeatLogRecord.model_validate(rec_in)
        add(f"heartbeat_{idx}", "heartbeat", rec_in, _write_line(HeartbeatEmitter(placeholder, "x"), record))
    rec_in = {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": TRICKY_TEXT, "body": {}}
    add(
        "heartbeat_no_uptime_tricky_text",
        "heartbeat",
        rec_in,
        _write_line(HeartbeatEmitter(placeholder, "x"), HeartbeatLogRecord.model_validate(rec_in)),
    )

    # metrics：嵌套 float / int、resource（Any：内部 null 保留）
    metrics_body = {
        "uptimeSeconds": 3600,
        "loadMetrics": {
            "activeTasks": 3,
            "queuedTasks": 0,
            "maxActiveTasks": 10,
            "cpuUsage": 50,
            "memoryUsage": 37.5,
            "diskUsage": 0.1,
            "networkInUsage": 1e-5,
            "networkOutUsage": 99.99,
        },
        "windowMetrics": [
            {
                "window": "PT1M",
                "successRate": 100,
                "requestTotal": 600,
                "requestPerSecond": 10,
                "avgThroughputMBps": 1.25,
                "peakThroughputMBps": 2,
                "avgLatencyMs": 12.345,
                "p99LatencyMs": 99,
                "p95LatencyMs": 95.5,
                "p90LatencyMs": 90,
                "p80LatencyMs": 80.25,
                "p75LatencyMs": 75,
                "p50LatencyMs": 50.125,
            },
            {"window": "PT5M", "successRate": 99.9},
        ],
    }
    resource = {
        "service.name": "svc-测试",
        "deployment.environment.name": "dev",
        "nested": {"k": [1, 2.5, None, True], "z": None},
    }
    for name, res in [("metrics_full", resource), ("metrics_empty_resource", {}), ("metrics_no_resource", None)]:
        rec_in = {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": "aic-metrics", "body": metrics_body}
        if res is not None:
            rec_in["resource"] = res
        if name == "metrics_full":
            rec_in.update(tail)
        emitter = MetricsEmitter(placeholder, "x", sampler=MagicMock(), resource=res)
        add(name, "metrics", rec_in, _write_line(emitter, MetricsLogRecord.model_validate(rec_in)))

    # access：durationMs int → 12.0；resource 在 model_dump 之后追加、空对象不写
    access_body = {
        "durationMs": 12,
        "request": {
            "method": "POST",
            "url": "https://partner.example/rpc?q=中文",
            "route": "/rpc",
            "headers": {
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "content-type": "application/json",
            },
            "bodySizeBytes": 256,
        },
        "response": {"statusCode": 200, "bodySizeBytes": 1024},
        "caller": {"aic": "caller-aic", "serviceName": "demo-leader", "ip": "10.0.0.1"},
        "callee": {"aic": "callee-aic", "serviceName": "demo-partner"},
        "error": {
            "code": "REJECTED",
            "message": TRICKY_TEXT,
            "data": {"a": None, "b": [1, 2.5, "x"], "c": {"d": False}},
            "stackTrace": "line1\nline2",
        },
    }
    for name, res in [
        ("access_full", {"service.name": "demo-leader", "n": 1.5}),
        ("access_empty_resource", {}),
        ("access_no_resource", None),
    ]:
        rec_in = {"log_id": LOG_ID, "timestamp": TS_ZULU, "aic": "aic-leader", "body": access_body, **tail}
        emitter = AccessEmitter(placeholder, "x", resource=res)
        add(name, "access", rec_in, _write_line(emitter, AccessLogRecord.model_validate(rec_in)), resource=res)
    rec_in = {
        "log_id": LOG_ID,
        "timestamp": TS_ZULU,
        "aic": "aic-leader",
        "body": {"durationMs": 0.5, "error": {"code": 404}},
    }
    add(
        "access_minimal",
        "access",
        rec_in,
        _write_line(AccessEmitter(placeholder, "x"), AccessLogRecord.model_validate(rec_in)),
    )

    # message
    message_body = {
        "eventType": "nack",
        "operationName": "basic.nack",
        "system": "rabbitmq",
        "destination": {"name": "group.abc", "kind": "exchange", "virtualHost": "acps"},
        "consumerGroupName": "cg",
        "subscriptionName": "partner.queue.1",
        "routing": {"key": "rk", "partition": "0", "offset": 42},
        "messageId": "cmd-001",
        "payloadSizeBytes": 128,
        "deliveryAttempt": 2,
        "settlement": {"latencyMs": 5, "reason": "handler failed"},
        "error": {"code": 500, "message": "boom"},
        "attributes": {"x": "y", "n": 1, "f": 0.25, "nested": {"list": [None, "中文"]}},
    }
    for name, res in [("message_full", {"service.name": "demo-partner"}), ("message_empty_resource", {})]:
        rec_in = {"log_id": LOG_ID, "timestamp": TS_ZULU, "aic": "aic-partner", "body": message_body, **tail}
        emitter = MessageEmitter(placeholder, "x", resource=res)
        add(name, "message", rec_in, _write_line(emitter, MessageLogRecord.model_validate(rec_in)), resource=res)

    # audit（未签名；签名样例见 signatures.json）
    audit_body = {
        "actor": {
            "id": "user-001",
            "type": "user",
            "name": "张三",
            "role": "admin",
            "ip": "10.1.2.3",
            "userAgent": "curl/8.0",
        },
        "action": {"name": "user.delete", "type": "user_management", "method": "DELETE /api/users/bob"},
        "target": {
            "type": "user",
            "id": "bob",
            "name": "Bob",
            "before": {"z": 1, "a": [1, 2.5, None], "u": "é"},
            "after": None,
        },
        "result": {"status": "failure", "reason": TRICKY_TEXT, "errorCode": "RESOURCE_NOT_FOUND"},
    }
    rec_in = {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": "aic-audit", "body": audit_body, **tail}
    add(
        "audit_unsigned",
        "audit",
        rec_in,
        _write_line(AuditEmitter(placeholder, "x"), AuditLogRecord.model_validate(rec_in)),
    )

    # system（LogRecord：resource 传入模型，空对象照样写出；body 为 None 时省略）
    system_cases = [
        (
            "system_full",
            {"message": "hi", "category": "llm", "tokens": 12, "ratio": 0.75, "meta": {"k": None}},
            {"service.name": "svc"},
        ),
        ("system_empty_resource", {"message": TRICKY_TEXT}, {}),
        ("system_no_body", None, None),
    ]
    for name, body, res in system_cases:
        rec_in: dict[str, Any] = {
            "schema_version": "1.0",
            "log_type": "system",
            "log_id": LOG_ID,
            "timestamp": TS_ZULU,
            "aic": "aic-system",
            "severity_number": 17,
            "severity_text": "ERROR",
            "trace_id": "t" * 32,
            "correlation_id": "corr-1",
        }
        if body is not None:
            rec_in["body"] = body
        if res is not None:
            rec_in["resource"] = res
        add(name, "system", rec_in, _write_line(SystemEmitter(placeholder, "x"), LogRecord.model_validate(rec_in)))

    # 大整数：free-form 字段里 < 1e21 的整数写全位数（Python int）；float 字段里按 float repr
    big_ints = {"p53": 2**53, "p60": 2**60, "e20": 10**20, "ns": 1760860872160000000, "neg": -(2**60), "f": 1e21}
    rec_in = {
        **{k: v for k, v in tail.items()},
        "schema_version": "1.0",
        "log_type": "system",
        "log_id": LOG_ID,
        "timestamp": TS_ZULU,
        "aic": "aic-system",
        "body": big_ints,
    }
    add(
        "system_big_ints",
        "system",
        rec_in,
        _write_line(SystemEmitter(placeholder, "x"), LogRecord.model_validate(rec_in)),
    )
    rec_in = {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": "aic", "body": {"uptimeSeconds": 2**60}}
    add(
        "heartbeat_big_int_uptime",
        "heartbeat",
        rec_in,
        _write_line(HeartbeatEmitter(placeholder, "x"), HeartbeatLogRecord.model_validate(rec_in)),
    )

    # NaN / ±Infinity：float 字段写 NaN / Infinity（json.dumps allow_nan），Any 字段经 pydantic JSON 模式变 null，
    # Access/Message 在 model_dump 之后追加的原始 resource 照样写 NaN
    nonfinite: list[tuple[str, str, dict[str, Any], Any]] = [
        (
            "nonfinite_heartbeat",
            "heartbeat",
            {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": "a", "body": {"uptimeSeconds": "__NaN__"}},
            None,
        ),
        (
            "nonfinite_metrics",
            "metrics",
            {
                "log_id": LOG_ID,
                "timestamp": TS_OFFSET,
                "aic": "a",
                "body": {
                    "uptimeSeconds": "__Infinity__",
                    "loadMetrics": {
                        "activeTasks": 0,
                        "queuedTasks": 0,
                        "cpuUsage": "__NaN__",
                        "memoryUsage": "__-Infinity__",
                    },
                    "windowMetrics": [{"window": "PT1M", "successRate": "__NaN__", "p50LatencyMs": "__Infinity__"}],
                },
                "resource": {"r": "__NaN__", "ok": 1},
            },
            None,
        ),
        (
            "nonfinite_access",
            "access",
            {
                "log_id": LOG_ID,
                "timestamp": TS_ZULU,
                "aic": "a",
                "body": {
                    "durationMs": "__-Infinity__",
                    "error": {"code": 1, "data": {"x": "__NaN__", "y": ["__Infinity__"]}},
                },
            },
            {"r": "__NaN__", "s": ["__-Infinity__"]},
        ),
        (
            "nonfinite_message",
            "message",
            {
                "log_id": LOG_ID,
                "timestamp": TS_ZULU,
                "aic": "a",
                "body": {
                    "eventType": "ack",
                    "system": "rabbitmq",
                    "destination": {"name": "x"},
                    "settlement": {"latencyMs": "__NaN__"},
                    "attributes": {"a": "__Infinity__"},
                },
            },
            None,
        ),
        (
            "nonfinite_system",
            "system",
            {
                "schema_version": "1.0",
                "log_type": "system",
                "log_id": LOG_ID,
                "timestamp": TS_ZULU,
                "aic": "a",
                "body": {"x": "__NaN__", "nested": [{"y": "__-Infinity__"}]},
            },
            None,
        ),
        (
            "nonfinite_audit",
            "audit",
            {
                "log_id": LOG_ID,
                "timestamp": TS_OFFSET,
                "aic": "a",
                "body": {
                    "actor": {"id": "u", "type": "user"},
                    "action": {"name": "n", "type": "t"},
                    "target": {"type": "t", "id": "i", "before": {"v": "__NaN__"}},
                    "result": {"status": "success"},
                },
            },
            None,
        ),
    ]
    models = {
        "heartbeat": HeartbeatLogRecord,
        "metrics": MetricsLogRecord,
        "access": AccessLogRecord,
        "message": MessageLogRecord,
        "system": LogRecord,
        "audit": AuditLogRecord,
    }
    for name, kind, rec_in, res in nonfinite:
        record = models[kind].model_validate(_decode_nonfinite(rec_in))
        if kind == "access":
            emitter: Any = AccessEmitter(placeholder, "x", resource=_decode_nonfinite(res))
        elif kind == "message":
            emitter = MessageEmitter(placeholder, "x", resource=_decode_nonfinite(res))
        else:
            emitter = {"heartbeat": HeartbeatEmitter, "system": SystemEmitter, "audit": AuditEmitter}.get(kind, None)
            emitter = (
                MetricsEmitter(placeholder, "x", sampler=MagicMock())
                if kind == "metrics"
                else emitter(placeholder, "x")
            )
        add(name, kind, rec_in, _write_line(emitter, record), resource=res)

    write_fixture(PKG, "records", {"cases": cases, "nonfinite_sentinels": list(NONFINITE_SENTINELS)})


# ─── 2. JCS 与 log_id 兜底 ─────────────────────────────────────────────────────


def gen_jcs() -> None:
    numbers = [
        0,
        -0.0,
        1,
        -1,
        0.1,
        0.2 + 0.1,
        1e21,
        1e20,
        1e-7,
        1e-6,
        1.5e-7,
        1e-5,
        1.2345e-5,
        1e16,
        1e15,
        123456789012345680000,
        1.7976931348623157e308,
        5e-324,
        2**53,
        2**53 + 1,
        2**64,
        9007199254740993,
        4.35,
        0.002,
        -1e-7,
        100,
        1e100,
        1e-100,
        333333333.3333333,
        12.0,
        -12.5,
        0.000001,
        999999999999999999999,
    ]
    cases: list[dict[str, Any]] = []
    for value in numbers:
        cases.append({"input": value, "canonical": jcs.canonicalize(value).decode("utf-8")})
    tricky_objects = [
        {"b": 1, "a": 2, "c": {"z": [3, {"y": 1, "x": 2}], "a": None}},
        # RFC 8785 §3.2.3 排序样例：按 UTF-16 码元而非码点
        {
            "€": "Euro Sign",
            "\r": "Carriage Return",
            "דּ": "Hebrew Letter Dalet With Dagesh",
            "1": "One",
            "\U0001f600": "Emoji: Grinning Face",
            "\u0080": "Control",
            "ö": "Latin Small Letter O With Diaeresis",
        },
        {"keys": {"": 1, "A": 2, "a": 3, "aa": 4, "é": 5, "￿": 6, "\U0001f600": 7, "10": 8, "9": 9}},
        {"text": TRICKY_TEXT, "controls": "".join(chr(i) for i in range(0x20)), "unicode": "  ﻿"},
        [None, True, False, [], {}, "", 0, [[]], {"a": {}}],
        {"numbers": [1e21, 0.1, -0.0, 1e-7, 2**53 + 1]},
    ]
    for obj in tricky_objects:
        cases.append({"input": obj, "canonical": jcs.canonicalize(obj).decode("utf-8")})

    fallback_inputs = [
        {
            "schema_version": "1.0",
            "timestamp": TS_ZULU,
            "aic": "aic-1",
            "log_type": "access",
            "body": {"durationMs": 12.5},
        },
        {
            "schema_version": "1.0",
            "timestamp": TS_OFFSET,
            "aic": "aic-2",
            "log_type": "audit",
            "log_id": "should-be-ignored",
            "observed_timestamp": "2026-10-19T08:01:13Z",
            "trace_id": "abc",
            "span_id": None,
            "parent_span_id": "",
            "correlation_id": "会话",
            "integrity": {"alg": "EdDSA", "kid": "k", "sig": "s"},
            "body": {"actor": {"id": "u", "type": "user"}, "z": [1, None, {"b": 2, "a": 1}], "text": TRICKY_TEXT},
        },
        {"timestamp": "t", "aic": "a", "log_type": "system", "body": None, "resource": {"x": 1}},
    ]
    fallback = [{"raw": raw, "log_id": compute_log_id_fallback(raw)} for raw in fallback_inputs]
    write_fixture(PKG, "jcs", {"cases": cases, "log_id_fallback": fallback})


# ─── 3. float repr / round / format ────────────────────────────────────────────


def gen_floats() -> None:
    rng = random.Random(20261019)
    values: list[float] = [
        0.0,
        -0.0,
        0.1,
        0.5,
        1.5,
        2.5,
        -2.5,
        0.125,
        0.375,
        2.675,
        1.005,
        0.0078125,
        1e-4,
        1e-5,
        9999999999999998.0,
        1e16,
        1e15,
        123.456,
        1e22,
        5e-324,
        1.7976931348623157e308,
        2.2250738585072014e-308,
        0.30000000000000004,
        12.0,
        37.5,
        99.995,
        0.045,
        1234.5,
        1234.55,
        85.0,
        1e-7,
        0.000123456789,
    ]
    while len(values) < 250:
        bits = rng.getrandbits(64)
        value = struct.unpack(">d", struct.pack(">Q", bits))[0]
        if math.isfinite(value):
            values.append(value)
    for _ in range(250):
        values.append(round(rng.uniform(-1e6, 1e6), rng.randint(0, 6)))
        values.append(rng.uniform(0, 100))
    cases = []
    for value in values:
        case: dict[str, Any] = {
            "bits": struct.pack(">d", value).hex(),
            "repr": repr(value),
            "json": json.dumps(value),
            "format6": f"{value:.6f}" if abs(value) < 1e30 else None,
        }
        if abs(value) < 1e30:
            case["round"] = [struct.pack(">d", float(round(value, n))).hex() for n in range(0, 4)]
            case["round_int"] = str(round(value))
        cases.append(case)
    write_fixture(PKG, "floats", {"cases": cases})


# ─── 4. 签名 ──────────────────────────────────────────────────────────────────


def _is_probable_prime(n: int, rng: random.Random) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(40):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _deterministic_rsa_key(seed: str) -> rsa.RSAPrivateKey:
    rng = random.Random(seed)
    e = 65537

    def prime() -> int:
        while True:
            candidate = rng.getrandbits(1024) | (3 << 1022) | 1
            if math.gcd(e, candidate - 1) == 1 and _is_probable_prime(candidate, rng):
                return candidate

    p, q = prime(), prime()
    if p < q:
        p, q = q, p
    n = p * q
    d = pow(e, -1, (p - 1) * (q - 1))
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=rsa.RSAPublicNumbers(e, n),
    )
    return numbers.private_key()


def _pem_private(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _pem_public(key: Any) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _cert(subject_key: Any, issuer_key: Any, serial: int, cn: str) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(subject_key.public_key())
        .serial_number(serial)
        .not_valid_before(datetime(2026, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2036, 1, 1, tzinfo=timezone.utc))
    )
    algorithm = None if isinstance(issuer_key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(issuer_key, algorithm).public_bytes(serialization.Encoding.PEM).decode()


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 0x80:
        return bytes([tag, length]) + content
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(encoded)]) + encoded + content


def _rsa_pss_pem(key: rsa.RSAPrivateKey) -> str:
    """把 RSA 私钥包装成 OID 为 id-RSASSA-PSS 的 PKCS#8（cryptography 加载时按普通 RSAPrivateKey 处理）。

    只在生成过程中使用，不写入样例。PEM 头尾沿用 cryptography 自己输出的 PKCS#8 PEM。
    """
    pkcs1 = key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    rsassa_pss_oid = bytes.fromhex("06092a864886f70d01010a")  # 1.2.840.113549.1.1.10
    pkcs8 = _der(0x30, bytes.fromhex("020100") + _der(0x30, rsassa_pss_oid) + _der(0x04, pkcs1))
    armor = _pem_private(key).splitlines()
    body = textwrap.wrap(base64.b64encode(pkcs8).decode(), 64)
    return "\n".join([armor[0], *body, armor[-1]]) + "\n"


# RFC 8032 §7.1 TEST 1 的 Ed25519 密钥（公开测试向量，非机密）。Ed25519 签名是确定性的，
# 因此 Node 测试用同一个种子即可逐字节对照签名行。
RFC8032_TEST1_SECRET_KEY_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC8032_TEST1_PUBLIC_KEY_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def gen_signatures() -> None:
    """签名对照样例。

    - Ed25519：私钥取 RFC 8032 公开测试向量，签名行逐字节对照；
    - RSA / RSA-PSS / ECDSA：私钥只在本进程内存中，样例只写公钥、证书与 Python 签出的行，
      Node 侧验签，并用测试时现场生成的密钥比对「除签名值以外」的内容。

    警告：下面的 RSA / EC 私钥由公开的固定种子确定性派生（这样每次生成的样例完全相同），
    任何人都能从本脚本重新算出它们——只是一次性测试密钥，绝不可在测试以外的任何场合复用。
    """
    ed_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(RFC8032_TEST1_SECRET_KEY_HEX))
    assert (
        ed_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        == RFC8032_TEST1_PUBLIC_KEY_HEX
    )
    # 一次性测试密钥（种子公开、可复算），切勿在任何真实环境中使用
    rsa_key = _deterministic_rsa_key("acps-sdk-node amp fixture rsa (in-memory only)")
    ec_scalar = int.from_bytes(hashlib.sha256(b"acps-sdk-node amp fixture ec (in-memory only)").digest(), "big")
    ec_key = ec.derive_private_key(ec_scalar % (2**255) + 1, ec.SECP256R1())

    ed_cert = _cert(ed_key, ed_key, 0x3F8A91B2C, "acps-amp-fixture-ed25519")  # 奇数位十六进制：Node 会补前导 0
    rsa_cert = _cert(rsa_key, rsa_key, 0x8FEDCBA987654321, "acps-amp-fixture-rsa")  # 最高位为 1：DER 带前导 00
    small_cert = _cert(ed_key, ed_key, 1, "acps-amp-fixture-serial-1")
    ec_cert = _cert(ec_key, ed_key, 0x0ABC, "acps-amp-fixture-ec")

    record_input = {
        "log_id": LOG_ID,
        "timestamp": TS_OFFSET,
        "aic": "1.2.156.3088.1.A.B.C.0.XYZW",
        "body": {
            "actor": {"id": "user-001", "type": "user", "name": "张三", "userAgent": "curl/8.0"},
            "action": {"name": "login", "type": "auth"},
            "target": {
                "type": "session",
                "id": "sess-1",
                "before": {"z": 1, "a": [1, 2.5, None, {"é": "😀"}]},
                "after": {"n": 1e21},
            },
            "result": {"status": "success"},
        },
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "correlation_id": "corr-1",
    }
    record = AuditLogRecord.model_validate(record_input)
    record_dict = _dump(record)
    canonical = jcs.canonicalize(record_dict)
    placeholder = Path("unused.jsonl")

    def signed_line(signer: Any) -> str:
        return _write_line(AuditEmitter(placeholder, "x", signer=signer), record)

    lines = {
        "ed25519": signed_line(Ed25519AuditSigner(_pem_private(ed_key), kid="audit-key-test")),
        "cert_ed25519": signed_line(CertificateAuditSigner(_pem_private(ed_key), ed_cert)),
        "cert_rsa": signed_line(CertificateAuditSigner(_pem_private(rsa_key), rsa_cert)),
        # RSA-PSS 受限 PKCS#8：Python 忽略 PSS 限制、照常用 PKCS#1 v1.5 签名
        "cert_rsa_pss": signed_line(CertificateAuditSigner(_rsa_pss_pem(rsa_key), rsa_cert)),
    }
    ecdsa_sig = ec_key.sign(canonical, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))

    write_fixture(
        PKG,
        "signatures",
        {
            "rfc8032_test1_ed25519": {
                "note": "RFC 8032 §7.1 TEST 1 public test vector (not a secret)",
                "secret_key_hex": RFC8032_TEST1_SECRET_KEY_HEX,
                "public_key_hex": RFC8032_TEST1_PUBLIC_KEY_HEX,
            },
            "public_keys": {
                "ed25519": _pem_public(ed_key),
                "rsa": _pem_public(rsa_key),
                "ec": _pem_public(ec_key),
            },
            "certs": {"ed25519": ed_cert, "rsa": rsa_cert, "serial_1": small_cert, "ec": ec_cert},
            # Node 测试现场签发证书时复用这些序列号（十六进制），以便 kid 与 Python 一致
            "serials_hex": {"ed25519": "3f8a91b2c", "rsa": "8fedcba987654321", "serial_1": "1", "ec": "abc"},
            "kids": {
                "ed25519": CertificateAuditSigner(_pem_private(ed_key), ed_cert).kid,
                "rsa": CertificateAuditSigner(_pem_private(rsa_key), rsa_cert).kid,
                "serial_1": CertificateAuditSigner(_pem_private(ed_key), small_cert).kid,
            },
            "record_input": record_input,
            "record_dict": record_dict,
            "canonical": canonical.decode("utf-8"),
            "lines": lines,
            "ecdsa_der_sig_b64url": base64.urlsafe_b64encode(ecdsa_sig).rstrip(b"=").decode(),
            "errors": {
                "ed25519_signer_with_rsa": _error(lambda: Ed25519AuditSigner(_pem_private(rsa_key), "k")),
                "ed25519_signer_with_ec": _error(lambda: Ed25519AuditSigner(_pem_private(ec_key), "k")),
                "cert_signer_with_ec": _error(lambda: CertificateAuditSigner(_pem_private(ec_key), ec_cert)),
            },
        },
    )


# ─── 5. traceparent ───────────────────────────────────────────────────────────


def gen_trace_context() -> None:
    fmt = [
        {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7", "sampled": True},
        {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7", "sampled": False},
        {"trace_id": "A" * 32, "span_id": "B" * 16, "sampled": True},
    ]
    format_cases = [{**c, "header": format_traceparent(TraceContext(**c))} for c in fmt]

    def parsed(header: str | None) -> Any:
        ctx = parse_traceparent(header)
        return None if ctx is None else {"trace_id": ctx.trace_id, "span_id": ctx.span_id, "sampled": ctx.sampled}

    headers = [
        None,
        "",
        "bad-header",
        "00-abc-def-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-ff",
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01",
        "  00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\n",
        "\x1c00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\x85",
        "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
        "00-4bf92f3577b34da6a3ce929d0e0e473g-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
        "00--00f067aa0ba902b7-01",
    ]
    # Python 的 int(x, 16) 还接受这些非法写法；Node 版有意拒绝（见 parseTraceparent 注释）
    lenient = [
        "00-0x4bf92f3577b34da6a3ce929d0e0e47-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6_3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-+bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00- bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    ]
    write_fixture(
        PKG,
        "trace_context",
        {
            "format": format_cases,
            "parse": [{"header": h, "parsed": parsed(h)} for h in headers],
            "python_lenient_parse": [{"header": h, "python_parsed": parsed(h)} for h in lenient],
        },
    )


# ─── 6. heartbeat_sync ────────────────────────────────────────────────────────


def gen_heartbeat_sync() -> None:
    shard_pairs = [
        (0, 1),
        (7, 10),
        (0, 1000),
        (999, 1000),
        (0, 1001),
        (1000, 1001),
        (0, 10000),
        (12345, 100000),
        (5, 0),
        (-5, 10),
        (-5, 1001),
        (3, -100),
    ]
    shard_index_inputs = [
        "hb-000",
        "hb-007",
        "hb-1000",
        "hb-0000123",
        "007",
        "hb-abc",
        "hb-",
        "hb--1",
        "HB-001",
        "hb-1.5",
        "hb- 1",
    ]
    seq_inputs = [
        "0",
        "42",
        "1000",
        "999",
        "007",
        "-0",
        "-00",
        "-1",
        "-007",
        "--5",
        "",
        "abc",
        "1.5",
        " 1",
        "1 ",
        "+1",
        "-",
        "5-",
        "1_000",
        str(10**15),
        "9007199254740991",
    ]
    object_ids = [
        "urn:amp:alive:foo",
        "urn:amp:alive:",
        "urn:other:foo",
        "URN:AMP:ALIVE:foo",
        "urn:amp:alive:1.2.156.3088.1.A.B.C.D.E",
    ]

    env_inputs = [
        {
            "shard": "hb-000",
            "seq": "1",
            "type": "amp-alive-delta",
            "id": "urn:amp:alive:agent-001",
            "version": "1",
            "op": "upsert",
            "kind": "enter_alive",
            "payload": {
                "aic": "agent-001",
                "lastSeenAt": "2026-01-01T00:00:00Z",
                "sourceTimestamp": "2026-01-01T00:00:00.5Z",
            },
        },
        {
            "shard": "hb-000",
            "seq": "2",
            "type": "amp-alive-delta",
            "id": "urn:amp:alive:agent-001",
            "version": "2",
            "op": "delete",
            "kind": "leave_alive",
            "payload": None,
        },
        {
            "shard": "hb-001",
            "seq": "3",
            "type": "amp-alive-delta",
            "id": "urn:amp:alive:agent-002",
            "version": "3",
            "op": "upsert",
            "kind": "refresh_alive",
            "payload": {"aic": "agent-002", "lastSeenAt": "2026-01-01T00:00:00Z"},
            "unknownField": 1,
        },
    ]
    info_input = {
        "type": "amp-alive-delta",
        "schemaVersion": "1",
        "snapshotContentType": "application/x-ndjson",
        "kafkaTopic": "amp.heartbeat.alive-delta",
        "shardCount": 2,
        "refreshEmitIntervalSeconds": 30,
        "deltaRetentionHours": 168,
        "currentPublishedSeqByShard": {"hb-000": "10", "hb-001": "20"},
    }
    meta_input = {
        "recordType": "snapshot-meta",
        "type": "amp-alive-delta",
        "cutoverSeqByShard": {"hb-000": "5"},
        "generatedAt": "2026-01-01T00:00:00Z",
    }

    write_fixture(
        PKG,
        "heartbeat_sync",
        {
            "alive_object_id": [
                {"aic": a, "object_id": alive_object_id(a)} for a in ["foo", "", "1.2.156.3088.1.A.B.C.D.E", "中文"]
            ],
            "aic_from_object_id": [
                {"input": o, "result": _error(lambda o=o: aic_from_object_id(o))} for o in object_ids
            ],
            "shard_id": [{"index": i, "count": c, "result": shard_id(i, c)} for i, c in shard_pairs],
            "shard_index_from_id": [
                {"input": s, "result": _error(lambda s=s: shard_index_from_id(s))} for s in shard_index_inputs
            ],
            "seq_from_str": [{"input": s, "result": _error(lambda s=s: seq_from_str(s))} for s in seq_inputs],
            "seq_to_str": [{"input": n, "result": seq_to_str(n)} for n in [0, 1, 42, 10**15]],
            "envelopes": [{"input": e, "dump": _dump(AliveDeltaEnvelope.model_validate(e))} for e in env_inputs],
            "sync_info": {"input": info_input, "dump": _dump(HeartbeatSyncInfo.model_validate(info_input))},
            "snapshot_meta": {"input": meta_input, "dump": _dump(AliveSnapshotMeta.model_validate(meta_input))},
        },
    )


# ─── 7. metrics catalog / demo ────────────────────────────────────────────────


def gen_metrics() -> None:
    names = [
        "cpuUsage",
        "p95LatencyMs",
        "requestTotal",
        "uptimeSeconds",
        "amp_load_cpu_usage",
        "",
        "nonexistent",
        "toString",
        "__proto__",
        "CPUUSAGE",
    ]
    write_fixture(
        PKG,
        "metrics_catalog",
        {
            "families": {m.name: m.value for m in MetricFamily},
            "known_windows": list(KNOWN_WINDOWS),
            "known_quantiles": list(KNOWN_QUANTILES),
            "catalog": [[k, v.model_dump(mode="json")] for k, v in PUBLIC_METRIC_CATALOG.items()],
            "lookups": [
                {"name": n, "result": None if (m := lookup_public_metric(n)) is None else m.model_dump(mode="json")}
                for n in names
            ],
        },
    )

    metrics_demo._state.clear()
    series: list[dict[str, Any]] = []

    def take(label: str, sampler: Any) -> None:
        body = _dump(sampler.sample())
        body.pop("uptimeSeconds", None)
        series.append({"label": label, "body": body})

    a1 = metrics_demo.DemoMetricsSampler("fixture-aic-A")
    b1 = metrics_demo.DemoMetricsSampler("fixture-aic-B", max_active=3, max_queued=5)
    for _ in range(6):
        take("A1", a1)
    a2 = metrics_demo.DemoMetricsSampler("fixture-aic-A")  # 同 aic 共享模块级游走状态
    for _ in range(3):
        take("A2", a2)
        take("A1", a1)
        take("B1", b1)
    c1 = metrics_demo.DemoMetricsSampler("中文-aic-😀")
    for _ in range(4):
        take("C1", c1)

    # 一条 demo 记录的 NDJSON 行（含 float 字段的 repr，uptime 固定为 1.5）
    demo_body = _dump(metrics_demo.DemoMetricsSampler("fixture-aic-line").sample())
    demo_body["uptimeSeconds"] = 1.5
    rec_in = {"log_id": LOG_ID, "timestamp": TS_OFFSET, "aic": "fixture-aic-line", "body": demo_body}
    line = _write_line(
        MetricsEmitter(Path("unused.jsonl"), "x", sampler=MagicMock()), MetricsLogRecord.model_validate(rec_in)
    )
    write_fixture(PKG, "metrics_demo", {"series": series, "line": {"input": rec_in, "line": line}})


# ─── 8. message tap ───────────────────────────────────────────────────────────


def gen_message_tap() -> None:
    common = {"system": "rabbitmq", "exchange": "group.abc", "virtual_host": "acps"}
    cases = [
        {"fn": "send", "kwargs": {**common, "routing_key": "", "message_id": "cmd-001", "payload_size_bytes": 256}},
        {
            "fn": "send",
            "kwargs": {
                **common,
                "routing_key": "rk",
                "message_id": "cmd-002",
                "payload_size_bytes": 0,
                "attributes": {"k": "v", "n": None},
                "error": {"code": "E", "message": "m"},
            },
        },
        {
            "fn": "receive",
            "kwargs": {
                **common,
                "routing_key": "",
                "queue_name": "partner.queue.1",
                "message_id": "cmd-001",
                "payload_size_bytes": 256,
                "delivery_attempt": 1,
                "attributes": {"a": 1},
            },
        },
    ]
    for event in ["ack", "nack", "reject", "timeout", "dead_letter"]:
        cases.append(
            {
                "fn": "settlement",
                "event_type": event,
                "kwargs": {
                    **common,
                    "queue_name": "q",
                    "message_id": "cmd-003",
                    "delivery_attempt": 2,
                    "latency_ms": 12,
                    "reason": "why",
                },
            }
        )
    cases.append(
        {
            "fn": "settlement",
            "event_type": "nack",
            "kwargs": {**common, "queue_name": "q", "message_id": "m", "delivery_attempt": 1, "latency_ms": 0.5},
        }
    )
    out = []
    for case in cases:
        kwargs = dict(case["kwargs"])
        if "error" in kwargs:
            from acps_sdk.amp.models import ErrorInfo

            kwargs["error"] = ErrorInfo.model_validate(kwargs["error"])
        if case["fn"] == "send":
            body = tap.build_send_body(**kwargs)
        elif case["fn"] == "receive":
            body = tap.build_receive_body(**kwargs)
        else:
            body = tap.build_settlement_body(case["event_type"], **kwargs)
        rec = MessageLogRecord(log_id=LOG_ID, timestamp=TS_ZULU, aic="aic-tap", body=body)
        out.append({**case, "dump": _dump(body), "line": _write_line(MessageEmitter(Path("unused.jsonl"), "x"), rec)})
    errors = [
        {
            "event_type": e,
            "result": _error(
                lambda e=e: tap.build_settlement_body(
                    e, **common, queue_name="q", message_id="m", delivery_attempt=1, latency_ms=1.0
                )
            ),
        }
        for e in ["send", "receive", "ACK", "", "it's"]
    ]
    write_fixture(PKG, "message_tap", {"cases": out, "errors": errors})


# ─── 9. alive sync ────────────────────────────────────────────────────────────


class FakeStore:
    """与 tests/amp/alive_sync/conftest.py 中 FakeAliveSyncStore 相同的内存实现。"""

    def __init__(self) -> None:
        self.rows: dict[str, AliveRecord] = {}
        self.checkpoints: dict[str, ShardCheckpoint] = {}
        self.calls: list[Any] = []

    async def load_alive_views(self, aics: list[str]) -> dict[str, AliveView]:
        return {
            a: AliveView(aic=r.aic, alive=r.alive, last_seen_at=r.last_seen_at) for a in aics if (r := self.rows.get(a))
        }

    async def replace_alive_set(self, records: Any, checkpoints: list[ShardCheckpoint]) -> None:
        self.calls.append(["replace_alive_set", [_cp(c) for c in checkpoints]])
        self.rows.clear()
        self.checkpoints.clear()
        async for record in records:
            self.rows[record.aic] = record
        for cp in checkpoints:
            self.checkpoints[cp.shard] = cp

    async def apply_upsert(
        self, record: AliveRecord, shard: str, last_seen_seq: int, kafka_next_offset: int | None
    ) -> None:
        self.calls.append(["apply_upsert", _rec(record), shard, last_seen_seq, kafka_next_offset])
        self.rows[record.aic] = record
        old = self.checkpoints.get(shard)
        self.checkpoints[shard] = ShardCheckpoint(
            shard,
            last_seen_seq,
            old.cutover_seq if old else 0,
            kafka_next_offset,
            old.snapshot_generated_at if old else None,
        )

    async def apply_delete(
        self, aic: str, shard: str, last_seen_seq: int, kafka_next_offset: int | None, version: int
    ) -> None:
        self.calls.append(["apply_delete", aic, shard, last_seen_seq, kafka_next_offset, version])
        old_row = self.rows.get(aic)
        if old_row is not None:
            self.rows[aic] = AliveRecord(old_row.aic, False, old_row.last_seen_at, version, old_row.shard)
        old = self.checkpoints.get(shard)
        self.checkpoints[shard] = ShardCheckpoint(
            shard,
            last_seen_seq,
            old.cutover_seq if old else 0,
            kafka_next_offset,
            old.snapshot_generated_at if old else None,
        )

    async def load_checkpoints(self) -> list[ShardCheckpoint]:
        return list(self.checkpoints.values())

    async def load_local_versions(self) -> dict[str, int]:
        return {aic: row.version for aic, row in self.rows.items()}

    async def reset(self) -> None:
        self.rows.clear()
        self.checkpoints.clear()


def _rec(r: AliveRecord) -> dict[str, Any]:
    return {"aic": r.aic, "alive": r.alive, "lastSeenAt": r.last_seen_at, "version": r.version, "shard": r.shard}


def _cp(c: ShardCheckpoint) -> dict[str, Any]:
    return {
        "shard": c.shard,
        "lastSeenSeq": c.last_seen_seq,
        "cutoverSeq": c.cutover_seq,
        "kafkaNextOffset": c.kafka_next_offset,
        "snapshotGeneratedAt": c.snapshot_generated_at,
    }


def _env(
    seq: int | str,
    op: str = "upsert",
    kind: str = "enter_alive",
    aic: str = "AIC-001",
    shard: str = "hb-000",
    version: int | str | None = None,
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "shard": shard,
        "seq": str(seq),
        "type": "amp-alive-delta",
        "id": f"urn:amp:alive:{aic}",
        "version": str(seq if version is None else version),
        "op": op,
        "kind": kind,
    }
    if op == "upsert":
        env["payload"] = {"aic": aic, "lastSeenAt": f"2026-06-13T01:{int(str(seq)) % 60:02d}:00Z"}
    return env


async def _run_scenario(steps: list[dict[str, Any]]) -> dict[str, Any]:
    store = FakeStore()
    engine = AliveSyncEngine(store)
    results: list[Any] = []
    for step in steps:
        try:
            if step["op"] == "hydrate":
                await engine.hydrate()
                results.append({"ok": True})
            elif step["op"] == "snapshot":
                meta = AliveSnapshotMeta.model_validate(step["meta"])
                rows = [AliveDeltaEnvelope.model_validate(r) for r in step["rows"]]

                async def gen(rows: list[AliveDeltaEnvelope] = rows) -> Any:
                    for r in rows:
                        yield r

                await engine.apply_snapshot(meta, gen())
                results.append({"ok": True})
            elif step["op"] == "delta":
                decision = await engine.apply_delta(
                    AliveDeltaEnvelope.model_validate(step["env"]), kafka_next_offset=step.get("kafka_next_offset")
                )
                results.append({"ok": True, "decision": decision.name})
            elif step["op"] == "seed_checkpoint":
                store.checkpoints[step["cp"]["shard"]] = ShardCheckpoint(
                    **{
                        "shard": step["cp"]["shard"],
                        "last_seen_seq": step["cp"]["lastSeenSeq"],
                        "cutover_seq": step["cp"]["cutoverSeq"],
                        "kafka_next_offset": step["cp"]["kafkaNextOffset"],
                        "snapshot_generated_at": step["cp"]["snapshotGeneratedAt"],
                    }
                )
                results.append({"ok": True})
            elif step["op"] == "seed_row":
                r = step["row"]
                store.rows[r["aic"]] = AliveRecord(r["aic"], r["alive"], r["lastSeenAt"], r["version"], r["shard"])
                results.append({"ok": True})
        except AliveSyncError as exc:
            info: dict[str, Any] = {"ok": False, "type": type(exc).__name__, "message": str(exc)}
            for attr in ("shard", "expected_seq", "got_seq", "reason"):
                if hasattr(exc, attr):
                    info[attr] = getattr(exc, attr)
            results.append(info)
        except ValueError as exc:
            results.append({"ok": False, "type": "ValueError", "message": str(exc)})
    views = await store.load_alive_views(sorted(store.rows) + ["UNKNOWN"])
    return {
        "results": results,
        "calls": store.calls,
        "rows": {k: _rec(v) for k, v in store.rows.items()},
        "checkpoints": {k: _cp(v) for k, v in store.checkpoints.items()},
        "engine": {
            "last_seen_seq": dict(engine._last_seen_seq),
            "local_version": dict(engine._local_version),
            "cutover_seq_by_shard": engine.cutover_seq_by_shard(),
        },
        "views": {k: v.to_output_dict() for k, v in views.items()},
    }


def gen_alive_sync() -> None:
    meta = {
        "recordType": "snapshot-meta",
        "type": "amp-alive-delta",
        "cutoverSeqByShard": {"hb-000": "20", "hb-001": "7"},
        "generatedAt": "2026-06-13T01:00:00Z",
    }
    scenarios = {
        "hydrate_without_checkpoints": [{"op": "hydrate"}],
        "snapshot_then_deltas": [
            {
                "op": "snapshot",
                "meta": meta,
                "rows": [
                    {**_env(20, kind="snapshot", aic="AIC-001"), "version": "20"},
                    {**_env(18, kind="snapshot", aic="AIC-002"), "version": "18"},
                    {**_env(7, kind="snapshot", aic="AIC-003", shard="hb-001"), "version": "7"},
                ],
            },
            {"op": "delta", "env": _env(21, kind="refresh_alive", aic="AIC-001"), "kafka_next_offset": 101},
            {"op": "delta", "env": _env(21, kind="refresh_alive", aic="AIC-001"), "kafka_next_offset": 101},
            {"op": "delta", "env": _env(22, op="delete", kind="leave_alive", aic="AIC-002"), "kafka_next_offset": 102},
            {"op": "delta", "env": _env(23, kind="enter_alive", aic="AIC-004")},
            {"op": "delta", "env": _env(30, kind="enter_alive", aic="AIC-005")},
            {
                "op": "delta",
                "env": _env(8, op="delete", kind="leave_alive", aic="AIC-003", shard="hb-001"),
                "kafka_next_offset": 7,
            },
            {"op": "delta", "env": _env(9, kind="enter_alive", aic="AIC-003", shard="hb-001", version=5)},
            {"op": "delta", "env": _env(1, kind="enter_alive", aic="AIC-009", shard="hb-002")},
            {"op": "delta", "env": _env(3, kind="enter_alive", aic="AIC-009", shard="hb-002")},
            {"op": "delta", "env": _env("1000", kind="enter_alive", aic="AIC-001", shard="hb-003")},
            {"op": "delta", "env": {**_env(2, aic="AIC-001", shard="hb-002"), "seq": "abc"}},
            {"op": "delta", "env": {**_env(2, aic="AIC-001", shard="hb-002"), "id": "urn:bad:AIC-001"}},
            {"op": "hydrate"},
        ],
        "hydrate_restores": [
            {
                "op": "seed_checkpoint",
                "cp": {
                    "shard": "hb-000",
                    "lastSeenSeq": 10,
                    "cutoverSeq": 5,
                    "kafkaNextOffset": 100,
                    "snapshotGeneratedAt": "2026-06-13T00:00:00Z",
                },
            },
            {
                "op": "seed_row",
                "row": {
                    "aic": "AIC-001",
                    "alive": True,
                    "lastSeenAt": "2026-06-13T00:00:00Z",
                    "version": 10,
                    "shard": "hb-000",
                },
            },
            {"op": "hydrate"},
            {"op": "delta", "env": _env(11, aic="AIC-001")},
            {"op": "delta", "env": _env(12, aic="AIC-001", version=9)},
            {"op": "delta", "env": _env(14, aic="AIC-001")},
        ],
        "snapshot_bad_cutover": [
            {"op": "snapshot", "meta": {**meta, "cutoverSeqByShard": {"hb-000": "-1"}}, "rows": []},
        ],
    }
    scenario_out = {name: {"steps": steps, **asyncio.run(_run_scenario(steps))} for name, steps in scenarios.items()}

    seek_inputs = [
        ("2026-06-13T01:20:00Z", 0),
        ("2026-06-13T01:20:00Z", 300),
        ("2026-06-13T01:20:00Z", 999_999_999),
        ("2026-06-13T01:20:00+00:00", 60),
        ("2026-06-13T09:20:00+08:00", 0),
        ("2026-06-13T01:20:00.123456Z", 0),
        ("2026-06-13T01:20:00.1234567Z", 0),
        ("2026-06-13T01:20:00.5Z", 1),
        ("2026-06-13T01:20:00,25Z", 0),
        ("2026-06-13 01:20:00Z", 0),
        ("2026-06-13X01:20Z", 0),
        ("20260613T012000Z", 0),
        ("20260613T012000+0530", 0),
        ("2026-06-13T01Z", 0),
        ("2026-06-13T01:20:00-08:00", 10),
        ("2026-06-13T01:20:00+05:30:15.5", 0),
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T00:00:00.000999Z", 0),
        ("1969-12-31T23:59:59.9995Z", 0),
        ("0001-01-01T00:00:00Z", 0),
        ("9999-12-31T23:59:59.999999Z", 0),
        ("2024-02-29T12:00:00Z", 0),
        ("2026-06-13T01:20:00.001Z", 0),
        ("2026-06-13T01:20:00.003Z", 0),
        ("2026-06-13T01:20:00.007Z", 0),
        ("2033-05-18T03:33:20.999999Z", 0),
    ]
    invalid_seek = [
        "not-a-date",
        "2026-06-13T24:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-06-13T01:60:00Z",
        "2026-06-13T01:20:60Z",
        "2026-06-13T1:20:00Z",
        " 2026-06-13T01:20:00Z",
        "2026-06-13T01:20:00Z ",
        "2026-06",
        "2026-06-13T01:20:00+24:00",
        "2026-06-13T01:20:00+2",
        "2026-06-13T01:20:00+123",
        "2026-06-13T01:20:00Zjunk",
        "2026-06-13T01:2",
        "2026-06-13T01:20:00.12a",
        "2026/06/13T01:20:00Z",
        "0000-01-01T00:00:00Z",
        "",
        # 日期后多出一个字符却没有合法时间部分
        "2026-06-013",
        "20260613t",
        "20260613T",
        "2026-06-13T",
        "2026-06-13 ",
        "2026-06-13-",
        "2026-06-13+",
        "2026-06-13T0",
    ]
    seek = [{"generated_at": g, "lookback_seconds": lb, "result": seek_timestamp_ms(g, lb)} for g, lb in seek_inputs]
    seek_errors = [{"generated_at": g, "result": _error(lambda g=g: seek_timestamp_ms(g, 0))} for g in invalid_seek]
    lookback = [
        {"current": c, "factor": f, "max_seconds": m, "result": next_lookback_seconds(c, factor=f, max_seconds=m)}
        for c, f, m in [(300, 2, 86400), (50000, 2, 86400), (86400, 2, 86400), (100, 3, 10000), (0, 2, 10)]
    ]

    valid_meta = {
        "recordType": "snapshot-meta",
        "type": "amp-alive-delta",
        "cutoverSeqByShard": {"hb-000": "42"},
        "generatedAt": "2026-06-13T01:20:00Z",
    }
    valid_row = {
        "shard": "hb-000",
        "seq": "43",
        "type": "amp-alive-delta",
        "id": "urn:amp:alive:AIC-001",
        "version": "43",
        "op": "upsert",
        "kind": "snapshot",
        "payload": {"aic": "AIC-001", "lastSeenAt": "2026-06-13T01:19:50Z"},
    }
    meta_lines = [
        json.dumps(valid_meta),
        json.dumps({**valid_meta, "recordType": "wrong"}),
        json.dumps({**valid_meta, "recordType": None}),
        json.dumps({**valid_meta, "type": 1.5}),
        json.dumps({**valid_meta, "type": True}),
        json.dumps({k: v for k, v in valid_meta.items() if k != "type"}),
        json.dumps({**valid_meta, "recordType": {"a": [1, None]}}),
    ]
    row_lines = [
        json.dumps(valid_row),
        json.dumps({**valid_row, "kind": "enter_alive"}),
        json.dumps({**valid_row, "op": "delete"}),
        json.dumps({k: v for k, v in valid_row.items() if k != "payload"}),
        json.dumps({**valid_row, "payload": {}}),
        json.dumps({**valid_row, "payload": 0}),
        json.dumps({**valid_row, "payload": []}),
        json.dumps({**valid_row, "kind": None}),
    ]

    def protocol(fn: Callable[[], Any]) -> dict[str, Any]:
        try:
            return {"ok": True, "value": _dump(fn())}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            return {
                "ok": False,
                "type": type(exc).__name__,
                "message": msg if "校验失败" not in msg else msg.split(":")[0],
            }

    write_fixture(
        PKG,
        "alive_sync",
        {
            "scenarios": scenario_out,
            "seek_timestamp_ms": seek,
            "seek_timestamp_ms_errors": seek_errors,
            "next_lookback_seconds": lookback,
            "parse_meta_line": [
                {"line": line, "result": protocol(lambda line=line: parse_meta_line(line))} for line in meta_lines
            ],
            "parse_snapshot_row": [
                {"line": line, "result": protocol(lambda line=line: parse_snapshot_row(line))} for line in row_lines
            ],
            "snapshot_row_to_record": _rec(snapshot_row_to_record(parse_snapshot_row(json.dumps(valid_row)))),
        },
    )


# ─── 10. 包导出（acps_sdk.amp.__all__）────────────────────────────────────────


def _public_methods(cls: type) -> list[str]:
    """类自身定义的公开方法（不含继承自 BaseModel 的方法与 pydantic 内部属性）。"""
    return sorted(
        name
        for name, value in vars(cls).items()
        if not name.startswith("_")
        and name != "model_config"
        and (inspect.isfunction(value) or isinstance(value, (staticmethod, classmethod)))
    )


def _export_kind(obj: Any) -> str:
    if isinstance(obj, type) and issubclass(obj, BaseModel):
        return "model"
    if dataclasses.is_dataclass(obj):
        return "dataclass"
    if isinstance(obj, type) and getattr(obj, "_is_protocol", False):
        return "protocol"
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        return "enum"
    if isinstance(obj, type):
        return "class"
    if inspect.isfunction(obj):
        return "function"
    if typing.get_origin(obj) is typing.Literal:
        return "type_alias"
    return "constant"


def gen_exports() -> None:
    exports = []
    for name in amp_package.__all__:
        obj = getattr(amp_package, name)
        kind = _export_kind(obj)
        methods = _public_methods(obj) if kind in ("model", "dataclass") else []
        exports.append({"name": name, "kind": kind, "methods": methods})
    write_fixture(PKG, "exports", {"all": exports})


def main() -> None:
    gen_records()
    gen_jcs()
    gen_floats()
    gen_signatures()
    gen_trace_context()
    gen_heartbeat_sync()
    gen_metrics()
    gen_message_tap()
    gen_alive_sync()
    gen_exports()


if __name__ == "__main__":
    main()
