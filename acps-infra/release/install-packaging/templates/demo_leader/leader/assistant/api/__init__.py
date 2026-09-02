"""
Leader Agent Platform - API Layer

本模块提供 Leader 的 HTTP API 端点实现。

模块结构：
- schemas: API 请求/响应的 Pydantic 模型
- routes: FastAPI 路由定义
"""

from .routes import init_routes, router

__all__ = [
    "init_routes",
    "router",
]
