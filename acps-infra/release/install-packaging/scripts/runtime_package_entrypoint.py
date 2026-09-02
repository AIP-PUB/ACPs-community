#!/usr/bin/env python3
"""解析 app-release runtime-package.toml 中指定组件 id 的 entrypoint。

host 模式 systemd ExecStart 契约（本设计 §5.2 / 实施计划 Step 8）：唯一权威来源是
[[components]].entrypoint；本脚本只做精确匹配查找，禁止按 component id 猜测模块名
（如 `python -m <id>`）。在目标主机上以该主机的 pinned python 运行（需 py3.11+ 的
tomllib，或已安装 tomli 后备）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True, help="runtime-package.toml 绝对路径")
    p.add_argument("--component", required=True, help="[[components]].id")
    args = p.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"[ERROR] runtime-package.toml not found: {path}", file=sys.stderr)
        return 2

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    components = data.get("components", [])
    if not isinstance(components, list):
        print(f"[ERROR] {path}: [[components]] must be an array table", file=sys.stderr)
        return 2

    ids: list[str] = []
    for entry in components:
        if not isinstance(entry, dict):
            continue
        comp_id = entry.get("id", "")
        ids.append(str(comp_id))
        if comp_id != args.component:
            continue
        entrypoint = str(entry.get("entrypoint", "") or "")
        if not entrypoint:
            print(
                f"[ERROR] component {args.component!r} in {path} has an empty entrypoint",
                file=sys.stderr,
            )
            return 2
        print(entrypoint)
        return 0

    print(
        f"[ERROR] component {args.component!r} not found in {path} [[components]] "
        f"(available: {', '.join(sorted(i for i in ids if i)) or '<none>'})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
