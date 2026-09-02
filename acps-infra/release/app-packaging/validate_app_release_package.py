#!/usr/bin/env python3
"""校验应用发布包的结构与 checksum 完整性。

确认确认 wheelhouse/、app/runtime-package.toml、build-manifest.toml、
checksums.txt 均存在；先校验根目录 checksums.txt，再以 app/ 为根校验 app/checksums.txt
确认 build-manifest.toml 记录的 app_wheel 同时存在于 wheelhouse/ 和 app/dist/ 且摘要一致
解析 app/runtime-package.toml，逐项确认 [[assets]] 声明的资源真实存在。

用法：
  python3 validate_app_release_package.py --package <app-release.tar.gz>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

CHECKSUMS_FILENAME = "checksums.txt"


def _is_safe_relpath(relpath: str) -> bool:
    """拒绝绝对路径和任何包含 '..' 路径段的相对路径。

    checksums.txt / build-manifest.toml / runtime-package.toml 均是待校验发布包内部的
    内容，可能被篡改或损坏；在把其中记录的路径与本地目录拼接之前必须先做这一步
    否则会构成目录穿越（CWE-22）——比如把 app_wheel 或 [[assets]].path 写成
    "../../../../etc/passwd" 就能让本工具在受信任目录之外读取/哈希任意文件。
    """
    if not relpath:
        return False
    candidate = Path(relpath)
    if candidate.is_absolute():
        return False
    return ".." not in candidate.parts


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest


def _parse_checksums_lines(checksums_path: Path, errors: list[str], label: str) -> dict[str, str]:
    """解析 checksums.txt 的每一行为 {relpath: digest}，跳过空行并记录无法解析/非法路径的行。"""
    recorded: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines:
        line = line.strip
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"{label}: 存在无法解析的行：{line!r}")
            continue
        digest, relpath = parts
        relpath = relpath.lstrip("*")
        if relpath.startswith("./"):
            relpath = relpath[2:]
        if not _is_safe_relpath(relpath):
            errors.append(f"{label}: 记录了非法路径（可能是目录穿越）：{relpath!r}")
            continue
        recorded[relpath] = digest
    return recorded


def _verify_recorded_files(root: Path, recorded: dict[str, str], errors: list[str], label: str) -> None:
    """逐项确认 checksums.txt 记录的文件真实存在，且摘要与记录一致。"""
    for relpath, digest in recorded.items:
        file_path = root / relpath
        if not file_path.is_file():
            errors.append(f"{label}: 记录的文件不存在：{relpath}")
            continue
        actual = sha256_of(file_path)
        if actual != digest:
            errors.append(f"{label}: {relpath} 摘要不匹配（记录 {digest}，实际 {actual}）")


def _verify_no_untracked_files(
    root: Path, checksums_path: Path, recorded: dict[str, str], errors: list[str], label: str
) -> None:
    """确认目录下不存在 checksums.txt 未记录的多余文件。"""
    for path in root.rglob("*"):
        if path.is_file() and path != checksums_path:
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel not in recorded:
                errors.append(f"{label}: 存在未被记录的文件：{rel}")


def verify_checksums_file(root: Path, checksums_path: Path, errors: list[str], label: str) -> None:
    if not checksums_path.is_file():
        errors.append(f"{label}: 文件不存在")
        return

    recorded = _parse_checksums_lines(checksums_path, errors, label)
    _verify_recorded_files(root, recorded, errors, label)
    _verify_no_untracked_files(root, checksums_path, recorded, errors, label)


def _verify_app_wheel(manifest: dict, wheelhouse_dir: Path, app_dir: Path, errors: list[str]) -> None:
    app_wheel = manifest.get("app_wheel", "")
    if not app_wheel:
        errors.append("build-manifest.toml 缺少 app_wheel 字段")
        return
    if not _is_safe_relpath(app_wheel):
        errors.append(f"build-manifest.toml 的 app_wheel 是非法路径（可能是目录穿越）：{app_wheel!r}")
        return

    wheelhouse_wheel = wheelhouse_dir / app_wheel
    app_dist_wheel = app_dir / "dist" / app_wheel
    if not wheelhouse_wheel.is_file():
        errors.append(f"app_wheel 在 wheelhouse/ 中不存在：{app_wheel}")
    if not app_dist_wheel.is_file():
        errors.append(f"app_wheel 在 app/dist/ 中不存在：{app_wheel}")
    if wheelhouse_wheel.is_file() and app_dist_wheel.is_file() and sha256_of(wheelhouse_wheel) != sha256_of(app_dist_wheel):
        errors.append(f"app_wheel 在 wheelhouse/ 与 app/dist/ 中摘要不一致：{app_wheel}")


def _verify_lockfile_fields(manifest: dict, app_dir: Path, errors: list[str]) -> None:
    """discovery-server CPU/GPU 变体：校验 build-manifest.toml 记录的
 lockfile 是安全相对路径，且 app/{lockfile} 真实存在并匹配 lockfile_sha256。"""
    lockfile = manifest.get("lockfile", "")
    if not lockfile:
        return
    if not _is_safe_relpath(lockfile):
        errors.append(f"build-manifest.toml 的 lockfile 是非法路径（可能是目录穿越）：{lockfile!r}")
        return

    lockfile_path = app_dir / lockfile
    lockfile_sha256 = manifest.get("lockfile_sha256", "")
    if not lockfile_path.is_file():
        errors.append(f"build-manifest.toml 记录的 lockfile 不存在：app/{lockfile}")
    elif not lockfile_sha256:
        errors.append("build-manifest.toml 声明了 lockfile 但缺少 lockfile_sha256 字段")
    elif sha256_of(lockfile_path) != lockfile_sha256:
        errors.append(f"app/{lockfile} 摘要与 build-manifest.toml 记录的 lockfile_sha256 不一致")


def _verify_variant_runtime_mode(manifest: dict, errors: list[str]) -> None:
    """discovery-server CPU/GPU 变体：校验 build-manifest.toml 的 variant 与
    runtime_mode 一致。二者只在声明了 variant_lockfiles 的 app（例如 discovery-server）上才会
    出现；只要两个字段都存在就必须相等，防止出现"依赖 profile 是 cpu，但业务运行模式记录成
 gpu"这类不一致的发布包。"""
    variant = manifest.get("variant")
    runtime_mode = manifest.get("runtime_mode")
    if variant is not None and runtime_mode is not None and variant != runtime_mode:
        errors.append(f"build-manifest.toml 的 variant（{variant!r}）与 runtime_mode（{runtime_mode!r}）不一致")


def _verify_build_manifest(build_manifest_path: Path, wheelhouse_dir: Path, app_dir: Path, errors: list[str]) -> None:
    if not build_manifest_path.is_file():
        errors.append("缺少 build-manifest.toml")
        return

    try:
        manifest = tomllib.loads(build_manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"build-manifest.toml 解析失败：{exc}")
        return
    if not isinstance(manifest, dict):
        errors.append("build-manifest.toml 顶层内容必须是表")
        return

    _verify_app_wheel(manifest, wheelhouse_dir, app_dir, errors)
    _verify_lockfile_fields(manifest, app_dir, errors)
    _verify_variant_runtime_mode(manifest, errors)


def _load_runtime_package_assets(app_runtime_toml: Path, errors: list[str]) -> list | None:
    """解析 app/runtime-package.toml，返回 [[assets]] 数组；解析失败或类型不符时记录错误并返回 None。"""
    if not app_runtime_toml.is_file():
        errors.append("缺少 app/runtime-package.toml")
        return None

    try:
        rt_data = tomllib.loads(app_runtime_toml.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"app/runtime-package.toml 解析失败：{exc}")
        return None

    assets = rt_data.get("assets", []) if isinstance(rt_data, dict) else []
    if not isinstance(assets, list):
        errors.append("app/runtime-package.toml 的 [[assets]] 必须是数组表")
        return None
    return assets


def _verify_single_asset(asset: object, app_dir: Path, errors: list[str]) -> None:
    """校验单个 [[assets]] 条目：path 必须是安全相对路径且真实存在，required_children 同理。"""
    if not isinstance(asset, dict):
        errors.append(f"[[assets]] 的条目必须是表：{asset!r}")
        return
    asset_path = asset.get("path")
    if not asset_path:
        return
    if not _is_safe_relpath(asset_path):
        errors.append(f"[[assets]] 的 path 是非法路径（可能是目录穿越）：{asset_path!r}")
        return
    abs_path = app_dir / asset_path
    if not abs_path.exists():
        errors.append(f"[[assets]] 路径不存在：app/{asset_path}")
        return
    for child in asset.get("required_children", []) or []:
        if not _is_safe_relpath(child):
            errors.append(f"[[assets]] required_children 是非法路径（可能是目录穿越）：{child!r}")
            continue
        if not (abs_path / child).exists():
            errors.append(f"[[assets]] required_children 缺失：app/{asset_path}/{child}")


def _verify_runtime_package_assets(app_runtime_toml: Path, app_dir: Path, errors: list[str]) -> None:
    assets = _load_runtime_package_assets(app_runtime_toml, errors)
    if assets is None:
        return
    for asset in assets:
        _verify_single_asset(asset, app_dir, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", required=True)
    args = parser.parse_args

    package_path = Path(args.package)
    if not package_path.is_file():
        raise SystemExit(f"[ERROR] 未找到发布包：{package_path}")

    errors: list[str] = []

    with tempfile.TemporaryDirectory as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(package_path) as tf:
            tf.extractall(tmp_path, filter="data")

        top_level = list(tmp_path.iterdir())
        roots = [p for p in top_level if p.is_dir()]
        if len(top_level) != 1 or len(roots) != 1:
            raise SystemExit(f"[ERROR] tar 顶层条目异常（必须恰好一个目录，不允许杂杂项）：{[p.name for p in top_level]}")
        root = roots[0]

        wheelhouse_dir = root / "wheelhouse"
        app_dir = root / "app"
        build_manifest_path = root / "build-manifest.toml"
        root_checksums = root / CHECKSUMS_FILENAME
        app_checksums = app_dir / CHECKSUMS_FILENAME
        app_runtime_toml = app_dir / "runtime-package.toml"

        for required, name in (
            (wheelhouse_dir, "wheelhouse/"),
            (app_dir, "app/"),
            (build_manifest_path, "build-manifest.toml"),
            (root_checksums, CHECKSUMS_FILENAME),
        ):
            if not required.exists():
                errors.append(f"发布包缺少必需路径：{name}")

        if root_checksums.is_file():
            verify_checksums_file(root, root_checksums, errors, "根目录 checksums.txt")

        if app_checksums.is_file():
            verify_checksums_file(app_dir, app_checksums, errors, "app/checksums.txt")
        else:
            errors.append("缺少 app/checksums.txt")

        _verify_build_manifest(build_manifest_path, wheelhouse_dir, app_dir, errors)
        _verify_runtime_package_assets(app_runtime_toml, app_dir, errors)

    if errors:
        print("[ERROR] 发布包结构/checksum 校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    print(f"[OK] 发布包结构与 checksum 校验通过：{package_path}")


if __name__ == "__main__":
    main
