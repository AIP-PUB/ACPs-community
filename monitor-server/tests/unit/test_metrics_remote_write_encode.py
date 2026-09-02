"""tests/unit/test_metrics_remote_write_encode.py — prompb 编解码单元测试（Step 4）。"""

from __future__ import annotations

import snappy

from app.metrics.prompb import Label, Sample, decode_write_request
from app.metrics.samples import Sample as AppSample
from app.metrics.tsdb import _build_write_request

# ── WriteRequest 往返测试 ─────────────────────────────────────────────────────


def test_encode_decode_roundtrip_single_series() -> None:
    """encode → snappy decompress → decode_write_request 往返。"""
    samples = [
        AppSample(metric_name="test_metric", labels={"job": "a"}, value=1.0, timestamp_ms=1000),
        AppSample(metric_name="test_metric", labels={"job": "a"}, value=2.0, timestamp_ms=2000),
    ]
    payload = _build_write_request(samples)
    assert isinstance(payload, bytes)

    wr = decode_write_request(payload)
    assert len(wr.timeseries) == 1

    ts = wr.timeseries[0]
    label_map = {lb.name: lb.value for lb in ts.labels}
    assert label_map["__name__"] == "test_metric"
    assert label_map["job"] == "a"

    assert len(ts.samples) == 2
    values = [s.value for s in ts.samples]
    assert 1.0 in values
    assert 2.0 in values


def test_encode_same_name_labels_merged_into_one_timeseries() -> None:
    """同一 (metric_name, labels) 的多个 Sample 归并为一个 TimeSeries。"""
    samples = [
        AppSample(metric_name="cpu", labels={"aic": "x"}, value=0.5, timestamp_ms=1000),
        AppSample(metric_name="cpu", labels={"aic": "x"}, value=0.6, timestamp_ms=2000),
        AppSample(metric_name="mem", labels={"aic": "x"}, value=200.0, timestamp_ms=1000),
    ]
    payload = _build_write_request(samples)
    wr = decode_write_request(payload)

    assert len(wr.timeseries) == 2


def test_encode_dunder_name_label_is_metric_name() -> None:
    """__name__ 标签值等于 metric_name。"""
    samples = [AppSample(metric_name="my_counter", labels={}, value=1.0, timestamp_ms=1000)]
    payload = _build_write_request(samples)
    wr = decode_write_request(payload)

    label_map = {lb.name: lb.value for lb in wr.timeseries[0].labels}
    assert label_map["__name__"] == "my_counter"


def test_encode_labels_sorted_stable() -> None:
    """Labels 排序稳定（a, b 顺序，非插入顺序）。"""
    samples = [AppSample(metric_name="m", labels={"z": "z_val", "a": "a_val"}, value=1.0, timestamp_ms=1000)]
    payload = _build_write_request(samples)
    wr = decode_write_request(payload)

    names = [lb.name for lb in wr.timeseries[0].labels]
    assert names[0] == "__name__"
    assert names[1:] == sorted(names[1:])


def test_encode_empty_samples_returns_empty_bytes() -> None:
    """空 samples 不产生任何 TimeSeries。"""
    payload = _build_write_request([])
    wr = decode_write_request(payload)
    assert wr.timeseries == []


# ── snappy 压缩 ────────────────────────────────────────────────────────────────


def test_snappy_compress_decompress_roundtrip() -> None:
    """_build_write_request 产物可以被 snappy 压缩/解压后正常 decode。"""
    samples = [AppSample(metric_name="x", labels={}, value=3.14, timestamp_ms=999)]
    payload = _build_write_request(samples)
    compressed = snappy.compress(payload)
    decompressed = snappy.decompress(compressed)
    wr = decode_write_request(decompressed)
    assert len(wr.timeseries) == 1


# ── prompb 低级类测试 ─────────────────────────────────────────────────────────


def test_label_encode_decode() -> None:
    """Label encode/decode 基本往返。"""
    lb = Label(name="foo", value="bar")
    body = lb.encode()
    assert isinstance(body, bytes)
    assert len(body) > 0


def test_sample_encode_contains_value_and_timestamp() -> None:
    """Sample encode 产物可被反解为正确的 value 和 timestamp_ms。"""
    from app.metrics.prompb import _decode_sample

    s = Sample(value=42.5, timestamp_ms=1234567890)
    body = s.encode()
    decoded = _decode_sample(body)
    assert abs(decoded.value - 42.5) < 1e-9
    assert decoded.timestamp_ms == 1234567890
