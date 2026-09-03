#!/usr/bin/env python3
"""image-mode 镜像打包共享工具函数。

被 lib/ 下其余模块和顶层编排脚本调用的 python 部分共同导入，避免 Docker platform /
platform slug 转换、镜像 tag、镜像包文件名这类命名规则出现多份实现后逐渐漂移。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 当前正式支持的 Docker platform 集合；镜像打包矩阵目前只覆盖 linux/amd64 与
# linux/arm64（应用/基础设施镜像矩阵均未出现其他平台）。新增平台时
# 在此处显式追加，不要放宽为任意字符串正则，避免把拼写错误的平台值当成合法输入。
KNOWN_DOCKER_PLATFORMS = ("linux/amd64", "linux/arm64")

_MODULE_ISH_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# 模块名只允许点分隔的 Python 标识符（如 "app"、"acps_sdk"、"a.b.c"），拒绝任何
# shell 元字符，作为把 import smoke 模块名嵌入 `python -c "..."` 字符串前的输入校验
# （与 release/assembly/runtime_smoke.py 的同名校验保持一致的正则）。
MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")


def is_safe_relpath(relpath: str) -> bool:
    """拒绝绝对路径和任何包含 '..' 路径段的相对路径（防止 CWE-22 目录穿越）。

    应用最终发布包内部的 build-manifest.toml / runtime-package.toml 可能被篡改或
    损坏；在把其中记录的路径与本地目录拼接之前必须先做这一步。与
    release/assembly/*.py、release/lib/runtime_package.py 中的同名校验保持一致实现。
    """
    if not relpath:
        return False
    candidate = Path(relpath)
    if candidate.is_absolute():
        return False
    return ".." not in candidate.parts


def is_safe_path_component(value: str) -> bool:
    """校验 value 可以安全地作为单个路径 segment 使用（例如拼进输出文件名）：
    非空、不含路径分隔符（'/' 或 '\\'）、不是 "." 或 ".."。

    用于防止 build-manifest.toml 等可能被篡改的数据字段（如 version/app/variant）在拼接成
    最终镜像包文件名时构成目录穿越（CWE-22）——与 is_safe_relpath 不同，这里校验的是
    单个名称段，不允许包含任何路径分隔符（而 is_safe_relpath 允许多级目录）。
    """
    if not value:
        return False
    if "/" in value or "\\" in value:
        return False
    return value not in (".", "..")


def docker_platform_to_slug(platform: str) -> str:
    """把 Docker platform（如 "linux/arm64"）转换成文件名/标签用的 slug（"linux-arm64"）。

    只接受 KNOWN_DOCKER_PLATFORMS 中的值，防止把 "linux-amd64" 误当成 Docker
    platform 传给 buildx，也防止把 "linux/amd64" 直接拼进文件名。
    """
    if platform not in KNOWN_DOCKER_PLATFORMS:
        raise SystemExit(
            f"[ERROR] 未知的 Docker platform：{platform!r}（当前只支持 {', '.join(KNOWN_DOCKER_PLATFORMS)}）"
        )
    return platform.replace("/", "-")


def validate_id(label: str, value: str) -> None:
    """校验 app id / infra id 之类的标识符只使用小写字母、数字和连字符。"""
    if not isinstance(value, str) or not _MODULE_ISH_ID_PATTERN.match(value):
        raise SystemExit(f"[ERROR] {label} 不是合法标识符（只允许小写字母/数字/连字符，且不能以连字符开头）：{value!r}")


def validate_version(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"[ERROR] {label} 不能为空")


# ---------------------------------------------------------------------------
# 应用镜像命名
# ---------------------------------------------------------------------------


def app_image_repo(app: str) -> str:
    return f"acps/{app}"


def app_image_tag_suffix(version: str, platform_slug: str, variant: str = "") -> str:
    parts = [version, platform_slug]
    if variant:
        parts.append(variant)
    return "-".join(parts)


def app_image_tag(app: str, version: str, platform_slug: str, variant: str = "") -> str:
    return f"{app_image_repo(app)}:{app_image_tag_suffix(version, platform_slug, variant)}"


def app_image_filename(app: str, version: str, platform_slug: str, variant: str = "") -> str:
    return f"acps-{app}-{app_image_tag_suffix(version, platform_slug, variant)}.image.tar.gz"


# ---------------------------------------------------------------------------
# 基础设施镜像命名
# ---------------------------------------------------------------------------


def infra_image_repo(infra_id: str) -> str:
    return f"acps/{infra_id}"


def infra_image_tag_suffix(upstream_version: str, acps_version: str, platform_slug: str) -> str:
    return f"{upstream_version}-{acps_version}-{platform_slug}"


def infra_image_tag(infra_id: str, upstream_version: str, acps_version: str, platform_slug: str) -> str:
    return f"{infra_image_repo(infra_id)}:{infra_image_tag_suffix(upstream_version, acps_version, platform_slug)}"


def infra_image_filename(infra_id: str, upstream_version: str, acps_version: str, platform_slug: str) -> str:
    suffix = infra_image_tag_suffix(upstream_version, acps_version, platform_slug)
    return f"acps-{infra_id}-{suffix}.image.tar.gz"


# ---------------------------------------------------------------------------
# 镜像级 smoke（ / 共用）：构造安全的 import 校验 python 片段
# ---------------------------------------------------------------------------


def validate_module_name(label: str, name: str) -> None:
    if not MODULE_NAME_PATTERN.match(name):
        raise SystemExit(f"[ERROR] {label} 不是合法的 Python 模块名（可能是命令注入尝试）：{name!r}")


def build_import_smoke_snippet(import_checks: list[str], assert_present: list[str], assert_absent: list[str]) -> str:
    """构造供 `python -c "<snippet>"` 使用的最小 import/断言校验语句串。

    与 release/assembly/runtime_smoke.py 的 `_build_python_snippet` 同构，独立实现
    以保持 image-packaging 与 assembly 两条流水线互不耦合。
    """
    for name in (*import_checks, *assert_present, *assert_absent):
        validate_module_name("import smoke 模块名", name)

    statements: list[str] = []
    if assert_present or assert_absent:
        statements.append("import importlib.util")
    for module in assert_present:
        statements.append(
            f"assert importlib.util.find_spec({module!r}) is not None, 'expected present but missing: {module}'"
        )
    for module in assert_absent:
        statements.append(f"assert importlib.util.find_spec({module!r}) is None, 'unexpected present: {module}'")
    statements.extend(f"import {module}" for module in import_checks)
    return "; ".join(statements)


def main() -> None:
    parser = argparse.ArgumentParser(description="image-packaging 共享命名/校验工具的 CLI 入口")
    subparsers = parser.add_subparsers(dest="command", required=True)

    platform_slug_cmd = subparsers.add_parser("platform-slug", help="把 Docker platform 转换成文件名/标签用的 slug")
    platform_slug_cmd.add_argument("--platform", required=True)

    smoke_cmd = subparsers.add_parser("smoke-snippet", help="生成 import smoke 用的 python -c 片段")
    smoke_cmd.add_argument("--import-check", action="append", default=[])
    smoke_cmd.add_argument("--assert-present", action="append", default=[])
    smoke_cmd.add_argument("--assert-absent", action="append", default=[])

    args = parser.parse_args()
    if args.command == "platform-slug":
        print(docker_platform_to_slug(args.platform))
        return
    if args.command == "smoke-snippet":
        print(build_import_smoke_snippet(args.import_check, args.assert_present, args.assert_absent))


if __name__ == "__main__":
    main()
