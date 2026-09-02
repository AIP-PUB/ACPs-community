#!/usr/bin/env python3
"""解析与校验 image-inputs.lock。

image-inputs.lock 只回答"某个 runtime/infra 镜像 id 现在对应哪个 digest"，本身不
决定这次构建要不要产出某个镜像；构建目标由 image-targets.toml（见 targets.py）决定。

用法：
  python3 image_inputs.py validate --lock <path> [--lock <path> ...]
  python3 image_inputs.py resolve --lock <path> --kind python_runtime \
      --platform linux/amd64 --python-tag cp314
  python3 image_inputs.py resolve --lock <path> --kind infra_base \
      --infra-id redis --platform linux/amd64
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

from common import KNOWN_DOCKER_PLATFORMS

# 值必须形如 "repo/name:tag@sha256:<64位hex>"，防止把本地 daemon image id
# （如裸 "acps-audit:xxx" 不带 digest）误当成可跨机器复现的正式输入。
_DIGEST_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

VALID_TABLES = ("python_runtime", "infra_base")


def load_image_inputs_lock(paths: list[Path]) -> dict[str, dict[str, str]]:
    """按顺序加载并合并多个 lock 文件；后面的路径覆盖前面同名 key（与
    assembly/assemble-and-validate.sh 的 IMAGES_LOCK_PATHS 叠加惯例一致）。"""
    merged: dict[str, dict[str, str]] = {table: {} for table in VALID_TABLES}
    found_any = False
    for path in paths:
        if not path.is_file():
            continue
        found_any = True
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"[ERROR] 无法解析 image-inputs.lock（{path}）：{exc}") from exc
        for table in VALID_TABLES:
            section = data.get(table, {})
            if not isinstance(section, dict):
                raise SystemExit(f"[ERROR] {path} 中的 [{table}] 必须是表")
            merged[table].update(section)
    if not found_any:
        joined = ", ".join(str(p) for p in paths)
        raise SystemExit(f"[ERROR] 未找到任何 image-inputs.lock 文件（查找路径：{joined}）")
    return merged


def validate_lock(lock: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    for table in VALID_TABLES:
        section = lock.get(table, {})
        for key, value in section.items():
            if not isinstance(value, str) or not _DIGEST_PATTERN.match(value):
                errors.append(
                    f"[{table}] key={key!r} 的值不是 'repo:tag@sha256:<64位hex>' 形式的可复现 registry digest：{value!r}"
                )
            if table == "python_runtime":
                parts = key.split(",")
                if len(parts) != 2:
                    errors.append(f"[{table}] key={key!r} 应为 'docker_platform,python_tag' 形式")
                elif parts[0] not in KNOWN_DOCKER_PLATFORMS:
                    errors.append(f"[{table}] key={key!r} 的 docker_platform 段不在已知平台集合内：{parts[0]!r}")
            if table == "infra_base":
                parts = key.split(",")
                if len(parts) != 2:
                    errors.append(f"[{table}] key={key!r} 应为 'infra_id,docker_platform' 形式")
                elif parts[1] not in KNOWN_DOCKER_PLATFORMS:
                    errors.append(f"[{table}] key={key!r} 的 docker_platform 段不在已知平台集合内：{parts[1]!r}")
    return errors


def resolve_python_runtime(lock: dict[str, dict[str, str]], platform: str, python_tag: str) -> str:
    key = f"{platform},{python_tag}"
    value = lock.get("python_runtime", {}).get(key)
    if not value:
        raise SystemExit(f"[ERROR] image-inputs.lock 的 [python_runtime] 中找不到 key={key!r}")
    return value


def resolve_infra_base(lock: dict[str, dict[str, str]], infra_id: str, platform: str) -> str:
    key = f"{infra_id},{platform}"
    value = lock.get("infra_base", {}).get(key)
    if not value:
        raise SystemExit(f"[ERROR] image-inputs.lock 的 [infra_base] 中找不到 key={key!r}")
    return value


def _default_lock_paths() -> list[Path]:
    return [Path(__file__).resolve().parent.parent.parent / "image-inputs.lock"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", action="append", default=[], help="image-inputs.lock 路径，可重复传入并按顺序叠加覆盖")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="校验 image-inputs.lock 结构")

    resolve = subparsers.add_parser("resolve", help="解析单个 digest")
    resolve.add_argument("--kind", choices=VALID_TABLES, required=True)
    resolve.add_argument("--platform", required=True)
    resolve.add_argument("--python-tag", help="kind=python_runtime 时必填")
    resolve.add_argument("--infra-id", help="kind=infra_base 时必填")

    args = parser.parse_args()
    lock_paths = [Path(p) for p in args.lock] if args.lock else _default_lock_paths()
    lock = load_image_inputs_lock(lock_paths)

    if args.command == "validate":
        errors = validate_lock(lock)
        if errors:
            print("[ERROR] image-inputs.lock 校验失败：", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            raise SystemExit(1)
        print("[OK] image-inputs.lock 校验通过")
        return

    if args.command == "resolve":
        if args.kind == "python_runtime":
            if not args.python_tag:
                raise SystemExit("[ERROR] --kind python_runtime 需要 --python-tag")
            print(resolve_python_runtime(lock, args.platform, args.python_tag))
        else:
            if not args.infra_id:
                raise SystemExit("[ERROR] --kind infra_base 需要 --infra-id")
            print(resolve_infra_base(lock, args.infra_id, args.platform))


if __name__ == "__main__":
    main()
