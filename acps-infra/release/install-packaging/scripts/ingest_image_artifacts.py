#!/usr/bin/env python3
"""已弃用：短名 ingest 已移除（2026-07-23）。

请改用 build-install-package.sh。本模块仅保留以免旧 import/文档破坏发现；
勿在产品路径调用。
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "[ERROR] ingest_image_artifacts.py is deprecated; "
        "use scripts/build-install-package.sh",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
