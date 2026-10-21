"""用 Python 版 acps-sdk 生成跨语言对照样例。

运行：npm run fixtures:python
依次执行 scripts/fixtures/gen_*.py，每个脚本把 JSON 写到 test/fixtures/python/<pkg>/。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    scripts = sorted((ROOT / "scripts" / "fixtures").glob("gen_*.py"))
    if not scripts:
        print("no fixture generators found", file=sys.stderr)
        return 1
    for script in scripts:
        print(f"[fixtures] {script.name}")
        runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
