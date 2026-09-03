#!/usr/bin/env python3
"""从 baseline-matrix.toml 读取 [os_whitelist].ids 并打印 JSON 数组。

供 host 模式 preflight 在控制节点断言 acps_os_id ∈ ids。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--matrix", required=True, help="path to baseline-matrix.toml")
    args = p.parse_args()
    path = Path(args.matrix)
    if not path.is_file():
        print(f"[ERROR] baseline-matrix not found: {path}", file=sys.stderr)
        return 2
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = data.get("os_whitelist")
    if not isinstance(section, dict):
        print(f"[ERROR] missing [os_whitelist] in {path}", file=sys.stderr)
        return 2
    ids = section.get("ids")
    if not isinstance(ids, list) or not ids:
        print(f"[ERROR] [os_whitelist].ids missing or empty in {path}", file=sys.stderr)
        return 2
    out = [str(x).strip() for x in ids if str(x).strip()]
    if not out:
        print(f"[ERROR] [os_whitelist].ids empty after normalize in {path}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
