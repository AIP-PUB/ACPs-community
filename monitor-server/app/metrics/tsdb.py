"""app/metrics/tsdb.py — VictoriaMetrics HTTP 客户端（查询 + Remote Write）。

封装对 vmselect（读）与 vminsert/vmagent（写）的全部 HTTP 调用：
- 查询走 JSON API（/api/v1/query 和 /api/v1/query_range）
- Remote Write 走 protobuf + snappy 压缩

模块级懒初始化单例 httpx.AsyncClient（模式对齐 redis_client.py）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import snappy
import structlog

from app.metrics.exception import ReadModelLaggingError, RemoteWriteError
from app.metrics.prompb import Label, TimeSeries, WriteRequest
from app.metrics.prompb import Sample as PrompbSample
from app.metrics.samples import Sample

logger = structlog.get_logger(__name__)

# ── 模块级单例 ─────────────────────────────────────────────────────────────────

_tsdb_client: httpx.AsyncClient | None = None


def get_tsdb_client() -> httpx.AsyncClient:
    """懒初始化 httpx.AsyncClient 单例。

    base_url 不设（query_url / remote_write_url 可能不同地址）；
    timeout 读配置 metrics_query_timeout_seconds。
    """
    global _tsdb_client
    if _tsdb_client is None:
        from app.core.config import get_settings

        s = get_settings()
        _tsdb_client = httpx.AsyncClient(timeout=s.metrics_query_timeout_seconds)
    return _tsdb_client


async def close_tsdb_client() -> None:
    """关闭 httpx.AsyncClient（lifespan 时调用，幂等）。"""
    global _tsdb_client
    if _tsdb_client is not None:
        await _tsdb_client.aclose()
        _tsdb_client = None


# ── 查询结果数据类 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstantSample:
    """单条即时查询结果（vector 中一条）。"""

    labels: dict[str, str]
    value: float
    timestamp_ms: int


@dataclass(frozen=True)
class RangeSeries:
    """Range query 单条时序（matrix 中一条）。"""

    labels: dict[str, str]
    points: list[tuple[int, float]] = field(default_factory=list)
    """(timestamp_ms, value) 升序列表。"""


# ── 工具：时间转换 ─────────────────────────────────────────────────────────────


def _to_unix_seconds(dt: datetime) -> float:
    """将 aware datetime 转换为 Unix 秒（float）。"""
    return dt.timestamp()


def _parse_vm_value(v: str) -> float:
    """解析 VictoriaMetrics 返回的 value 字符串（NaN/+Inf/-Inf 兜底为 0.0）。"""
    try:
        return float(v)
    except ValueError, TypeError:
        return 0.0


# ── 即时查询 ──────────────────────────────────────────────────────────────────


async def instant(expr: str, *, at: datetime) -> list[InstantSample]:
    """即时查询（§6.0.1.A），返回 vector 型结果。

    Args:
        expr: MetricsQL / PromQL 表达式。
        at: 查询时刻（aware UTC datetime）。

    Returns:
        list[InstantSample]

    Raises:
        ReadModelLaggingError: 查询超时（从 TSDB 语义上视为读模型滞后）。
        RemoteWriteError: 非 2xx 响应（复用此异常向上报告 TSDB 不可用）。
    """
    from app.core.config import get_settings

    s = get_settings()
    client = get_tsdb_client()
    url = f"{s.vm_query_url.rstrip('/')}/api/v1/query"
    params: dict[str, str] = {"query": expr, "time": str(_to_unix_seconds(at))}

    try:
        resp = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise ReadModelLaggingError(f"TSDB instant query timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RemoteWriteError(f"TSDB instant query failed: {exc}") from exc

    if resp.status_code != 200:
        raise RemoteWriteError(f"TSDB instant query returned {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    result = body.get("data", {}).get("result", [])
    return [
        InstantSample(
            labels=dict(item["metric"]),
            value=_parse_vm_value(item["value"][1]),
            timestamp_ms=int(float(item["value"][0]) * 1000),
        )
        for item in result
    ]


async def instant_many[K](exprs: dict[K, str], *, at: datetime) -> dict[K, list[InstantSample]]:
    """并发执行多个即时查询（asyncio.gather）。

    Args:
        exprs: key → expr 映射。
        at: 查询时刻。

    Returns:
        dict[K, list[InstantSample]]
    """
    import asyncio

    keys = list(exprs.keys())
    tasks = [instant(exprs[k], at=at) for k in keys]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return dict(zip(keys, results, strict=True))


# ── Range 查询 ────────────────────────────────────────────────────────────────


async def range_query(
    expr: str,
    *,
    start: datetime,
    end: datetime,
    step_ms: int,
) -> list[RangeSeries]:
    """Range query（§6.2），返回 matrix 型结果。

    Args:
        expr: MetricsQL / PromQL 表达式。
        start: 起始时刻（aware UTC datetime）。
        end: 结束时刻（aware UTC datetime）。
        step_ms: 步长毫秒。

    Returns:
        list[RangeSeries]
    """
    from app.core.config import get_settings

    s = get_settings()
    client = get_tsdb_client()
    url = f"{s.vm_query_url.rstrip('/')}/api/v1/query_range"
    params: dict[str, str] = {
        "query": expr,
        "start": str(_to_unix_seconds(start)),
        "end": str(_to_unix_seconds(end)),
        "step": f"{step_ms}ms",
    }

    try:
        resp = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise ReadModelLaggingError(f"TSDB range query timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RemoteWriteError(f"TSDB range query failed: {exc}") from exc

    if resp.status_code != 200:
        raise RemoteWriteError(f"TSDB range query returned {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    result = body.get("data", {}).get("result", [])
    return [
        RangeSeries(
            labels=dict(item["metric"]),
            points=[(int(float(pt[0]) * 1000), _parse_vm_value(pt[1])) for pt in item["values"]],
        )
        for item in result
    ]


async def range_many[K](
    exprs: dict[K, str],
    *,
    start: datetime,
    end: datetime,
    step_ms: int,
) -> dict[K, list[RangeSeries]]:
    """并发执行多个 range query（capacity 候选明细并发拉取）。"""
    import asyncio

    keys = list(exprs.keys())
    tasks = [range_query(exprs[k], start=start, end=end, step_ms=step_ms) for k in keys]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return dict(zip(keys, results, strict=True))


# ── Remote Write ──────────────────────────────────────────────────────────────


def encode_write_request(samples: list[Sample]) -> bytes:
    """将 Sample 列表序列化为 Prometheus Remote Write protobuf payload（snappy 前，§8.2 方案 A）。"""
    return _build_write_request(samples)


def _build_write_request(samples: list[Sample]) -> bytes:
    """将 Sample 列表序列化为 Prometheus Remote Write protobuf payload（snappy 前）。

    同一 (metric_name, labels) 归并为一个 TimeSeries；__name__ 作为 metric 名标签。
    """
    # 按 (metric_name, frozenset(labels.items())) 分组
    grouped: dict[tuple[str, frozenset[tuple[str, str]]], list[Sample]] = {}
    for s in samples:
        key = (s.metric_name, frozenset(s.labels.items()))
        grouped.setdefault(key, []).append(s)

    time_series_list = []
    for (metric_name, _), group in grouped.items():
        first = group[0]
        # 构造 labels（排序保证稳定）
        lbs = [Label(name="__name__", value=metric_name)]
        for k, v in sorted(first.labels.items()):
            lbs.append(Label(name=k, value=v))
        # 构造 samples
        pb_samples = [PrompbSample(value=s.value, timestamp_ms=s.timestamp_ms) for s in group]
        time_series_list.append(TimeSeries(labels=lbs, samples=pb_samples))

    wr = WriteRequest(timeseries=time_series_list)
    return wr.encode()


async def remote_write(samples: list[Sample]) -> None:
    """将 Sample 列表通过 Prometheus Remote Write 协议写入 VictoriaMetrics。

    含 3 次有界重试（§6.14：tsdb 内 retry，Writer 层不 commit 直至成功）。

    Args:
        samples: 待写入的样本列表。

    Raises:
        RemoteWriteError: 写入失败（非 2xx 或网络异常），Writer 据此重试。
    """
    from app.core.config import get_settings
    from app.metrics.metrics import metrics as _metrics

    if not samples:
        return

    s = get_settings()
    client = get_tsdb_client()
    url = f"{s.vm_remote_write_url.rstrip('/')}/api/v1/write"

    payload = encode_write_request(samples)
    compressed = snappy.compress(payload)

    headers = {
        "Content-Encoding": "snappy",
        "Content-Type": "application/x-protobuf",
        "X-Prometheus-Remote-Write-Version": "0.1.0",
    }

    max_attempts = 3
    last_exc: RemoteWriteError | None = None

    for attempt in range(1, max_attempts + 1):
        t0 = time.monotonic()
        try:
            resp = await client.post(url, content=compressed, headers=headers)
            if resp.status_code in (200, 204):
                elapsed = int((time.monotonic() - t0) * 1000)
                _metrics.observe_ms("amp_metrics_remote_write_latency_ms", elapsed)
                logger.debug("tsdb.remote_write.done", samples=len(samples), elapsed_ms=elapsed, attempt=attempt)
                return
            last_exc = RemoteWriteError(f"Remote Write returned {resp.status_code}: {resp.text[:200]}")
        except httpx.TimeoutException as exc:
            last_exc = RemoteWriteError(f"Remote Write timed out: {exc}")
        except httpx.HTTPError as exc:
            last_exc = RemoteWriteError(f"Remote Write network error: {exc}")

        if attempt < max_attempts:
            await asyncio.sleep(0.1 * (2 ** (attempt - 1)))

    if last_exc is None:
        raise RemoteWriteError("Remote Write failed without exception")
    raise last_exc


# ── 健康探活 ──────────────────────────────────────────────────────────────────


async def check_victoria_metrics() -> bool:
    """探活 VictoriaMetrics（/api/v1/query?query=vm_app_version 或 /health）。

    Returns:
        bool: True 表示健康，False 表示不可达或返回非 200。
    """
    from app.core.config import get_settings

    s = get_settings()
    client = get_tsdb_client()
    url = f"{s.vm_query_url.rstrip('/')}/api/v1/query"

    try:
        resp = await client.get(url, params={"query": "vm_app_version"}, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


__all__ = [
    "InstantSample",
    "RangeSeries",
    "check_victoria_metrics",
    "close_tsdb_client",
    "encode_write_request",
    "get_tsdb_client",
    "instant",
    "instant_many",
    "range_many",
    "range_query",
    "remote_write",
]
