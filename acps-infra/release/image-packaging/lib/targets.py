#!/usr/bin/env python3
"""解析与校验镜像打包目标清单 image-targets.toml。

目标清单只声明"要构建哪些镜像目标"：每个 app target 对应一个应用最终发布包
组合 {app, platform, python_tag, variant}；每个 infra target 对应一个基础设施
镜像组合 {id, platform}。目标清单不重复维护 digest——具体基础镜像 digest 统一
由 image_inputs.py 从 image-inputs.lock 解析。

用法：
  python3 targets.py validate --targets <path> --lock <path> [--lock <path> ...]
  python3 targets.py list-app-targets --targets <path> [--app ...] [--platform ...] [--variant ...]
  python3 targets.py list-infra-targets --targets <path> [--id ...] [--platform ...]
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from common import (
    KNOWN_DOCKER_PLATFORMS,
    docker_platform_to_slug,
    is_safe_path_component,
    validate_id,
    validate_version,
)
from image_inputs import load_image_inputs_lock, resolve_infra_base, resolve_python_runtime

VALID_INFRA_KINDS = ("wrapper", "derived")


@dataclass(frozen=True)
class AppTarget:
    app: str
    platform: str
    python_tag: str
    variant: str = ""

    @property
    def platform_slug(self) -> str:
        return docker_platform_to_slug(self.platform)


@dataclass(frozen=True)
class InfraTarget:
    id: str
    kind: str
    upstream_version: str
    acps_version: str
    platform: str

    @property
    def platform_slug(self) -> str:
        return docker_platform_to_slug(self.platform)


def _require_str(table: dict, key: str, errors: list[str], context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{context}: 缺少必填字段 {key!r}")
        return ""
    return value


def _semantic_check_id(label: str, value: str, errors: list[str], context: str) -> None:
    try:
        validate_id(label, value)
    except SystemExit as exc:
        errors.append(f"{context}: {exc}")


def _semantic_check_path_component(label: str, value: str, errors: list[str], context: str) -> None:
    if not is_safe_path_component(value):
        errors.append(f"{context}: {label} 不是安全的单段名称（可能导致文件名/路径穿越）：{value!r}")


def parse_image_targets(path: Path) -> tuple[list[AppTarget], list[InfraTarget], list[str]]:
    """解析 image-targets.toml，返回 (app_targets, infra_targets, errors)。

    errors 非空时，调用方应视为解析失败；仍然返回尽可能多的已解析条目，方便
    校验工具一次性报告多个问题，而不是遇到第一个错误就中止。
    """
    if not path.is_file():
        raise SystemExit(f"[ERROR] 未找到目标清单：{path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[ERROR] 无法解析目标清单（{path}）：{exc}") from exc

    errors: list[str] = []
    app_targets: list[AppTarget] = []
    seen_app_keys: set[tuple[str, str, str, str]] = set()
    for index, entry in enumerate(data.get("app_targets", [])):
        context = f"app_targets[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{context}: 必须是表")
            continue
        app = _require_str(entry, "app", errors, context)
        platform = _require_str(entry, "platform", errors, context)
        python_tag = _require_str(entry, "python_tag", errors, context)
        variant = entry.get("variant", "") or ""
        if not isinstance(variant, str):
            errors.append(f"{context}: variant 必须是字符串")
            variant = ""
        if not app or not platform or not python_tag:
            continue
        _semantic_check_id("app_targets[].app", app, errors, context)
        _semantic_check_path_component("app_targets[].python_tag", python_tag, errors, context)
        if variant:
            _semantic_check_path_component("app_targets[].variant", variant, errors, context)
        if platform not in KNOWN_DOCKER_PLATFORMS:
            errors.append(f"{context}: platform 不在已知 Docker platform 集合内：{platform!r}")
            continue
        key = (app, platform, python_tag, variant)
        if key in seen_app_keys:
            errors.append(f"{context}: 重复的 app target：{key}")
            continue
        seen_app_keys.add(key)
        app_targets.append(AppTarget(app=app, platform=platform, python_tag=python_tag, variant=variant))

    infra_targets: list[InfraTarget] = []
    seen_infra_keys: set[tuple[str, str]] = set()
    for index, entry in enumerate(data.get("infra_targets", [])):
        context = f"infra_targets[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{context}: 必须是表")
            continue
        infra_id = _require_str(entry, "id", errors, context)
        kind = _require_str(entry, "kind", errors, context)
        upstream_version = _require_str(entry, "upstream_version", errors, context)
        acps_version = _require_str(entry, "acps_version", errors, context)
        platform = _require_str(entry, "platform", errors, context)
        if kind and kind not in VALID_INFRA_KINDS:
            errors.append(f"{context}: kind 必须是 {VALID_INFRA_KINDS} 之一，实际为 {kind!r}")
        if not infra_id or not kind or not upstream_version or not acps_version or not platform:
            continue
        _semantic_check_id("infra_targets[].id", infra_id, errors, context)
        _semantic_check_path_component("infra_targets[].upstream_version", upstream_version, errors, context)
        _semantic_check_path_component("infra_targets[].acps_version", acps_version, errors, context)
        if platform not in KNOWN_DOCKER_PLATFORMS:
            errors.append(f"{context}: platform 不在已知 Docker platform 集合内：{platform!r}")
            continue
        key = (infra_id, platform)
        if key in seen_infra_keys:
            errors.append(f"{context}: 重复的 infra target：{key}")
            continue
        seen_infra_keys.add(key)
        infra_targets.append(
            InfraTarget(
                id=infra_id,
                kind=kind,
                upstream_version=upstream_version,
                acps_version=acps_version,
                platform=platform,
            )
        )

    return app_targets, infra_targets, errors


def cross_check_lock(
    app_targets: list[AppTarget],
    infra_targets: list[InfraTarget],
    lock: dict[str, dict[str, str]],
) -> list[str]:
    """确认目标清单里引用的每个 runtime/infra id 都能在 image-inputs.lock 里解析到 digest。"""
    errors: list[str] = []
    for target in app_targets:
        try:
            validate_id("app_targets[].app", target.app)
            resolve_python_runtime(lock, target.platform, target.python_tag)
        except SystemExit as exc:
            errors.append(f"app target {target}: {exc}")
    for target in infra_targets:
        try:
            validate_id("infra_targets[].id", target.id)
            validate_version("infra_targets[].upstream_version", target.upstream_version)
            validate_version("infra_targets[].acps_version", target.acps_version)
            resolve_infra_base(lock, target.id, target.platform)
        except SystemExit as exc:
            errors.append(f"infra target {target}: {exc}")
    return errors


def _filter_app_targets(
    targets: list[AppTarget], app: str | None, platform: str | None, variant: str | None
) -> list[AppTarget]:
    result = targets
    if app:
        result = [t for t in result if t.app == app]
    if platform:
        result = [t for t in result if t.platform == platform]
    if variant is not None:
        result = [t for t in result if t.variant == variant]
    return result


def _filter_infra_targets(targets: list[InfraTarget], infra_id: str | None, platform: str | None) -> list[InfraTarget]:
    result = targets
    if infra_id:
        result = [t for t in result if t.id == infra_id]
    if platform:
        result = [t for t in result if t.platform == platform]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--targets", required=True, help="image-targets.toml 路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_cmd = subparsers.add_parser("validate", help="校验目标清单结构，并与 image-inputs.lock 交叉校验")
    validate_cmd.add_argument("--lock", action="append", default=[], help="image-inputs.lock 路径，可重复传入")

    list_app = subparsers.add_parser("list-app-targets", help="按 JSON Lines 输出匹配的 app targets")
    list_app.add_argument("--app")
    list_app.add_argument("--platform")
    list_app.add_argument("--variant")
    list_app.add_argument("--format", choices=("json", "shell"), default="json")

    list_infra = subparsers.add_parser("list-infra-targets", help="按 JSON Lines 输出匹配的 infra targets")
    list_infra.add_argument("--id", dest="infra_id")
    list_infra.add_argument("--platform")
    list_infra.add_argument("--format", choices=("json", "shell"), default="json")

    args = parser.parse_args()
    targets_path = Path(args.targets)
    app_targets, infra_targets, errors = parse_image_targets(targets_path)

    if args.command == "validate":
        if errors:
            print("[ERROR] 目标清单结构校验失败：", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            raise SystemExit(1)
        lock_paths = [Path(p) for p in args.lock] if args.lock else [targets_path.parent.parent / "image-inputs.lock"]
        lock = load_image_inputs_lock(lock_paths)
        cross_errors = cross_check_lock(app_targets, infra_targets, lock)
        if cross_errors:
            print("[ERROR] 目标清单与 image-inputs.lock 交叉校验失败：", file=sys.stderr)
            for error in cross_errors:
                print(f"  - {error}", file=sys.stderr)
            raise SystemExit(1)
        print(f"[OK] 目标清单校验通过：{len(app_targets)} 个 app targets，{len(infra_targets)} 个 infra targets")
        return

    if errors:
        print("[ERROR] 目标清单结构校验失败，无法列出目标：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    if args.command == "list-app-targets":
        for target in _filter_app_targets(app_targets, args.app, args.platform, args.variant):
            if args.format == "shell":
                print(
                    f"APP={shlex.quote(target.app)} "
                    f"PLATFORM={shlex.quote(target.platform)} "
                    f"PYTHON_TAG={shlex.quote(target.python_tag)} "
                    f"VARIANT={shlex.quote(target.variant)}"
                )
            else:
                print(json.dumps(asdict(target)))
        return

    if args.command == "list-infra-targets":
        for target in _filter_infra_targets(infra_targets, args.infra_id, args.platform):
            if args.format == "shell":
                print(
                    f"INFRA_ID={shlex.quote(target.id)} "
                    f"INFRA_KIND={shlex.quote(target.kind)} "
                    f"UPSTREAM_VERSION={shlex.quote(target.upstream_version)} "
                    f"ACPS_VERSION={shlex.quote(target.acps_version)} "
                    f"PLATFORM={shlex.quote(target.platform)}"
                )
            else:
                print(json.dumps(asdict(target)))


if __name__ == "__main__":
    main()
