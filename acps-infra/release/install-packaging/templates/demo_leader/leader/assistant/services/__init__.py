"""
Leader Agent Platform - Services Layer

本模块提供外部服务集成。

模块结构：
- scenario_loader: 场景配置加载器
- discovery_client: ADP 发现服务客户端
"""

from .discovery_client import (
    DiscoveryClient,
    DiscoveryClientError,
    get_discovery_client,
)
from .scenario_loader import ScenarioLoader, get_scenario_loader

__all__ = [
    "DiscoveryClient",
    "DiscoveryClientError",
    "ScenarioLoader",
    "get_discovery_client",
    "get_scenario_loader",
]
