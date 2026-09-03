#!/usr/bin/env python3
"""共享 runtime-package.toml 生成与校验工具。

供各应用项目的 package builder（package-build-lib.sh / package-wheel-runtime.sh）在生成
应用薄包时调用；也供 acps-infra 在采集、装配、最终包校验阶段复用同一份 schema 实现，
避免 shell 和 Python 出现两套隐式契约并逐渐漂移。

背景设计：应用最终发布包设计中的 runtime-package.toml 与运行期资源清单章节。
实现结果：应用薄包生成与校验已在当前发布包实现中落地。

用法：
  python3 runtime_package.py generate [options] --output <path>
  python3 runtime_package.py validate --path <path> [--asset-root <dir>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 运行环境应保证 Python 3.11+
    print("[ERROR] 需要 Python 3.11+ 提供的 tomllib 模块", file=sys.stderr)
    raise

# 新 schema 明确禁止的旧字段（源设计 §4.3：删除 target.*、minimum_host_baseline、wheelhouse）。
FORBIDDEN_KEYS = {"target", "minimum_host_baseline", "wheelhouse"}


def _is_safe_relpath(relpath: str) -> bool:
    """拒绝绝对路径和任何包含 '..' 路径段的相对路径。

    validate 子命令校验的 runtime-package.toml 可能来自外部/被篡改的包，
    [[assets]].path / required_children 拼接 asset_root 之前必须先拒绝目录穿越（CWE-22）。
    """
    if not relpath:
        return False
    candidate = Path(relpath)
    if candidate.is_absolute():
        return False
    return ".." not in candidate.parts


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string(value: str) -> str:
    return f'"{_toml_escape(value)}"'


def _toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def _parse_component_spec(spec: str) -> str:
    """将 "id|type|entrypoint|ports|health_check|smoke_test|config_templates" 渲染为 [[components]] 块。

    - id / type / entrypoint 必填。
    - ports、config_templates 允许用英文逗号分隔多个值。
    - health_check、smoke_test 可留空。
    """
    fields = spec.split("|")
    if len(fields) < 3:
        raise SystemExit(f"[ERROR] --component 格式非法（至少需要 id|type|entrypoint）：{spec}")
    fields = fields + [""] * (7 - len(fields))
    comp_id, comp_type, entrypoint, ports, health_check, smoke_test, config_templates = fields[:7]

    if not comp_id.strip() or not comp_type.strip() or not entrypoint.strip():
        raise SystemExit(f"[ERROR] --component 缺少必填字段（id/type/entrypoint）：{spec}")

    lines = ["[[components]]"]
    lines.append(f"id = {_toml_string(comp_id.strip())}")
    lines.append(f"type = {_toml_string(comp_type.strip())}")
    lines.append(f"entrypoint = {_toml_string(entrypoint.strip())}")
    if config_templates.strip():
        items = [item.strip() for item in config_templates.split(",") if item.strip()]
        lines.append(f"config_templates = {_toml_string_array(items)}")
    if ports.strip():
        items = [item.strip() for item in ports.split(",") if item.strip()]
        for item in items:
            if not item.isdigit():
                raise SystemExit(f"[ERROR] --component 的 ports 必须是整数：{spec}")
        lines.append("ports = [" + ", ".join(items) + "]")
    if health_check.strip():
        lines.append(f"health_check = {_toml_string(health_check.strip())}")
    if smoke_test.strip():
        lines.append(f"smoke_test = {_toml_string(smoke_test.strip())}")
    return "\n".join(lines)


def _parse_bundle_entry(entry: str) -> tuple[str, str, str]:
    parts = entry.split("|")
    if len(parts) == 2:
        src, dest = parts
        kind = "other"
    elif len(parts) == 3:
        src, dest, kind = parts
    else:
        raise SystemExit(f"[ERROR] --bundle-map 格式非法（需要 src|dest 或 src|dest|kind）：{entry}")
    if not src or not dest:
        raise SystemExit(f"[ERROR] --bundle-map 的 src/dest 不能为空：{entry}")
    return src, dest, (kind or "other")


def _render_assets(
    asset_root: Path,
    bundle_entries: list[str],
    required_paths: set[str],
    chmod_paths: set[str],
) -> str:
    """按 §4.4 的规则，从既有 BUNDLE_MAP/REQUIRED_PATHS/CHMOD_PATHS 机械生成 [[assets]]。

    调用方必须保证 asset_root 已经是拷贝、post-stage 清理之后的最终 staging 目录，
    这样生成的 assets 才反映"这次应用薄包最终产出了什么"，而不是构建前的源码状态。
    """
    blocks = []
    for entry in bundle_entries:
        src, dest, kind = _parse_bundle_entry(entry)
        abs_dest = asset_root / dest
        if not abs_dest.exists():
            raise SystemExit(
                "[ERROR] bundle map 声明的目标路径在 staging 中不存在，无法生成 asset："
                f"{dest}（来自 bundle map 条目 {entry}）"
            )

        required_children: list[str] = []
        if abs_dest.is_dir():
            # 目录 asset 有两种"必需"来源：REQUIRED_PATHS 直接列出整个目录（视为整体必需，
            # 不生成 required_children），或者只列出目录下若干具体文件（按前缀匹配，填入
            # required_children，目录内其余文件仍然可选）。
            required = src in required_paths
            prefix = src.rstrip("/") + "/"
            for req in sorted(required_paths):
                if req.startswith(prefix):
                    required = True
                    required_children.append(req[len(prefix) :])
        else:
            required = src in required_paths

        lines = ["[[assets]]", f"path = {_toml_string(dest)}", f"kind = {_toml_string(kind)}"]
        lines.append(f"required = {'true' if required else 'false'}")
        if required_children:
            lines.append(f"required_children = {_toml_string_array(sorted(required_children))}")
        if dest in chmod_paths:
            lines.append("executable = true")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def cmd_generate(args: argparse.Namespace) -> None:
    asset_root = Path(args.asset_root)
    if not asset_root.is_dir():
        raise SystemExit(f"[ERROR] --asset-root 不存在或不是目录：{asset_root}")

    sections: list[str] = []

    sections.append(
        "\n".join(
            [
                "[package]",
                f"name = {_toml_string(args.name)}",
                f"version = {_toml_string(args.version)}",
                f"build_id = {_toml_string(args.build_id)}",
            ]
        )
    )

    artifact_lines = ["[artifacts]", f"dist = {_toml_string(args.dist_dir)}"]
    if args.variant_lockfile:
        if args.lockfile:
            raise SystemExit("[ERROR] --lockfile 与 --variant-lockfile 不能同时声明")
        artifact_lines.append(f"checksums = {_toml_string(args.checksums)}")
        artifact_lines.append("")
        artifact_lines.append("[artifacts.variant_lockfiles]")
        for item in args.variant_lockfile:
            if "=" not in item:
                raise SystemExit(f"[ERROR] --variant-lockfile 需要 key=value 格式：{item}")
            key, value = item.split("=", 1)
            key = key.strip()
            if not key:
                raise SystemExit(f"[ERROR] --variant-lockfile 的 key 不能为空：{item}")
            artifact_lines.append(f"{key} = {_toml_string(value.strip())}")
    else:
        if not args.lockfile:
            raise SystemExit("[ERROR] 未声明 --variant-lockfile 时必须提供 --lockfile")
        artifact_lines.append(f"lockfile = {_toml_string(args.lockfile)}")
        artifact_lines.append(f"checksums = {_toml_string(args.checksums)}")
    sections.append("\n".join(artifact_lines))

    if not args.component:
        raise SystemExit("[ERROR] 至少需要一个 --component 声明")
    for component_spec in args.component:
        sections.append(_parse_component_spec(component_spec))

    dependency_lines = ["[dependencies]"]
    dependency_lines.append(f"internal_wheels = {_toml_string_array(args.internal_wheel)}")
    if args.external_component:
        dependency_lines.append(f"external_components = {_toml_string_array(args.external_component)}")
    sections.append("\n".join(dependency_lines))

    if args.bundle_map:
        required_paths = set(args.required_path)
        chmod_paths = set(args.chmod_path)
        assets_toml = _render_assets(asset_root, args.bundle_map, required_paths, chmod_paths)
        if assets_toml:
            sections.append(assets_toml)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"[OK] 已生成 {output_path}")


def _collect_forbidden_keys(data: object, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_KEYS:
                found.append(current_path)
            found.extend(_collect_forbidden_keys(value, current_path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(_collect_forbidden_keys(item, f"{path}[{index}]"))
    return found


def cmd_validate(args: argparse.Namespace) -> None:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"[ERROR] 未找到 runtime-package.toml：{path}")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []

    forbidden = _collect_forbidden_keys(data)
    if forbidden:
        errors.append("包含新 schema 禁止的旧字段：" + ", ".join(sorted(forbidden)))

    for required_table in ("package", "artifacts", "dependencies"):
        if required_table not in data:
            errors.append(f"缺少必需的 [{required_table}] 表")

    package_table = data.get("package", {}) if isinstance(data.get("package"), dict) else {}
    if not package_table.get("name"):
        errors.append("[package].name 不能为空")
    if not package_table.get("version"):
        errors.append("[package].version 不能为空")

    artifacts = data.get("artifacts", {}) if isinstance(data.get("artifacts"), dict) else {}
    has_lockfile = bool(artifacts.get("lockfile"))
    has_variant_lockfiles = bool(artifacts.get("variant_lockfiles"))
    if has_lockfile == has_variant_lockfiles:
        errors.append("[artifacts] 必须恰好声明 lockfile 或 variant_lockfiles 之一，不能同时声明或都不声明")

    # lockfile / variant_lockfiles 的值同样来自可能被篡改/损坏的文件，装配阶段
    # （assemble.sh）会直接拿它们拼接文件路径去读取——必须和 [[assets]].path 一样做
    # 路径安全校验，并确认文件在 asset_root 下真实存在，否则"校验通过"并不能真正
    # 保证装配阶段能找到、且只能在预期目录内找到这份 lockfile。
    asset_root_for_lockfile = Path(args.asset_root) if args.asset_root else path.parent
    if has_lockfile:
        lockfile_value = artifacts.get("lockfile")
        if not isinstance(lockfile_value, str) or not _is_safe_relpath(lockfile_value):
            errors.append(f"[artifacts].lockfile 是非法路径（可能是目录穿越）：{lockfile_value!r}")
        elif not (asset_root_for_lockfile / lockfile_value).is_file():
            errors.append(f"[artifacts].lockfile 声明的文件不存在：{lockfile_value}（相对 {asset_root_for_lockfile}）")
    if has_variant_lockfiles:
        variant_lockfiles = artifacts.get("variant_lockfiles")
        if not isinstance(variant_lockfiles, dict):
            errors.append("[artifacts].variant_lockfiles 必须是表（key = variant，value = 文件名）")
        else:
            for variant_key, lockfile_value in variant_lockfiles.items():
                if not isinstance(lockfile_value, str) or not _is_safe_relpath(lockfile_value):
                    errors.append(
                        f"[artifacts].variant_lockfiles.{variant_key} 是非法路径（可能是目录穿越）：{lockfile_value!r}"
                    )
                elif not (asset_root_for_lockfile / lockfile_value).is_file():
                    errors.append(
                        f"[artifacts].variant_lockfiles.{variant_key} 声明的文件不存在："
                        f"{lockfile_value}（相对 {asset_root_for_lockfile}）"
                    )

    components = data.get("components", [])
    if not isinstance(components, list):
        errors.append("[[components]] 必须是数组表（`[[components]]`），不能是单个表或其他类型")
        components = []
    if not components:
        errors.append("至少需要一个 [[components]] 声明")
    for component in components:
        if not isinstance(component, dict):
            errors.append(f"[[components]] 的条目必须是表：{component!r}")
            continue
        for required_field in ("id", "type", "entrypoint"):
            if not component.get(required_field):
                errors.append(f"[[components]] 缺少必需字段 {required_field}：{component}")

    dependencies = data.get("dependencies", {}) if isinstance(data.get("dependencies"), dict) else {}
    if "internal_wheels" not in dependencies:
        errors.append("[dependencies] 缺少 internal_wheels（即便为空也需要声明为空数组）")

    asset_root = Path(args.asset_root) if args.asset_root else path.parent
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        errors.append("[[assets]] 必须是数组表（`[[assets]]`），不能是单个表或其他类型")
        assets = []
    for asset in assets:
        if not isinstance(asset, dict):
            errors.append(f"[[assets]] 的条目必须是表：{asset!r}")
            continue
        asset_path = asset.get("path")
        if not asset_path:
            errors.append(f"[[assets]] 缺少 path 字段：{asset}")
            continue
        if not _is_safe_relpath(asset_path):
            errors.append(f"[[assets]] 的 path 是非法路径（可能是目录穿越）：{asset_path!r}")
            continue
        abs_path = asset_root / asset_path
        if not abs_path.exists():
            errors.append(f"[[assets]] 声明的路径不存在：{asset_path}（相对 {asset_root}）")
            continue
        for child in asset.get("required_children", []) or []:
            if not _is_safe_relpath(child):
                errors.append(f"[[assets]] required_children 是非法路径（可能是目录穿越）：{child!r}")
                continue
            if not (abs_path / child).exists():
                errors.append(f"[[assets]] required_children 缺失：{asset_path}/{child}")

    if errors:
        print("[ERROR] runtime-package.toml 校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    print(f"[OK] runtime-package.toml 校验通过：{path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="生成新 schema 的 runtime-package.toml")
    generate.add_argument("--name", required=True, help="项目 id，例如 registry-server")
    generate.add_argument("--version", required=True)
    generate.add_argument("--build-id", required=True)
    generate.add_argument("--dist-dir", default="dist/")
    generate.add_argument("--lockfile", default="", help="单一 lockfile 文件名（与 --variant-lockfile 互斥）")
    generate.add_argument(
        "--variant-lockfile",
        action="append",
        default=[],
        help="variant=文件名，可重复传入（与 --lockfile 互斥）",
    )
    generate.add_argument("--checksums", default="checksums.txt")
    generate.add_argument(
        "--component",
        action="append",
        default=[],
        help="id|type|entrypoint|ports|health_check|smoke_test|config_templates，可重复传入",
    )
    generate.add_argument("--internal-wheel", action="append", default=[])
    generate.add_argument("--external-component", action="append", default=[])
    generate.add_argument("--asset-root", required=True, help="已完成拷贝/清理的 staging 目录")
    generate.add_argument("--bundle-map", action="append", default=[], help="src|dest 或 src|dest|kind")
    generate.add_argument("--required-path", action="append", default=[])
    generate.add_argument("--chmod-path", action="append", default=[])
    generate.add_argument("--output", required=True)
    generate.set_defaults(func=cmd_generate)

    validate = subparsers.add_parser("validate", help="校验 runtime-package.toml 是否符合新 schema")
    validate.add_argument("--path", required=True)
    validate.add_argument("--asset-root", default="", help="assets 校验的根目录，默认取 toml 所在目录")
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
