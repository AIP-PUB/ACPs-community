"""pytest 配置：为 SDK 测试启用 asyncio 模式。"""
import pytest


def pytest_configure(config):
    """注册 asyncio_mode 配置。"""
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")
