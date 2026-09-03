"""alive-sync e2e sub-directory conftest.

这些测试全部使用 in-process TestClient + monkeypatch，不依赖真实数据库或 Kafka。
覆盖父级 e2e conftest 中所有 autouse 基础设施 fixture，以跳过数据库种子/Runtime 准备。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(scope="session", autouse=True)
def prepare_e2e_seed_data() -> None:
    """覆盖父级 fixture：本目录测试不需要数据库种子数据。"""


@pytest.fixture(scope="session", autouse=True)
def prepare_e2e_base_url(prepare_e2e_seed_data: None) -> Generator[None]:
    """覆盖父级 fixture：本目录测试使用 ASGI TestClient，无需外部服务器。"""
    del prepare_e2e_seed_data
    yield


@pytest.fixture(scope="session", autouse=True)
def prepare_e2e_runtime_rows(prepare_e2e_base_url: None) -> Generator[None]:
    """覆盖父级 fixture：本目录测试不需要 available_agents_runtime 数据。"""
    del prepare_e2e_base_url
    yield
