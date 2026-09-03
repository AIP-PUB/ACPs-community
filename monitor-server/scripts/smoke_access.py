#!/usr/bin/env python3
"""Access 开发模式冒烟测试（与 e2e_access_verify.py 同实现）。

前置：infra（kafka+redis+clickhouse）、monitor-server 已启动。
运行：
    cd monitor-server && APP_ENV=development uv run python scripts/smoke_access.py

不依赖 demo-leader / demo-partner / Fluent Bit，最短路径验证：
  Kafka amp.access → AccessWriter → ClickHouse → /access/events/query
"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "e2e_access_verify.py"), run_name="__main__")
