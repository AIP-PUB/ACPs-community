"""tests/support/vm_helper.py — VictoriaMetrics respx 拦截辅助函数（Step 4 §4.1.4）。

用法（集成/单元测试）：
    mount_vm(respx_mock, query=vector_result(({}, 1.0, ts)), query_range=matrix_result())
    decode_remote_write(body)  # 反解 encode_write_request 产物供断言
"""

from __future__ import annotations

import snappy

from app.metrics.prompb import (
    decode_write_request,
)
from tests.support.constants import TEST_VM_QUERY_URL, TEST_VM_REMOTE_WRITE_URL

# ── 构造虚假 VM 响应 ──────────────────────────────────────────────────────────


def vector_result(*samples: tuple[dict[str, str], float, int]) -> dict:
    """构造 VictoriaMetrics instant query 返回值（vector 格式）。

    Args:
        samples: (labels, value, timestamp_ms) 元组列表。

    Returns:
        符合 VM /api/v1/query 响应格式的 dict。
    """
    result = [
        {
            "metric": labels,
            "value": [str(ts_ms / 1000), str(v)],
        }
        for labels, v, ts_ms in samples
    ]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def matrix_result(*series: tuple[dict[str, str], list[tuple[int, float]]]) -> dict:
    """构造 VictoriaMetrics range query 返回值（matrix 格式）。

    Args:
        series: (labels, [(timestamp_ms, value)]) 元组列表。

    Returns:
        符合 VM /api/v1/query_range 响应格式的 dict。
    """
    result = [
        {
            "metric": labels,
            "values": [[str(ts_ms / 1000), str(v)] for ts_ms, v in points],
        }
        for labels, points in series
    ]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def empty_vector() -> dict:
    """空 vector 响应（无数据）。"""
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def empty_matrix() -> dict:
    """空 matrix 响应（无数据）。"""
    return {"status": "success", "data": {"resultType": "matrix", "result": []}}


def mount_vm(
    respx_mock: object,
    *,
    query: dict | None = None,
    query_range: dict | None = None,
    write_status: int = 204,
) -> None:
    """在 respx_mock 上注册 VictoriaMetrics 端点拦截。

    Args:
        respx_mock: respx 的 MockRouter 实例（由 pytest fixture 注入）。
        query: /api/v1/query 返回内容（None 则返回空 vector）。
        query_range: /api/v1/query_range 返回内容（None 则返回空 matrix）。
        write_status: /api/v1/write 返回状态码（默认 204）。
    """
    import httpx

    query_base = TEST_VM_QUERY_URL.rstrip("/")
    write_base = TEST_VM_REMOTE_WRITE_URL.rstrip("/")

    respx_mock.get(f"{query_base}/api/v1/query").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=query or empty_vector())
    )
    respx_mock.get(f"{query_base}/api/v1/query_range").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=query_range or empty_matrix())
    )
    respx_mock.post(f"{write_base}/api/v1/write").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(write_status)
    )


# ── Remote Write 反解 ──────────────────────────────────────────────────────────


def decode_remote_write(body: bytes) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    """反解 remote_write 请求体，返回 [(labels, [(timestamp_ms, value)])]。

    支持 snappy 压缩（如果 magic bytes 匹配）或未压缩的原始 protobuf。

    Args:
        body: Remote Write HTTP 请求体。

    Returns:
        [(labels_dict, [(timestamp_ms, value)])]
    """
    # 尝试 snappy 解压
    try:
        raw = snappy.decompress(body)
    except Exception:
        raw = body

    wr = decode_write_request(raw)

    result = []
    for ts in wr.timeseries:
        labels = {lb.name: lb.value for lb in ts.labels}
        points = [(s.timestamp_ms, s.value) for s in ts.samples]
        result.append((labels, points))
    return result


__all__ = [
    "decode_remote_write",
    "empty_matrix",
    "empty_vector",
    "matrix_result",
    "mount_vm",
    "vector_result",
]
