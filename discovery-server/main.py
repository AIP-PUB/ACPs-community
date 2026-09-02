"""根兼容启动壳。"""

from __future__ import annotations

import sys

import app.main as discovery_main

app = discovery_main.app
run = discovery_main.run


if __name__ == "__main__":
    sys.stderr.write("[WARN] main.py 已降级为兼容入口，请优先使用 `uv run python -m app.main` 或 `just dev start`。\n")
    run()
