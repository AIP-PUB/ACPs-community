#!/usr/bin/env python3
"""规划 / 核对镜像包产物（image-mode 镜像打包 UX）。

复用 targets.py / common.py / image_inputs.py 的校验与命名规则，不重复实现。

用法：
  python3 plan_image_packages.py \\
    --app-release-dir <dir> [--output <dir>] \\
    --targets <path> --lock <path> --platform <linux/amd64|linux/arm64>
"""

from __future__ import annotations

import argparse
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from common import app_image_filename, infra_image_filename
from image_inputs import load_image_inputs_lock
from targets import AppTarget, InfraTarget, cross_check_lock, parse_image_targets

# {app}-{os}-{arch}-{python_tag}[-{variant}]-app-release-{version}.tar.gz
_APP_RELEASE_NAME_RE = re.compile(
    r"^(?P<app>.+)"
    r"-(?P<platform_slug>(?:linux|darwin)-(?:amd64|arm64))"
    r"-(?P<python_tag>cp\d+)"
    r"(?:-(?P<variant>[a-z0-9]+))?"
    r"-app-release-(?P<version>.+)\.tar\.gz$"
)
_VERSION_FROM_RELEASE_RE = re.compile(r"-app-release-(.+)\.tar\.gz$")


@dataclass(frozen=True)
class ParsedAppRelease:
    basename: str
    app: str
    platform_slug: str
    python_tag: str
    variant: str
    version: str
    is_darwin: bool


@dataclass(frozen=True)
class ExpectedAppImage:
    target: AppTarget
    input_status: str  # OK | MISSING_INPUT | AMBIGUOUS_INPUT
    release_basename: str
    image_filename: str
    note: str = ""


@dataclass(frozen=True)
class ExpectedInfraImage:
    target: InfraTarget
    image_filename: str


def detect_host_arch() -> str:
    uname_m = platform.machine()
    if uname_m == "x86_64":
        return "amd64"
    if uname_m in ("arm64", "aarch64"):
        return "arm64"
    raise SystemExit(f"[ERROR] 不支持的构建机 arch：{uname_m}（仅支持 amd64 / arm64）")


def filter_app_targets(targets: list[AppTarget], platform_filter: str) -> list[AppTarget]:
    return [t for t in targets if t.platform == platform_filter]


def filter_infra_targets(targets: list[InfraTarget], platform_filter: str) -> list[InfraTarget]:
    return [t for t in targets if t.platform == platform_filter]


def parse_app_release_basename(name: str) -> ParsedAppRelease | None:
    match = _APP_RELEASE_NAME_RE.match(name)
    if not match:
        return None
    platform_slug = match.group("platform_slug")
    return ParsedAppRelease(
        basename=name,
        app=match.group("app"),
        platform_slug=platform_slug,
        python_tag=match.group("python_tag"),
        variant=match.group("variant") or "",
        version=match.group("version"),
        is_darwin=platform_slug.startswith("darwin-"),
    )


def scan_app_release_dir(app_release_dir: Path) -> list[Path]:
    if not app_release_dir.is_dir():
        raise SystemExit(f"[ERROR] --app-release-dir 不存在或不是目录：{app_release_dir}")
    return sorted(app_release_dir.glob("*-app-release-*.tar.gz"))


def app_release_glob_pattern(target: AppTarget) -> str:
    slug = target.platform_slug
    if target.variant:
        return f"{target.app}-{slug}-{target.python_tag}-{target.variant}-app-release-*.tar.gz"
    return f"{target.app}-{slug}-{target.python_tag}-app-release-*.tar.gz"


def find_unique_app_release(app_release_dir: Path, target: AppTarget) -> tuple[str, Path | None, str]:
    """返回 (status, path_or_None, note)。status: OK | MISSING_INPUT | AMBIGUOUS_INPUT。"""
    pattern = app_release_glob_pattern(target)
    matches = sorted(p for p in app_release_dir.glob(pattern) if p.is_file())
    if not matches:
        return "MISSING_INPUT", None, f"未找到匹配：{pattern}"
    if len(matches) > 1:
        listed = ", ".join(p.name for p in matches)
        return "AMBIGUOUS_INPUT", None, f"匹配到多个：{listed}"
    return "OK", matches[0], ""


def version_from_app_release_path(path: Path) -> str:
    match = _VERSION_FROM_RELEASE_RE.search(path.name)
    if not match:
        raise SystemExit(f"[ERROR] 无法从应用发布包文件名解析 version：{path.name}")
    return match.group(1)


def plan_expected_app_images(app_release_dir: Path, targets: list[AppTarget]) -> list[ExpectedAppImage]:
    result: list[ExpectedAppImage] = []
    for target in targets:
        status, package, note = find_unique_app_release(app_release_dir, target)
        if status != "OK" or package is None:
            result.append(
                ExpectedAppImage(
                    target=target,
                    input_status=status,
                    release_basename="",
                    image_filename="",
                    note=note,
                )
            )
            continue
        version = version_from_app_release_path(package)
        filename = app_image_filename(target.app, version, target.platform_slug, target.variant)
        result.append(
            ExpectedAppImage(
                target=target,
                input_status="OK",
                release_basename=package.name,
                image_filename=filename,
            )
        )
    return result


def plan_expected_infra_images(targets: list[InfraTarget]) -> list[ExpectedInfraImage]:
    return [
        ExpectedInfraImage(
            target=target,
            image_filename=infra_image_filename(
                target.id, target.upstream_version, target.acps_version, target.platform_slug
            ),
        )
        for target in targets
    ]


def _print_section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def run_plan(
    *,
    app_release_dir: Path,
    targets_path: Path,
    lock_paths: list[Path],
    platform_filter: str,
    output_dir: Path | None,
) -> int:
    app_targets, infra_targets, errors = parse_image_targets(targets_path)
    if errors:
        print("[ERROR] 目标清单结构校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    lock = load_image_inputs_lock(lock_paths)
    cross_errors = cross_check_lock(app_targets, infra_targets, lock)
    if cross_errors:
        print("[ERROR] 目标清单与 image-inputs.lock 交叉校验失败：", file=sys.stderr)
        for error in cross_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"[OK] 目标清单校验通过：{len(app_targets)} 个 app targets，{len(infra_targets)} 个 infra targets")
    print(f"过滤平台：{platform_filter}")
    print(f"应用发布包目录：{app_release_dir}")
    if output_dir is not None:
        print(f"输出目录（核对）：{output_dir}")

    _print_section("一、应用发布包扫描")
    packages = scan_app_release_dir(app_release_dir)
    if not packages:
        print(f"（目录下未找到 *-app-release-*.tar.gz：{app_release_dir}）")
    for path in packages:
        parsed = parse_app_release_basename(path.name)
        if parsed is None:
            print(f"  {path.name}")
            print("    解析：无法解析文件名（仍可能被通配匹配使用）")
            continue
        note = ""
        if parsed.is_darwin:
            note = " [skipped for Linux image matrix]"
        variant_disp = parsed.variant or "<none>"
        print(f"  {path.name}{note}")
        print(
            f"    app={parsed.app} platform_slug={parsed.platform_slug} "
            f"python_tag={parsed.python_tag} variant={variant_disp} version={parsed.version}"
        )

    filtered_apps = filter_app_targets(app_targets, platform_filter)
    filtered_infras = filter_infra_targets(infra_targets, platform_filter)
    expected_apps = plan_expected_app_images(app_release_dir, filtered_apps)
    expected_infras = plan_expected_infra_images(filtered_infras)

    _print_section(f"二、期望应用镜像（platform={platform_filter}）")
    if not expected_apps:
        print("  （无匹配的 app target）")
    missing_input_count = 0
    for item in expected_apps:
        t = item.target
        variant_disp = t.variant or "<none>"
        label = f"app={t.app} platform={t.platform} python_tag={t.python_tag} variant={variant_disp}"
        if item.input_status != "OK":
            missing_input_count += 1
            print(f"  {item.input_status} {label}")
            if item.note:
                print(f"    {item.note}")
            continue
        print(f"  OK {label}")
        print(f"    输入：{item.release_basename}")
        print(f"    期望镜像包：{item.image_filename}")

    _print_section(f"三、期望基础设施镜像（platform={platform_filter}）")
    if not expected_infras:
        print("  （无匹配的 infra target）")
    for item in expected_infras:
        t = item.target
        print(
            f"  id={t.id} kind={t.kind} upstream={t.upstream_version} "
            f"acps={t.acps_version} platform={t.platform}"
        )
        print(f"    期望镜像包：{item.image_filename}")

    expected_count = len(expected_apps) + len(expected_infras)
    if expected_count == 0:
        print("[ERROR] 过滤条件没有匹配到任何期望镜像目标", file=sys.stderr)
        return 1
    if missing_input_count > 0:
        print(f"[WARN] {missing_input_count} 个 app target 缺少唯一应用发布包输入", file=sys.stderr)

    if output_dir is None:
        _print_section("四、输出目录核对")
        print("  （未传 --output，仅打印规划，不核对产物）")
        return 0

    _print_section(f"四、输出目录核对（{output_dir}）")
    if not output_dir.is_dir():
        print(f"[ERROR] --output 不存在或不是目录：{output_dir}", file=sys.stderr)
        return 1

    missing_files = 0
    present_files = 0

    def check_file(filename: str, context: str) -> None:
        nonlocal missing_files, present_files
        path = output_dir / filename
        if path.is_file():
            present_files += 1
            print(f"  PRESENT {filename}")
            print(f"    ({context})")
        else:
            missing_files += 1
            print(f"  MISSING {filename}")
            print(f"    ({context})")

    for item in expected_apps:
        t = item.target
        variant_disp = t.variant or "<none>"
        context = f"app={t.app} variant={variant_disp}"
        if item.input_status != "OK" or not item.image_filename:
            print(f"  SKIP （输入 {item.input_status}，无法推导镜像文件名） {context}")
            continue
        check_file(item.image_filename, context)

    for item in expected_infras:
        check_file(item.image_filename, f"infra={item.target.id}")

    print()
    print(f"核对汇总：PRESENT={present_files} MISSING={missing_files} INPUT_GAPS={missing_input_count}")
    if missing_files > 0 or missing_input_count > 0:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-release-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--lock", action="append", default=[], help="image-inputs.lock，可重复")
    parser.add_argument("--platform", default="", help="过滤 Docker platform；默认 linux/<host_arch>")
    args = parser.parse_args()

    platform_filter = args.platform.strip()
    if not platform_filter:
        platform_filter = f"linux/{detect_host_arch()}"

    lock_paths = [Path(p) for p in args.lock]
    if not lock_paths:
        raise SystemExit("[ERROR] 至少需要一个 --lock")

    raise SystemExit(
        run_plan(
            app_release_dir=args.app_release_dir,
            targets_path=args.targets,
            lock_paths=lock_paths,
            platform_filter=platform_filter,
            output_dir=args.output,
        )
    )


if __name__ == "__main__":
    main()
