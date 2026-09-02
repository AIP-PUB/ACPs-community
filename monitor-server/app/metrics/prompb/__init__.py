"""Prometheus Remote Write protobuf 序列化模块。

手写最小实现（无需构建期 protoc），覆盖 WriteRequest / TimeSeries / Label / Sample。
产物直接纳入仓库，依赖标准库 struct；编码方式与 prometheus/prometheus remote.proto 完全一致。

Wire format 参考（protobuf binary encoding）：
  WriteRequest  { 1: repeated TimeSeries timeseries }
  TimeSeries    { 1: repeated Label labels, 2: repeated Sample samples }
  Label         { 1: string name, 2: string value }
  Sample        { 1: double value, 2: int64 timestamp_ms }
"""

from __future__ import annotations

import struct

# ── protobuf 基元编码 ──────────────────────────────────────────────────────────


def _encode_varint(value: int) -> bytes:
    """将非负整数编码为 LEB128 varint（protobuf unsigned/signed）。"""
    buf = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            buf.append(bits | 0x80)
        else:
            buf.append(bits)
            break
    return bytes(buf)


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    """编码 protobuf tag（field number + wire type）。"""
    return _encode_varint((field_number << 3) | wire_type)


_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2


def _encode_bytes_field(field_number: int, data: bytes) -> bytes:
    """编码 length-delimited 字段（wire type 2）：string / bytes / embedded message。"""
    tag = _encode_tag(field_number, _WIRE_LEN)
    length = _encode_varint(len(data))
    return tag + length + data


def _encode_string_field(field_number: int, value: str) -> bytes:
    """编码 string 字段。"""
    return _encode_bytes_field(field_number, value.encode("utf-8"))


def _encode_double_field(field_number: int, value: float) -> bytes:
    """编码 double 字段（wire type 1，little-endian 8 bytes）。"""
    tag = _encode_tag(field_number, _WIRE_64BIT)
    return tag + struct.pack("<d", value)


def _encode_int64_field(field_number: int, value: int) -> bytes:
    """编码 int64 字段（wire type 0，varint — protobuf int64 使用正 varint）。"""
    tag = _encode_tag(field_number, _WIRE_VARINT)
    # protobuf int64 用 varint 编码；负数须转为 two's-complement uint64
    if value < 0:
        value = value + (1 << 64)
    return tag + _encode_varint(value)


# ── 数据类（对外接口）────────────────────────────────────────────────────────────


class Label:
    """protobuf Label { string name = 1; string value = 2; }"""

    __slots__ = ("name", "value")

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value

    def encode(self) -> bytes:
        """返回 Label message body（不含外层 tag/length）。"""
        return _encode_string_field(1, self.name) + _encode_string_field(2, self.value)


class Sample:
    """protobuf Sample { double value = 1; int64 timestamp = 2; }"""

    __slots__ = ("timestamp_ms", "value")

    def __init__(self, value: float, timestamp_ms: int) -> None:
        self.value = value
        self.timestamp_ms = timestamp_ms

    def encode(self) -> bytes:
        """返回 Sample message body（不含外层 tag/length）。"""
        return _encode_double_field(1, self.value) + _encode_int64_field(2, self.timestamp_ms)


class TimeSeries:
    """protobuf TimeSeries { repeated Label labels = 1; repeated Sample samples = 2; }"""

    __slots__ = ("labels", "samples")

    def __init__(self, labels: list[Label], samples: list[Sample]) -> None:
        self.labels = labels
        self.samples = samples

    def encode(self) -> bytes:
        """返回 TimeSeries message body（不含外层 tag/length）。"""
        parts: list[bytes] = []
        for label in self.labels:
            body = label.encode()
            parts.append(_encode_bytes_field(1, body))
        for sample in self.samples:
            body = sample.encode()
            parts.append(_encode_bytes_field(2, body))
        return b"".join(parts)


class WriteRequest:
    """protobuf WriteRequest { repeated TimeSeries timeseries = 1; }"""

    __slots__ = ("timeseries",)

    def __init__(self, timeseries: list[TimeSeries]) -> None:
        self.timeseries = timeseries

    def encode(self) -> bytes:
        """返回完整 WriteRequest protobuf 字节串。"""
        parts: list[bytes] = []
        for ts in self.timeseries:
            body = ts.encode()
            parts.append(_encode_bytes_field(1, body))
        return b"".join(parts)


# ── 反解（仅供测试 decode_remote_write 使用）────────────────────────────────────


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """从 pos 位置读取一个 varint，返回 (value, new_pos)。"""
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result, pos


def _read_field(data: bytes, pos: int) -> tuple[int, int, bytes | int | float]:
    """读取一个字段，返回 (field_number, wire_type, value) 和 new_pos。

    value 类型：
      wire 0 → int（varint）
      wire 1 → float（double）
      wire 2 → bytes（length-delimited）
    """
    tag, pos = _read_varint(data, pos)
    field_number = tag >> 3
    wire_type = tag & 0x7

    if wire_type == _WIRE_VARINT:
        value_int, pos = _read_varint(data, pos)
        return field_number, wire_type, value_int
    if wire_type == _WIRE_64BIT:
        value_f = struct.unpack_from("<d", data, pos)[0]
        pos += 8
        return field_number, wire_type, value_f
    if wire_type == _WIRE_LEN:
        length, pos = _read_varint(data, pos)
        value_b = data[pos : pos + length]
        pos += length
        return field_number, wire_type, value_b
    raise ValueError(f"不支持的 wire type: {wire_type}")


def decode_write_request(data: bytes) -> WriteRequest:
    """将 WriteRequest protobuf 字节串反解为 WriteRequest 对象（仅供测试）。"""
    pos = 0
    timeseries: list[TimeSeries] = []
    while pos < len(data):
        field_number, _, value = _read_field(data, pos)
        # 计算 new_pos
        tag, p2 = _read_varint(data, pos)
        wt = tag & 0x7
        if wt == _WIRE_LEN:
            length, p3 = _read_varint(data, p2)
            pos = p3 + length
        elif wt == _WIRE_VARINT:
            _, pos = _read_varint(data, p2)
        elif wt == _WIRE_64BIT:
            pos = p2 + 8
        else:
            raise ValueError(f"顶层字段 wire type 不支持: {wt}")

        if field_number == 1 and isinstance(value, bytes):
            timeseries.append(_decode_timeseries(value))

    return WriteRequest(timeseries=timeseries)


def _decode_timeseries(data: bytes) -> TimeSeries:
    pos = 0
    labels: list[Label] = []
    samples: list[Sample] = []
    while pos < len(data):
        field_number, _, value = _read_field(data, pos)
        tag, p2 = _read_varint(data, pos)
        wt = tag & 0x7
        if wt == _WIRE_LEN:
            length, p3 = _read_varint(data, p2)
            pos = p3 + length
        elif wt == _WIRE_VARINT:
            _, pos = _read_varint(data, p2)
        elif wt == _WIRE_64BIT:
            pos = p2 + 8
        else:
            raise ValueError(f"TimeSeries 字段 wire type 不支持: {wt}")

        if isinstance(value, bytes):
            if field_number == 1:
                labels.append(_decode_label(value))
            elif field_number == 2:
                samples.append(_decode_sample(value))
    return TimeSeries(labels=labels, samples=samples)


def _decode_label(data: bytes) -> Label:
    pos = 0
    name = ""
    value = ""
    while pos < len(data):
        field_number, _, fv = _read_field(data, pos)
        tag, p2 = _read_varint(data, pos)
        wt = tag & 0x7
        if wt == _WIRE_LEN:
            length, p3 = _read_varint(data, p2)
            pos = p3 + length
        elif wt == _WIRE_VARINT:
            _, pos = _read_varint(data, p2)
        elif wt == _WIRE_64BIT:
            pos = p2 + 8
        else:
            raise ValueError(f"Label 字段 wire type 不支持: {wt}")
        if isinstance(fv, bytes):
            if field_number == 1:
                name = fv.decode("utf-8")
            elif field_number == 2:
                value = fv.decode("utf-8")
    return Label(name=name, value=value)


def _decode_sample(data: bytes) -> Sample:
    pos = 0
    value = 0.0
    timestamp_ms = 0
    while pos < len(data):
        field_number, _, fv = _read_field(data, pos)
        tag, p2 = _read_varint(data, pos)
        wt = tag & 0x7
        if wt == _WIRE_LEN:
            length, p3 = _read_varint(data, p2)
            pos = p3 + length
        elif wt == _WIRE_VARINT:
            _, pos = _read_varint(data, p2)
        elif wt == _WIRE_64BIT:
            pos = p2 + 8
        else:
            raise ValueError(f"Sample 字段 wire type 不支持: {wt}")
        if field_number == 1 and isinstance(fv, float):
            value = fv
        elif field_number == 2 and isinstance(fv, int):
            # int64 negative varint → two's-complement
            if fv >= (1 << 63):
                fv -= 1 << 64
            timestamp_ms = fv
    return Sample(value=value, timestamp_ms=timestamp_ms)


__all__ = [
    "Label",
    "Sample",
    "TimeSeries",
    "WriteRequest",
    "decode_write_request",
]
