"""生成脚本共用的输出工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parent.parent.parent / "test" / "fixtures" / "python"


def write_fixture(pkg: str, name: str, data: Any) -> None:
    target = FIXTURE_ROOT / pkg / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
