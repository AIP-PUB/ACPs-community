#!/usr/bin/env python3
"""对最终发布包 wheelhouse/ 中的平台相关 wheel 做基线 / 平台 tag 校验。

Linux 目标：跳过所有 `*-none-any.whl`；对剩下的平台相关 wheel 在 audit 容器内执行
`auditwheel show`，解析报告的兼容 manylinux tag，并与本次装配声明的目标基线比较。

Darwin 目标（`--platform darwin/*`）：跳过 manylinux/`auditwheel`；对非 pure 的 wheel
做平台 tag 轻量检查（须含 macosx，且 arch 与目标一致：arm64→arm64/universal2，
amd64→x86_64/universal2/intel）。

可选：用 --deny-package/--require-package 对 wheelhouse/ 中存在的 wheel 文件名做 canonical
包名对比（不是简单字符串 grep），用于 discovery-server 这类有 CPU/GPU variant 依赖差异的
 app：CPU final package 用 --deny-package 确认不包含本地模型推理栈，GPU final package 用
 --require-package 确认它们确实存在。这是设计要求的三层 deny-list 防线之一（lockfile /
wheelhouse / runtime smoke），与其余两层独立。

可选：用 --skip-package-prefix 跳过特定 canonical 包名前缀的 manylinux 基线审计。GPU final
 package 中的 NVIDIA vendor CUDA wheel 会依赖系统级 CUDA 共享库（例如 libnvJitLink.so.13），
 这类 wheel 不适合套用纯 manylinux 基线规则；因此只在 GPU variant 的发布校验里跳过该前缀，
 CPU final package 仍保持全量审计。

本脚本自身只负责在宿主机上编排 `docker run`（Linux）、解析输出、比较基线；不在
本机重新实现 ELF 符号扫描。

用法：
  python3 audit_wheelhouse.py --package <final-release.tar.gz> --platform linux/arm64 \
      --baseline manylinux_2_28 --audit-image acps-audit:manylinux_2_28-arm64

  python3 audit_wheelhouse.py --package <darwin-cli.tar.gz> --platform darwin/arm64 \
      --baseline manylinux_2_28 --audit-image host-native-darwin-tag-check
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

TAG_PATTERN = re.compile(r'"(manylinux\w*)_(?:x86_64|aarch64|i686|armv7l|ppc64le|s390x|universal2)"')

# manylinux 别名 -> (glibc major, glibc minor)。manylinux_X_Y 直接从数字取值。
LEGACY_ALIASES = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def wheel_distribution_name(wheel_filename: str) -> str:
    """从 wheel 文件名解析 PEP 503 canonical distribution 名。

    wheel 文件名约定为
    {distribution}-{version}(-{build tag})?-{python tag}-{abi tag}-{platform tag}.whl，
    各段用 "-" 分隔，而 distribution 段自身内部的非字母数字字符已被打包工具正规化为 "_"，
    因此取第一个 "-" 分隔段就是完整 distribution，不会被切断。
    """
    stem = wheel_filename[: -len(".whl")] if wheel_filename.endswith(".whl") else wheel_filename
    distribution = stem.split("-")[0]
    return re.sub(r"[-_.]+", "-", distribution.lower())


def parse_manylinux_baseline(tag: str) -> tuple[int, int]:
    tag = tag.strip()
    if tag in LEGACY_ALIASES:
        return LEGACY_ALIASES[tag]
    match = re.fullmatch(r"manylinux_(\d+)_(\d+)", tag)
    if match:
        return int(match.group(1)), int(match.group(2))
    raise ValueError(f"无法解析的 manylinux 基线标签：{tag}")


def extract_reported_tags(auditwheel_output: str) -> list[str]:
    return TAG_PATTERN.findall(auditwheel_output)


def _audit_deny_require_packages(
    wheels: list[Path],
    deny_package: list[str],
    deny_package_prefix: list[str],
    require_package: list[str],
    errors: list[str],
) -> None:
    """discovery-server CPU/GPU 依赖分层设计 ：对 wheelhouse/ 中存在的 wheel 文件名做
    canonical 包名对比（不是简单字符串 grep），用于 CPU final package 确认不包含 GPU 本地
    模型推理栈，GPU final package 确认它们确实存在。"""
    if not (deny_package or deny_package_prefix or require_package):
        return

    present_names = {wheel_distribution_name(wheel.name) for wheel in wheels}
    deny_exact = {name.lower() for name in deny_package}
    deny_prefixes = tuple(prefix.lower() for prefix in deny_package_prefix)
    for name in sorted(present_names):
        if name in deny_exact:
            errors.append(f"wheelhouse/ 中出现被禁止的 GPU 依赖：{name}")
        elif deny_prefixes and name.startswith(deny_prefixes):
            errors.append(f"wheelhouse/ 中出现被禁止的 GPU 依赖（前缀匹配）：{name}")
    for required in require_package:
        if required.lower() not in present_names:
            errors.append(f"wheelhouse/ 中缺少必需的 GPU 依赖：{required}")


def _should_skip_baseline_audit(wheel: Path, skip_package_prefix: list[str]) -> bool:
    if not skip_package_prefix:
        return False
    canonical_name = wheel_distribution_name(wheel.name)
    skip_prefixes = tuple(prefix.lower() for prefix in skip_package_prefix)
    return canonical_name.startswith(skip_prefixes)


def _wheel_platform_tags(wheel_filename: str) -> list[str]:
    """PEP 427：文件名末段为 platform tag（可含 '.' 连接的多 tag）。"""
    stem = wheel_filename[: -len(".whl")] if wheel_filename.endswith(".whl") else wheel_filename
    parts = stem.split("-")
    if len(parts) < 5:
        return []
    return parts[-1].split(".")


def _audit_wheel_darwin_platform_tags(wheel: Path, platform: str, errors: list[str]) -> None:
    """darwin/*：非 pure wheel 须带 macosx tag，且 arch 与目标一致。"""
    arch = platform.rsplit("/", 1)[-1]
    tags = _wheel_platform_tags(wheel.name)
    if not tags:
        errors.append(f"{wheel.name}: 无法解析 platform tag")
        return
    if not any(tag.startswith("macosx") for tag in tags):
        errors.append(f"{wheel.name}: Darwin 包中的平台相关 wheel 缺少 macosx tag（tags={tags}）")
        return
    if arch == "arm64":
        ok = any(("arm64" in tag) or ("universal2" in tag) for tag in tags)
        if not ok:
            errors.append(f"{wheel.name}: 期望 arm64/universal2 macosx tag，实际={tags}")
    elif arch in ("amd64", "x86_64"):
        ok = any(
            ("x86_64" in tag) or ("universal2" in tag) or ("intel" in tag)
            for tag in tags
        )
        if not ok:
            errors.append(f"{wheel.name}: 期望 x86_64/universal2 macosx tag，实际={tags}")
    else:
        errors.append(f"{wheel.name}: 不支持的 darwin arch：{arch}")


def _audit_wheel_manylinux_baseline(
    wheel: Path,
    platform: str,
    audit_image: str,
    baseline: str,
    target_version: tuple[int, int],
    errors: list[str],
) -> None:
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            "-v",
            f"{wheel.parent}:/wheelhouse:ro",
            audit_image,
            "auditwheel",
            "show",
            f"/wheelhouse/{wheel.name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + "\n" + proc.stderr
    reported_tags = extract_reported_tags(output)
    if not reported_tags:
        errors.append(f"{wheel.name}: 无法从 auditwheel 输出中解析兼容 tag（exit={proc.returncode}）\n{output.strip()}")
        return

    # auditwheel 可能一次报告多个兼容 tag（同一基线的不同别名，或多个候选）
    # 取其中要求最新 glibc 的一个作为这个 wheel 实际的最低兼容基线，
    # 代表最严格情况，避免因为报告顺序而误判通过。单个 wheel 的 tag 解析失败
    # 只记为该 wheel 的一条错误并继续检查其余 wheel，不中断整批校验。
    try:
        reported_version = max(parse_manylinux_baseline(tag) for tag in reported_tags)
        reported_tag = max(reported_tags, key=parse_manylinux_baseline)
    except ValueError as exc:
        errors.append(f"{wheel.name}: {exc}（原始输出：{output.strip()}）")
        return
    if reported_version > target_version:
        errors.append(
            f"{wheel.name}: 实际兼容基线 {reported_tag}（glibc {reported_version}）"
            f"高于目标基线 {baseline}（glibc {target_version}），不满足承诺"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", required=True)
    parser.add_argument("--platform", required=True, help="例如 linux/arm64 或 darwin/arm64")
    parser.add_argument(
        "--baseline",
        required=True,
        help="Linux：manylinux_2_28 / manylinux2014；darwin/* 时忽略该值（仍须传入以保持 CLI 稳定）",
    )
    parser.add_argument(
        "--audit-image",
        required=True,
        help="Linux：预装 auditwheel 的镜像引用；darwin/*：占位字符串即可（不跑 Docker）",
    )
    parser.add_argument(
        "--deny-package",
        action="append",
        default=[],
        help="wheelhouse/ 中不允许出现的 canonical 包名（精确匹配），可重复传入",
    )
    parser.add_argument(
        "--deny-package-prefix",
        action="append",
        default=[],
        help="wheelhouse/ 中不允许出现的 canonical 包名前缀（如 nvidia-），可重复传入",
    )
    parser.add_argument(
        "--require-package",
        action="append",
        default=[],
        help="wheelhouse/ 中必须存在的 canonical 包名（精确匹配），可重复传入",
    )
    parser.add_argument(
        "--skip-package-prefix",
        action="append",
        default=[],
        help="跳过 manylinux 基线审计的 canonical 包名前缀（如 nvidia-），可重复传入",
    )
    args = parser.parse_args()

    package_path = Path(args.package)
    if not package_path.is_file():
        raise SystemExit(f"[ERROR] 未找到最终包：{package_path}")

    is_darwin = args.platform.startswith("darwin/")
    target_version: tuple[int, int] | None = None
    if not is_darwin:
        try:
            target_version = parse_manylinux_baseline(args.baseline)
        except ValueError as exc:
            raise SystemExit(f"[ERROR] --baseline 非法：{exc}")

    errors: list[str] = []
    checked = 0
    skipped = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(package_path) as tf:
            tf.extractall(tmp_path, filter="data")

        roots = [p for p in tmp_path.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise SystemExit(f"[ERROR] tar 顶层目录数量异常：{[p.name for p in roots]}")
        wheelhouse_dir = roots[0] / "wheelhouse"
        if not wheelhouse_dir.is_dir():
            raise SystemExit("[ERROR] 最终包缺少 wheelhouse/ 目录")

        wheels = sorted(wheelhouse_dir.glob("*.whl"))
        if not wheels:
            raise SystemExit("[ERROR] wheelhouse/ 下没有任何 wheel 文件")

        _audit_deny_require_packages(wheels, args.deny_package, args.deny_package_prefix, args.require_package, errors)

        for wheel in wheels:
            if wheel.name.endswith("-none-any.whl"):
                skipped += 1
                continue

            if _should_skip_baseline_audit(wheel, args.skip_package_prefix):
                skipped += 1
                continue

            checked += 1
            if is_darwin:
                _audit_wheel_darwin_platform_tags(wheel, args.platform, errors)
            else:
                assert target_version is not None
                _audit_wheel_manylinux_baseline(
                    wheel, args.platform, args.audit_image, args.baseline, target_version, errors
                )

    if is_darwin:
        print(f"[INFO] Darwin 平台 tag 检查：共检查 {checked} 个平台相关 wheel，跳过 {skipped} 个 pure/跳过前缀")
    else:
        print(f"[INFO] 共检查 {checked} 个平台相关 wheel，跳过 {skipped} 个纯 Python wheel")

    if errors:
        label = "Darwin 平台 tag" if is_darwin else "auditwheel 基线"
        print(f"[ERROR] {label}校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    if is_darwin:
        print("[OK] 全部平台相关 wheel 的 Darwin/macosx 平台 tag 检查通过")
    else:
        print(f"[OK] 全部平台相关 wheel 的 manylinux 基线均不高于 {args.baseline}")


if __name__ == "__main__":
    main()
