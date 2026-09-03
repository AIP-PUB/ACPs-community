"""tests/unit/test_access_runtime_config.py — 跨键配置校验测试（C-2）。

TDD C-2：先写测试（红）→ 实现 runtime.validate_access_config（绿）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


def _make_settings_mock(**overrides: Any) -> Any:
    """Mock Settings 对象，提供默认合法值 + overrides 覆写。"""
    defaults = {
        "access_insert_batch_interval_seconds": 5,
        "access_insert_batch_max_rows": 1000,
        "access_raw_retention_days": 30,
        "access_archive_retention_days": 90,
        "access_topology_retention_days": 90,
        "access_lagging_threshold_ms": 300_000,
        "access_query_timeout_seconds": 30,
        "access_trace_max_spans": 500,
        "access_slow_top_max_n": 100,
        "access_error_attribution_max_n": 50,
        "access_error_status_threshold": 500,
        "access_dedup_window_hours": 24,
        "access_trace_max_duration_hours": 2,
        "access_lagging_response_mode": "503",
    }
    defaults.update(overrides)
    mock = type("MockSettings", (), defaults)()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


class TestValidateAccessConfig:
    """validate_access_config 跨键约束测试。"""

    def _validate(self, **overrides: Any) -> None:
        from app.access.runtime import validate_access_config

        mock_settings = _make_settings_mock(**overrides)
        with patch("app.access.runtime.settings", mock_settings):
            validate_access_config()

    def test_valid_defaults_pass(self) -> None:
        self._validate()

    def test_archive_less_than_raw_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(
                access_raw_retention_days=30,
                access_archive_retention_days=20,  # < raw_retention
            )

    def test_topology_less_than_raw_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(
                access_raw_retention_days=30,
                access_topology_retention_days=10,  # < raw_retention
            )

    def test_archive_equal_raw_passes(self) -> None:
        self._validate(
            access_raw_retention_days=30,
            access_archive_retention_days=30,
        )

    def test_topology_equal_raw_passes(self) -> None:
        self._validate(
            access_raw_retention_days=30,
            access_topology_retention_days=30,
        )

    def test_invalid_lagging_response_mode_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_lagging_response_mode="invalid")

    def test_partial_mode_passes(self) -> None:
        self._validate(access_lagging_response_mode="partial")

    def test_zero_insert_batch_interval_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_insert_batch_interval_seconds=0)

    def test_negative_insert_batch_max_rows_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_insert_batch_max_rows=-1)

    def test_zero_trace_max_spans_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_trace_max_spans=0)

    def test_dedup_window_less_than_one_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_dedup_window_hours=0)

    def test_trace_max_duration_less_than_one_raises(self) -> None:
        from app.access.exception import AccessConfigError

        with pytest.raises(AccessConfigError):
            self._validate(access_trace_max_duration_hours=0)
