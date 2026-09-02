"""单元测试：AMPResponseMeta 新增字段及 AMPFieldSampleCoverage（A-1）。

验证：
- AMPResponseMeta.partial_data_fields / sample_coverage 为可选字段（默认 None）
- camelCase 别名序列化正确
- exclude_none=True 时默认 None 字段不输出（向后兼容）
- 现有字段不受影响（回归）
- AMPFieldSampleCoverage 四字段正确序列化
"""

from __future__ import annotations

import pytest

from app.core.amp_api_schema import (
    AMPFieldSampleCoverage,
    AMPResponseMeta,
)


class TestAMPResponseMetaNewFields:
    """partial_data_fields / sample_coverage 新增字段。"""

    def test_partial_data_fields_default_none(self) -> None:
        meta = AMPResponseMeta()
        assert meta.partial_data_fields is None

    def test_sample_coverage_default_none(self) -> None:
        meta = AMPResponseMeta()
        assert meta.sample_coverage is None

    def test_partial_data_fields_camel_alias(self) -> None:
        meta = AMPResponseMeta(partial_data_fields=["visibleDepth"])
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "partialDataFields" in dumped
        assert dumped["partialDataFields"] == ["visibleDepth"]

    def test_sample_coverage_camel_alias(self) -> None:
        coverage = AMPFieldSampleCoverage(
            available_samples=8,
            total_samples=10,
            coverage_ratio=0.8,
            status="partial",
        )
        meta = AMPResponseMeta(sample_coverage={"visibleDepth": coverage})
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "sampleCoverage" in dumped

    def test_partial_data_fields_excluded_when_none(self) -> None:
        meta = AMPResponseMeta()
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "partialDataFields" not in dumped
        assert "sampleCoverage" not in dumped

    def test_existing_fields_not_broken(self) -> None:
        meta = AMPResponseMeta(
            data_freshness_at="2025-01-01T00:00:00Z",
            next_cursor="abc123",
        )
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert dumped["dataFreshnessAt"] == "2025-01-01T00:00:00Z"
        assert dumped["nextCursor"] == "abc123"
        assert "partialDataFields" not in dumped

    def test_populate_by_name_snake_case(self) -> None:
        # populate_by_name=True 允许直接传 snake_case 名
        meta = AMPResponseMeta(partial_data_fields=["x"])
        assert meta.partial_data_fields == ["x"]

    def test_populate_by_alias_camel_case(self) -> None:
        meta = AMPResponseMeta(**{"partialDataFields": ["y"]})
        assert meta.partial_data_fields == ["y"]


class TestAMPFieldSampleCoverage:
    """AMPFieldSampleCoverage 四字段序列化（设计 §5.5）。"""

    def test_fields_present(self) -> None:
        cov = AMPFieldSampleCoverage(
            available_samples=5,
            total_samples=10,
            coverage_ratio=0.5,
            status="partial",
        )
        assert cov.available_samples == 5
        assert cov.total_samples == 10
        assert cov.coverage_ratio == 0.5
        assert cov.status == "partial"

    def test_camel_alias_serialization(self) -> None:
        cov = AMPFieldSampleCoverage(
            available_samples=10,
            total_samples=10,
            coverage_ratio=1.0,
            status="complete",
        )
        dumped = cov.model_dump(by_alias=True)
        assert dumped["availableSamples"] == 10
        assert dumped["totalSamples"] == 10
        assert dumped["coverageRatio"] == 1.0
        assert dumped["status"] == "complete"

    def test_status_unavailable(self) -> None:
        cov = AMPFieldSampleCoverage(
            available_samples=0,
            total_samples=5,
            coverage_ratio=0.0,
            status="unavailable",
        )
        assert cov.status == "unavailable"

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError):
            AMPFieldSampleCoverage(
                available_samples=0,
                total_samples=5,
                coverage_ratio=0.0,
                status="unknown_status",
            )

    def test_populate_by_name(self) -> None:
        cov = AMPFieldSampleCoverage(
            available_samples=3,
            total_samples=9,
            coverage_ratio=0.333,
            status="partial",
        )
        assert cov.available_samples == 3


class TestAMPResponseMetaRegression:
    """已有 AMPResponseMeta 字段不受新增字段影响（向后兼容）。"""

    def test_all_default_none_fields(self) -> None:
        meta = AMPResponseMeta()
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert dumped == {}

    def test_data_freshness_at_alias(self) -> None:
        meta = AMPResponseMeta(data_freshness_at="2025-06-01T00:00:00Z")
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "dataFreshnessAt" in dumped

    def test_ingestion_lag_ms_alias(self) -> None:
        meta = AMPResponseMeta(ingestion_lag_ms=5000)
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "ingestionLagMs" in dumped

    def test_partial_field(self) -> None:
        meta = AMPResponseMeta(partial=True)
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert dumped.get("partial") is True

    def test_elapsed_ms_alias(self) -> None:
        meta = AMPResponseMeta(elapsed_ms=42)
        dumped = meta.model_dump(by_alias=True, exclude_none=True)
        assert "elapsedMs" in dumped
