#!/usr/bin/env python3
"""生成应用镜像 build context。

从已经过校验、规范化的 app-release/ 目录出发，生成：

  build-context/
    app-release/          # 产物，原样保留（wheelhouse/、app/、build-manifest.toml、checksums.txt）
    runtime-app/           # 从 app-release/app/ 按 [[assets]] kind 策略过滤出的运行期资源，
                           # 含 runtime-package.toml 副本（Dockerfile 整体 COPY 到 /opt/acps/app/）
    app-run.sh             # 按 [[components]] 生成的 app-level dispatcher

不生成 Dockerfile / entrypoint.sh 本身——那是 image-packaging/app/ 下的静态模板，由
上层编排脚本（build-app-image.sh）直接复制进 build context 根目录。

默认资源过滤策略：排除 [[assets]].kind ∈ {"doc", "systemd_unit"}，其余 kind
（含 "other"）默认全部包含；可通过 startup-strategies.toml 的
force_include_assets / force_exclude_assets 按 asset path 精确覆盖。

用法：
  python3 build_context.py generate --app-release-dir <build-context>/app-release \
      --strategy startup-strategies.toml --dest <build-context>
  python3 build_context.py strategy --app registry-server --strategy startup-strategies.toml
"""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common import is_safe_relpath, validate_id

DEFAULT_EXCLUDE_KINDS = {"doc", "systemd_unit"}


@dataclass(frozen=True)
class Component:
    id: str
    type: str
    entrypoint: str
    ports: tuple[int, ...] = ()
    health_check: str = ""


@dataclass(frozen=True)
class StartupStrategy:
    app: str
    import_checks: list[str] = field(default_factory=list)
    gpu_assert_present: list[str] = field(default_factory=list)
    cpu_assert_absent: list[str] = field(default_factory=list)
    variant_pip_extras: dict[str, str] = field(default_factory=dict)
    force_include_assets: set[str] = field(default_factory=set)
    force_exclude_assets: set[str] = field(default_factory=set)


def load_startup_strategy(strategy_path: Path, app: str) -> StartupStrategy:
    if not strategy_path.is_file():
        raise SystemExit(f"[ERROR] 未找到 startup-strategies.toml：{strategy_path}")
    data = tomllib.loads(strategy_path.read_text(encoding="utf-8"))
    apps_table = data.get("apps", {})
    entry = apps_table.get(app)
    if not isinstance(entry, dict):
        raise SystemExit(
            f"[ERROR] startup-strategies.toml 中没有 app={app!r} 的显式声明"
            "（镜像打包不允许从 app id 猜测 import smoke 目标）"
        )
    import_checks = entry.get("import_checks")
    if not isinstance(import_checks, list) or not import_checks or not all(isinstance(m, str) for m in import_checks):
        raise SystemExit(f"[ERROR] startup-strategies.toml 中 apps.{app}.import_checks 必须是非空字符串数组")
    return StartupStrategy(
        app=app,
        import_checks=list(import_checks),
        gpu_assert_present=list(entry.get("gpu_assert_present", []) or []),
        cpu_assert_absent=list(entry.get("cpu_assert_absent", []) or []),
        variant_pip_extras=dict(entry.get("variant_pip_extras", {}) or {}),
        force_include_assets=set(entry.get("force_include_assets", []) or []),
        force_exclude_assets=set(entry.get("force_exclude_assets", []) or []),
    )


def _parse_components(runtime_package: dict) -> list[Component]:
    raw_components = runtime_package.get("components", [])
    if not isinstance(raw_components, list):
        raise SystemExit("[ERROR] runtime-package.toml 的 [[components]] 必须是数组表")

    components: list[Component] = []
    seen_ids: set[str] = set()
    for entry in raw_components:
        if not isinstance(entry, dict):
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[components]] 条目必须是表：{entry!r}")
        comp_id = entry.get("id", "")
        comp_type = entry.get("type", "")
        entrypoint = entry.get("entrypoint", "")
        if not comp_id or not comp_type or not entrypoint:
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[components]] 缺少必填字段：{entry}")
        validate_id("runtime-package.toml 的 [[components]].id", comp_id)
        if comp_id in seen_ids:
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[components]] 存在重复的 id：{comp_id!r}")
        seen_ids.add(comp_id)
        components.append(
            Component(
                id=comp_id,
                type=comp_type,
                entrypoint=entrypoint,
                ports=tuple(entry.get("ports", []) or []),
                health_check=entry.get("health_check", "") or "",
            )
        )
    if not components:
        raise SystemExit("[ERROR] runtime-package.toml 中没有任何 [[components]]")
    return components


def _copy_asset(src: Path, dest: Path, executable: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)
        if executable:
            mode = dest.stat().st_mode
            dest.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def generate_runtime_app(
    app_release_dir: Path,
    runtime_app_dir: Path,
    strategy: StartupStrategy,
) -> tuple[dict, list[Component]]:
    """从 app_release_dir/app/ 生成过滤后的 runtime_app_dir/，返回
    (runtime-package.toml 解析结果, components 列表)。"""
    app_dir = app_release_dir / "app"
    runtime_package_path = app_dir / "runtime-package.toml"
    if not runtime_package_path.is_file():
        raise SystemExit(f"[ERROR] 未找到 app/runtime-package.toml：{runtime_package_path}")
    runtime_package = tomllib.loads(runtime_package_path.read_text(encoding="utf-8"))

    components = _parse_components(runtime_package)

    if runtime_app_dir.exists():
        shutil.rmtree(runtime_app_dir)
    runtime_app_dir.mkdir(parents=True)

    assets = runtime_package.get("assets", [])
    if not isinstance(assets, list):
        raise SystemExit("[ERROR] runtime-package.toml 的 [[assets]] 必须是数组表")
    copied_paths: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[assets]] 条目必须是表：{asset!r}")
        asset_path = asset.get("path", "")
        if not asset_path:
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[assets]] 缺少 path 字段：{asset}")
        if not is_safe_relpath(asset_path):
            raise SystemExit(f"[ERROR] runtime-package.toml 的 [[assets]].path 是非法路径（可能是目录穿越）：{asset_path!r}")

        kind = asset.get("kind", "other") or "other"
        executable = bool(asset.get("executable", False))

        if asset_path in strategy.force_exclude_assets:
            continue
        if asset_path not in strategy.force_include_assets and kind in DEFAULT_EXCLUDE_KINDS:
            continue

        src = app_dir / asset_path
        if not src.exists():
            raise SystemExit(f"[ERROR] runtime-package.toml 声明的资源在 app-release 中不存在：{asset_path}")
        _copy_asset(src, runtime_app_dir / asset_path, executable)
        copied_paths.append(asset_path)

    # runtime-package.toml 本身始终保留在镜像内，供运行期读取组件/端口/健康检查语义
    # 它不是 [[assets]] 条目，需要单独复制。
    shutil.copy2(runtime_package_path, runtime_app_dir / "runtime-package.toml")

    return runtime_package, components


def _shell_single_quote(value: str) -> str:
    """POSIX shell 单引号安全转义：把 "'" 替换成 "'\\''"。"""
    return "'" + value.replace("'", "'\\''") + "'"


def _shell_case_literal(value: str) -> str:
    """把 value 转成安全的 case 模式标签：用双引号包裹后，POSIX shell 会对被引用的
    pattern 字符禁用其 glob 特殊含义（`*`、`?`、`[...]` 不再被当成通配符），
    避免 component id 里偶然出现的 glob 元字符让 case 分支匹配到非预期的
    $ACPS_RUN_ENTRY 输入。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def generate_dispatcher(components: list[Component]) -> str:
    lines = [
        "#!/bin/sh",
        "# 由 image-packaging/lib/build_context.py 自动生成。",
        "# 不要手工编辑：内容源自应用最终发布包 app/runtime-package.toml 的 [[components]]。",
        "set -e",
        "",
        'run_entry="${ACPS_RUN_ENTRY:-}"',
        "",
    ]

    if len(components) == 1:
        only = components[0]
        lines.append(f'if [ -n "$run_entry" ] && [ "$run_entry" != {_shell_single_quote(only.id)} ]; then')
        lines.append(
            f'    echo "[ERROR] ACPS_RUN_ENTRY=\'$run_entry\' 不是有效入口；本镜像只有一个入口：{only.id}" >&2'
        )
        lines.append("    exit 2")
        lines.append("fi")
        lines.append(f"exec sh -c {_shell_single_quote(only.entrypoint)}")
        lines.append("")
        return "\n".join(lines)

    valid_ids = " ".join(c.id for c in components)
    lines.append('if [ -z "$run_entry" ]; then')
    lines.append('    echo "[ERROR] 本镜像声明了多个入口，必须显式设置 ACPS_RUN_ENTRY 环境变量选择其中一个：" >&2')
    for comp in components:
        lines.append(f'    echo "  {comp.id}" >&2')
    lines.append("    exit 2")
    lines.append("fi")
    lines.append("")
    lines.append('case "$run_entry" in')
    for comp in components:
        lines.append(f"    {_shell_case_literal(comp.id)})")
        lines.append(f"        exec sh -c {_shell_single_quote(comp.entrypoint)}")
        lines.append("        ;;")
    lines.append("    *)")
    lines.append(f'        echo "[ERROR] 未知的 ACPS_RUN_ENTRY=\'$run_entry\'；有效入口：{valid_ids}" >&2')
    lines.append("        exit 2")
    lines.append("        ;;")
    lines.append("esac")
    lines.append("")
    return "\n".join(lines)


def generate_build_context(app_release_dir: Path, strategy_path: Path, dest_dir: Path, app: str) -> list[Component]:
    strategy = load_startup_strategy(strategy_path, app)
    runtime_app_dir = dest_dir / "runtime-app"
    _runtime_package, components = generate_runtime_app(app_release_dir, runtime_app_dir, strategy)

    app_run_path = dest_dir / "app-run.sh"
    app_run_path.write_text(generate_dispatcher(components), encoding="utf-8")
    mode = app_run_path.stat().st_mode
    app_run_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return components


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_cmd = subparsers.add_parser("generate", help="生成 runtime-app/ 与 app-run.sh")
    generate_cmd.add_argument("--app", required=True)
    generate_cmd.add_argument("--app-release-dir", required=True, help=" 产物：<build-context>/app-release")
    generate_cmd.add_argument("--strategy", required=True, help="startup-strategies.toml 路径")
    generate_cmd.add_argument("--dest", required=True, help="build context 根目录")

    strategy_cmd = subparsers.add_parser("strategy", help="以 JSON 打印指定 app 的 startup strategy，供 shell 编排脚本消费")
    strategy_cmd.add_argument("--app", required=True)
    strategy_cmd.add_argument("--strategy", required=True)

    args = parser.parse_args()
    if args.command == "generate":
        components = generate_build_context(
            Path(args.app_release_dir), Path(args.strategy), Path(args.dest), args.app
        )
        print(f"[OK] 已生成 build context：{args.dest}")
        for comp in components:
            print(f"  component id={comp.id} type={comp.type} entrypoint={comp.entrypoint!r}")
        return

    if args.command == "strategy":
        strategy = load_startup_strategy(Path(args.strategy), args.app)
        payload = asdict(strategy)
        payload["force_include_assets"] = sorted(strategy.force_include_assets)
        payload["force_exclude_assets"] = sorted(strategy.force_exclude_assets)
        print(json.dumps(payload))


if __name__ == "__main__":
    main()
