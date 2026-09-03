#!/usr/bin/env python3
"""从 release-manifest.toml 查找组件制品映射。

支持两种清单形态（本设计 §5.1 / host 直装设计 2026-07-24）：
  image 清单：仅 [images.*]，字段 tag（可选 file，省略时由 tag 派生归档名）。
  host  清单：[apps.*] / [vendor.*] / [os_packages.*]，逐条带 artifact_kind；
              [apps.*] 字段为 app_release（归档文件名，位于 artifacts/apps/）。

--table 默认 images（向后兼容既有调用方，未显式传参也不变行为）。
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

VALID_TABLES = ("images", "apps", "vendor", "os_packages", "control")


def artifact_filename_from_tag(tag: str) -> str:
    tag = (tag or "").strip()
    if not tag.startswith("acps/"):
        raise ValueError(
            f"tag must be acps/<repo>:<suffix> to derive archive filename, got {tag!r}"
        )
    try:
        repo_part, suffix = tag.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"tag missing ':' : {tag!r}") from exc
    repo = repo_part.split("/", 1)[-1]
    if not repo or not suffix:
        raise ValueError(f"cannot derive filename from tag {tag!r}")
    return f"acps-{repo}-{suffix}.image.tar.gz"


def enrich_meta(table: str, meta: dict) -> dict:
    out = dict(meta)
    if table == "images" and not out.get("file"):
        tag = out.get("tag", "")
        out["file"] = artifact_filename_from_tag(tag)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--component", required=True)
    p.add_argument(
        "--table",
        default="images",
        choices=VALID_TABLES,
        help="release-manifest.toml 中的顶层表名（默认 images，向后兼容）",
    )
    args = p.parse_args()
    data = tomllib.loads(Path(args.manifest).read_text(encoding="utf-8"))
    section = data.get(args.table, {})
    if args.component not in section:
        print(
            f"[ERROR] component {args.component!r} not in [{args.table}] of {args.manifest}",
            file=sys.stderr,
        )
        return 2
    try:
        meta = enrich_meta(args.table, dict(section[args.component]))
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
