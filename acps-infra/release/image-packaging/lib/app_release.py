#!/usr/bin/env python3
"""应用最终发布包安全解包与轻量校验。

只做镜像构建前的输入防线，不重新执行里的 audit/runtime smoke 校验：

  1. 解包前计算原始 tar 文件 SHA256（供后续 APP_RELEASE_SHA256 build arg 和 label 使用）。
  2. 安全解包：拒绝绝对路径、".." 路径段和符号链接逃逸（tarfile "data" filter，PEP 706），
     并校验解包后顶层恰好一个目录，规范化到 build-context/app-release/。
  3. 校验根目录 checksums.txt（结构 + 摘要），不重新校验内部 app/checksums.txt
     （那是装配阶段已经做过的 audit 范围）。
  4. 校验 build-manifest.toml、app/runtime-package.toml、wheelhouse/ 存在。
  5. 读取 build-manifest.toml 的 platform、python_tag、variant、app_wheel、version。
  6. 校验 build-manifest.toml 记录的 platform 与目标 Docker platform 完全一致
     （build-manifest.toml 的 platform 字段本身就是 Docker platform 形式，如
     "linux/arm64"，不是 slug——不要在这里误用 platform slug 比较）。
  7. 校验 app_wheel 存在于 wheelhouse/ 中。

用法：
  python3 app_release.py inspect --package <final-release.tar.gz> \
      --target-platform linux/arm64 --target-python-tag cp314 --dest <build-context-dir>
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from common import is_safe_path_component, is_safe_relpath

CHECKSUMS_FILENAME = "checksums.txt"


@dataclass(frozen=True)
class AppReleaseInfo:
    root: Path  # 规范化后的应用最终发布包根目录（build-context/app-release/）
    tar_sha256: str
    app: str
    version: str
    platform: str
    python_tag: str
    variant: str
    app_wheel: str


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksums_lines(checksums_path: Path, errors: list[str]) -> dict[str, str]:
    recorded: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"根 checksums.txt 存在无法解析的行：{line!r}")
            continue
        digest, relpath = parts
        relpath = relpath.lstrip("*")
        if relpath.startswith("./"):
            relpath = relpath[2:]
        if not is_safe_relpath(relpath):
            errors.append(f"根 checksums.txt 记录了非法路径（可能是目录穿越）：{relpath!r}")
            continue
        recorded[relpath] = digest
    return recorded


def verify_root_checksums(root: Path, errors: list[str]) -> None:
    """校验根目录 checksums.txt 记录的文件存在且摘要匹配，并拒绝未被记录的多余文件。

    这里仍然不重新执行 app/checksums.txt / runtime smoke 等上游重校验，但根
    checksums.txt 作为镜像打包阶段的最外层输入完整性防线，必须至少保证：

    1. 记录过的文件摘要正确。
    2. 包内不存在未被根 checksums.txt 记录的额外文件。

    否则只要把 checksums.txt 清空或删掉部分条目，篡改后的最终发布包仍可能通过
    image-mode 的  轻量校验，失去这层完整性门禁。
    """
    checksums_path = root / CHECKSUMS_FILENAME
    if not checksums_path.is_file():
        errors.append(f"缺少根 {CHECKSUMS_FILENAME}")
        return
    recorded = _parse_checksums_lines(checksums_path, errors)
    for relpath, digest in recorded.items():
        file_path = root / relpath
        if not file_path.is_file():
            errors.append(f"根 checksums.txt 记录的文件不存在：{relpath}")
            continue
        actual = sha256_of_file(file_path)
        if actual != digest:
            errors.append(f"根 checksums.txt 记录的 {relpath} 摘要不匹配（记录 {digest}，实际 {actual}）")

    for path in root.rglob("*"):
        if path.is_file() and path != checksums_path:
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel not in recorded:
                errors.append(f"根 checksums.txt 未记录包内文件：{rel}")


def _safe_extract_and_normalize(package_path: Path, app_release_dir: Path) -> Path:
    """使用 tarfile "data" filter（PEP 706）解包到临时 scratch 目录，校验解包结果
    顶层恰好一个目录，再把该目录的内容"提升"到 app_release_dir 本身（等价于安全的
    `tar --strip-components=1`），使调用方后续可以统一按 app_release_dir/wheelhouse、
    app_release_dir/app 等路径消费。"""
    scratch_dir = app_release_dir.parent / f".{app_release_dir.name}-scratch"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True)
    try:
        with tarfile.open(package_path) as tf:
            tf.extractall(scratch_dir, filter="data")
    except tarfile.TarError as exc:
        raise SystemExit(f"[ERROR] 应用最终发布包解包失败（可能包含目录穿越或非法成员）：{exc}") from exc

    top_level = list(scratch_dir.iterdir())
    dirs = [p for p in top_level if p.is_dir()]
    if len(top_level) != 1 or len(dirs) != 1:
        raise SystemExit(
            "[ERROR] 应用最终发布包顶层条目异常（必须恰好一个目录，不允许杂项文件）："
            f"{[p.name for p in top_level]}"
        )

    sole_dir = dirs[0]
    if app_release_dir.exists():
        shutil.rmtree(app_release_dir)
    shutil.move(str(sole_dir), str(app_release_dir))
    shutil.rmtree(scratch_dir)
    return app_release_dir


def inspect_app_release(
    package_path: Path,
    dest_dir: Path,
    target_platform: str,
    target_python_tag: str,
) -> AppReleaseInfo:
    """解包 package_path 到 dest_dir/app-release/（已规范化，去除 tar 顶层目录名，
    见 _safe_extract_and_normalize），做轻量结构校验，返回解析结果。

    dest_dir 应是本次构建专用的临时 build-context 目录；调用方负责在使用完毕后清理。
    校验失败时抛出 SystemExit 并汇总尽可能多的错误信息。
    """
    if not package_path.is_file():
        raise SystemExit(f"[ERROR] 应用最终发布包不存在：{package_path}")

    tar_sha256 = sha256_of_file(package_path)

    app_release_dir = dest_dir / "app-release"
    extracted_root = _safe_extract_and_normalize(package_path, app_release_dir)

    errors: list[str] = []
    verify_root_checksums(extracted_root, errors)

    build_manifest_path = extracted_root / "build-manifest.toml"
    wheelhouse_dir = extracted_root / "wheelhouse"
    app_dir = extracted_root / "app"
    runtime_package_path = app_dir / "runtime-package.toml"

    if not build_manifest_path.is_file():
        errors.append("缺少 build-manifest.toml")
    if not runtime_package_path.is_file():
        errors.append("缺少 app/runtime-package.toml")
    if not wheelhouse_dir.is_dir():
        errors.append("缺少 wheelhouse/ 目录")

    if errors:
        _fail(errors)

    try:
        manifest = tomllib.loads(build_manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[ERROR] 无法解析 build-manifest.toml：{exc}") from exc

    app = manifest.get("app", "")
    version = manifest.get("version", "")
    platform = manifest.get("platform", "")
    python_tag = manifest.get("python_tag", "")
    variant = manifest.get("variant", "") or ""
    app_wheel = manifest.get("app_wheel", "")

    for field_name, value in (("app", app), ("version", version), ("platform", platform), ("python_tag", python_tag), ("app_wheel", app_wheel)):
        if not value:
            errors.append(f"build-manifest.toml 缺少字段 {field_name!r}")

    # app / version / variant 会被直接拼进最终镜像包文件名（common.py 的
    # app_image_filename），build-manifest.toml 是应用最终发布包内部数据，可能被
    # 篡改或损坏；必须先确认它们是安全的单个路径 segment（不含 '/'，不是 ".."），
    # 否则会构成目录穿越（CWE-22），例如把最终产物写到 --output 目录之外。
    for field_name, value in (("app", app), ("version", version)):
        if value and not is_safe_path_component(value):
            errors.append(f"build-manifest.toml 的 {field_name} 包含非法字符（可能是目录穿越）：{value!r}")
    if variant and not is_safe_path_component(variant):
        errors.append(f"build-manifest.toml 的 variant 包含非法字符（可能是目录穿越）：{variant!r}")

    if app_wheel and not is_safe_relpath(app_wheel):
        errors.append(f"build-manifest.toml 的 app_wheel 是非法路径（可能是目录穿越）：{app_wheel!r}")
    elif app_wheel and not (wheelhouse_dir / app_wheel).is_file():
        errors.append(f"wheelhouse/ 中不存在 app_wheel：{app_wheel}")

    if platform and platform != target_platform:
        errors.append(
            f"build-manifest.toml 的 platform（{platform!r}）与目标 Docker platform（{target_platform!r}）不一致"
        )
    if python_tag and python_tag != target_python_tag:
        errors.append(
            f"build-manifest.toml 的 python_tag（{python_tag!r}）与目标 python_tag（{target_python_tag!r}）不一致"
        )

    if errors:
        _fail(errors)

    return AppReleaseInfo(
        root=extracted_root,
        tar_sha256=tar_sha256,
        app=app,
        version=version,
        platform=platform,
        python_tag=python_tag,
        variant=variant,
        app_wheel=app_wheel,
    )


def _fail(errors: list[str]) -> None:
    print("[ERROR] 应用最终发布包轻量校验失败：", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_cmd = subparsers.add_parser("inspect", help="解包并校验应用最终发布包")
    inspect_cmd.add_argument("--package", required=True)
    inspect_cmd.add_argument("--target-platform", required=True, help="Docker platform，例如 linux/arm64")
    inspect_cmd.add_argument("--target-python-tag", required=True)
    inspect_cmd.add_argument("--dest", required=True, help="解包目标目录（会创建 <dest>/app-release/）")
    inspect_cmd.add_argument("--clean", action="store_true", help="解包前先清空 --dest")

    args = parser.parse_args()
    if args.command == "inspect":
        dest_dir = Path(args.dest)
        if args.clean and dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        info = inspect_app_release(
            Path(args.package), dest_dir, args.target_platform, args.target_python_tag
        )
        print(f"[OK] 应用最终发布包校验通过：{info.root}")
        print(f"  app={info.app} version={info.version} platform={info.platform} "
              f"python_tag={info.python_tag} variant={info.variant or '<none>'}")
        print(f"  app_wheel={info.app_wheel}")
        print(f"  tar_sha256={info.tar_sha256}")


if __name__ == "__main__":
    main()
