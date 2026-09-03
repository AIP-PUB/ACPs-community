#!/usr/bin/env python3
"""对最终发布包做离线安装 + 最小 import 冒烟验证。

Linux 目标：使用与 (platform, python_tag) 匹配的最小 runtime 镜像，只做：
  1. `pip install --no-index --find-links wheelhouse wheelhouse/{app_wheel}` 离线安装。
  2. 对显式配置的顶层模块执行最小 `import` 验证。
  3. 可选：对显式配置的模块名断言安装后不存在（--assert-absent）或必须存在
     （--assert-present）。

Darwin 目标（`--platform darwin/*`）：在本机构建机 venv 中做等价离线安装与 import
（不访问外网索引）；`--smoke-image` 可为占位字符串。

不安装 auditwheel；Linux 容器以 --network none 运行。只引用最终包根目录的
wheelhouse/，不从 app/dist/ 安装，也不引用装配包 packages/。

import 目标必须显式传入（--import-check），不能从 app id 或 build-manifest.toml 推导。

用法：
  python3 runtime_smoke.py --package <final-release.tar.gz> --platform linux/arm64 \
      --python-tag cp314 --smoke-image python:3.14-slim@sha256:... --import-check app

  python3 runtime_smoke.py --package <darwin-cli.tar.gz> --platform darwin/arm64 \
      --python-tag cp314 --smoke-image host-native --import-check acps_cli
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

# 模块名只允许点分隔的 Python 标识符（如 "app"、"acps_cli"、"a.b.c"），
# 拒绝任何 shell 元字符，作为嵌入 sh -c 命令字符串前的第一道输入校验。
MODULE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")


def _is_safe_relpath(relpath: str) -> bool:
    """拒绝绝对路径和任何包含 '..' 路径段的相对路径，防止 build-manifest.toml
    （可能被篡改/损坏）里的 app_wheel 字段构成目录穿越。"""
    if not relpath:
        return False
    candidate = Path(relpath)
    if candidate.is_absolute():
        return False
    return ".." not in candidate.parts


def _wheel_distribution_name(wheel_filename: str) -> str:
    stem = wheel_filename[: -len(".whl")] if wheel_filename.endswith(".whl") else wheel_filename
    distribution = stem.split("-")[0]
    return re.sub(r"[-_.]+", "-", distribution.lower())


def _build_python_snippet(assert_absent: list[str], assert_present: list[str], import_check: list[str]) -> str:
    statements: list[str] = []
    if assert_absent or assert_present:
        statements.append("import importlib.util")
    for module in assert_absent:
        statements.append(f"assert importlib.util.find_spec({module!r}) is None, 'unexpected present: {module}'")
    for module in assert_present:
        statements.append(
            f"assert importlib.util.find_spec({module!r}) is not None, 'expected present but missing: {module}'"
        )
    statements.extend(f"import {module}" for module in import_check)
    return "; ".join(statements)


def _run_smoke_container(platform: str, wheelhouse_dir: Path, package_spec: str, snippet: str, smoke_image: str) -> subprocess.CompletedProcess[str]:
    quoted_package_spec = shlex.quote(package_spec)
    quoted_snippet = shlex.quote(snippet)
    container_cmd = (
        "set -e; "
        f"pip install --no-cache-dir --no-index --find-links /wheelhouse {quoted_package_spec} "
        f"&& python -c {quoted_snippet} "
        "&& echo ACPS_RUNTIME_SMOKE_IMPORT_OK"
    )
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            "--network",
            "none",
            "-v",
            f"{wheelhouse_dir}:/wheelhouse:ro",
            smoke_image,
            "sh",
            "-c",
            container_cmd,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_host_python(python_tag: str) -> str:
    """cp314 → python3.14；可用 ACPS_HOST_ASSEMBLE_PYTHON 覆盖。"""
    import os
    import shutil

    override = os.environ.get("ACPS_HOST_ASSEMBLE_PYTHON", "").strip()
    match = re.fullmatch(r"cp([0-9])([0-9]+)", python_tag)
    if not match:
        raise SystemExit(f"[ERROR] 无法从 python_tag={python_tag!r} 推导本机解释器版本")
    major_minor = f"{match.group(1)}.{match.group(2)}"
    candidates: list[str] = []
    if override:
        candidates.append(override)
    candidates.extend([f"python{major_minor}", "python3"])
    for cand in candidates:
        bin_path = shutil.which(cand) if not Path(cand).is_file() else cand
        if not bin_path:
            continue
        probe = subprocess.run(
            [
                bin_path,
                "-c",
                (
                    "import sys; "
                    f"raise SystemExit(0 if sys.version_info[:2]==({match.group(1)},{match.group(2)}) else 1)"
                ),
            ],
            check=False,
        )
        if probe.returncode != 0:
            continue
        pip_probe = subprocess.run([bin_path, "-m", "pip", "--version"], capture_output=True, check=False)
        if pip_probe.returncode == 0:
            return bin_path
    raise SystemExit(
        f"[ERROR] 未找到带 pip 的 Python {major_minor}（python_tag={python_tag}）；"
        f"可设置 ACPS_HOST_ASSEMBLE_PYTHON"
    )


def _run_smoke_host(python_tag: str, wheelhouse_dir: Path, package_spec: str, snippet: str) -> subprocess.CompletedProcess[str]:
    python_bin = _resolve_host_python(python_tag)
    with tempfile.TemporaryDirectory() as venv_parent:
        venv_dir = Path(venv_parent) / "venv"
        create = subprocess.run([python_bin, "-m", "venv", str(venv_dir)], capture_output=True, text=True, check=False)
        if create.returncode != 0:
            return create
        venv_python = venv_dir / "bin" / "python"
        venv_pip = venv_dir / "bin" / "pip"
        install = subprocess.run(
            [
                str(venv_pip),
                "install",
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(wheelhouse_dir),
                package_spec,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            return install
        smoke = subprocess.run(
            [str(venv_python), "-c", snippet + "; print('ACPS_RUNTIME_SMOKE_IMPORT_OK')"],
            capture_output=True,
            text=True,
            check=False,
        )
        # 合并 stdout，便于调用方匹配 ACPS_RUNTIME_SMOKE_IMPORT_OK
        combined = subprocess.CompletedProcess(
            args=smoke.args,
            returncode=smoke.returncode,
            stdout=(install.stdout or "") + (smoke.stdout or ""),
            stderr=(install.stderr or "") + (smoke.stderr or ""),
        )
        return combined


def _extract_package_root(package_path: Path, tmp_path: Path) -> Path:
    with tarfile.open(package_path) as tf:
        tf.extractall(tmp_path, filter="data")

    top_level = list(tmp_path.iterdir())
    roots = [p for p in top_level if p.is_dir()]
    if len(top_level) != 1 or len(roots) != 1:
        raise SystemExit(f"[ERROR] tar 顶层条目异常（必须恰好一个目录，不允许杂杂项）：{[p.name for p in top_level]}")
    return roots[0]


def _resolve_app_wheel(root: Path) -> tuple[Path, str, str, str, str]:
    wheelhouse_dir = root / "wheelhouse"
    build_manifest_path = root / "build-manifest.toml"
    if not wheelhouse_dir.is_dir():
        raise SystemExit("[ERROR] 最终包缺失 wheelhouse/ 目录")
    if not build_manifest_path.is_file():
        raise SystemExit("[ERROR] 最终包缺失 build-manifest.toml")

    manifest = tomllib.loads(build_manifest_path.read_text(encoding="utf-8"))
    app_wheel = manifest.get("app_wheel", "")
    app_version = manifest.get("version", "")
    app_variant = manifest.get("variant", "")
    if not app_wheel:
        raise SystemExit("[ERROR] build-manifest.toml 缺少 app_wheel 字段")
    if not _is_safe_relpath(app_wheel):
        raise SystemExit(f"[ERROR] build-manifest.toml 的 app_wheel 是非法路径（可能是目录穿越）：{app_wheel!r}")
    if not (wheelhouse_dir / app_wheel).is_file():
        raise SystemExit(f"[ERROR] wheelhouse/ 中不存在 app_wheel：{app_wheel}")
    if not app_version:
        raise SystemExit("[ERROR] build-manifest.toml 缺少 version 字段")
    return wheelhouse_dir, app_wheel, _wheel_distribution_name(app_wheel), app_version, app_variant


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", required=True)
    parser.add_argument("--platform", required=True, help="例如 linux/arm64 或 darwin/arm64")
    parser.add_argument("--python-tag", required=True, help="例如 cp314；darwin 本机 smoke 用其选择解释器")
    parser.add_argument(
        "--smoke-image",
        required=True,
        help="Linux：与 (platform, python_tag) 匹配的镜像引用；darwin/*：可为 host-native 占位",
    )
    parser.add_argument(
        "--import-check",
        action="append",
        default=[],
        required=True,
        help="安装完成后要 import 的顶层模块名，可重复传入；必须显式配置",
    )
    parser.add_argument(
        "--assert-absent",
        action="append",
        default=[],
        help="断言安装后不存在的顶层模块名（importlib.util.find_spec 为 None），可重复传入；用于 CPU variant 断言 GPU 本地模型依赖不存在",
    )
    parser.add_argument(
        "--assert-present",
        action="append",
        default=[],
        help="断言安装后必须存在的顶层模块名（importlib.util.find_spec 不为 None），可重复传入；用于 GPU variant 确认本地模型依赖确实被安装",
    )
    args = parser.parse_args()

    package_path = Path(args.package)
    if not package_path.is_file():
        raise SystemExit(f"[ERROR] 未找到最终包：{package_path}")

    for module in (*args.import_check, *args.assert_absent, *args.assert_present):
        if not MODULE_NAME_PATTERN.match(module):
            raise SystemExit(f"[ERROR] 模块名参数非法（只允许点分隔的 Python 标识符）：{module!r}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root = _extract_package_root(package_path, tmp_path)
        wheelhouse_dir, app_wheel, package_name, package_version, package_variant = _resolve_app_wheel(root)

        package_spec = package_name
        if package_variant:
            package_spec = f"{package_spec}[{package_variant}]"
        package_spec = f"{package_spec}=={package_version}"

        snippet = _build_python_snippet(args.assert_absent, args.assert_present, args.import_check)
        if args.platform.startswith("darwin/"):
            proc = _run_smoke_host(args.python_tag, wheelhouse_dir, package_spec, snippet)
        else:
            proc = _run_smoke_container(args.platform, wheelhouse_dir, package_spec, snippet, args.smoke_image)
        print(proc.stdout)

        if proc.returncode != 0 or "ACPS_RUNTIME_SMOKE_IMPORT_OK" not in proc.stdout:
            print("[ERROR] runtime smoke 校验失败：", file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            raise SystemExit(1)

    summary_parts = [f"import {', '.join(args.import_check)}"] if args.import_check else []
    if args.assert_absent:
        summary_parts.append(f"确认不存在 {', '.join(args.assert_absent)}")
    if args.assert_present:
        summary_parts.append(f"确认存在 {', '.join(args.assert_present)}")
    print(f"[OK] runtime smoke 校验通过：离线安装 {app_wheel} 并成功{'；'.join(summary_parts)}")


if __name__ == "__main__":
    main()
