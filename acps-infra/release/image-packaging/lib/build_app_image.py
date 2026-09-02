#!/usr/bin/env python3
"""应用镜像单目标构建准备。

把 （app_release.py）、（build_context.py）和 （image_inputs.py /
targets.py 的命名规则，来自 common.py）串起来，为单个 {app, platform, python_tag,
variant} 组合生成完整 build context，并把编排 shell 脚本（build-app-image.sh）需要
的构建参数、镜像 tag、文件名、smoke 用的 import/assert 列表输出为可以直接
`source` 的 shell 变量声明。

用法：
  python3 build_app_image.py prepare \
      --app registry-server --platform linux/arm64 --python-tag cp314 [--variant cpu] \
      --package <final-release.tar.gz> \
      --strategy startup-strategies.toml \
      --lock <image-inputs.lock> [--lock <path> ...] \
      --build-context <dir> \
      --app-dockerfile-dir <image-packaging/app 目录> \
      > vars.sh
  source vars.sh
"""

from __future__ import annotations

import argparse
import shlex
import shutil
from pathlib import Path

from app_release import inspect_app_release
from build_context import generate_build_context, load_startup_strategy
from common import KNOWN_DOCKER_PLATFORMS, app_image_filename, app_image_tag, docker_platform_to_slug, validate_id
from image_inputs import load_image_inputs_lock, resolve_python_runtime


def _sh_assign(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def prepare(args: argparse.Namespace) -> None:
    if args.platform not in KNOWN_DOCKER_PLATFORMS:
        raise SystemExit(f"[ERROR] 未知的 Docker platform：{args.platform!r}")
    # --app 直接参与镜像 tag 和输出文件名拼接（见 common.app_image_filename）；即便
    # 是本工具自己的 CLI 参数，也在这里做一次身份标识符校验，防止误传路径穿越字符。
    validate_id("--app", args.app)

    build_context_dir = Path(args.build_context)
    if build_context_dir.exists():
        shutil.rmtree(build_context_dir)
    build_context_dir.mkdir(parents=True)

    release_info = inspect_app_release(
        Path(args.package), build_context_dir, args.platform, args.python_tag
    )
    if release_info.app != args.app:
        raise SystemExit(
            f"[ERROR] --app={args.app!r} 与应用最终发布包 build-manifest.toml 的 app={release_info.app!r} 不一致"
        )
    variant = args.variant or ""
    if release_info.variant != variant:
        raise SystemExit(
            f"[ERROR] --variant={variant!r} 与应用最终发布包 build-manifest.toml 的 variant={release_info.variant!r} 不一致"
        )

    strategy = load_startup_strategy(Path(args.strategy), args.app)
    components = generate_build_context(
        build_context_dir / "app-release", Path(args.strategy), build_context_dir, args.app
    )

    app_dockerfile_dir = Path(args.app_dockerfile_dir)
    shutil.copy2(app_dockerfile_dir / "Dockerfile", build_context_dir / "Dockerfile")
    shutil.copy2(app_dockerfile_dir / "entrypoint.sh", build_context_dir / "entrypoint.sh")
    shutil.copy2(
        app_dockerfile_dir / "normalize_installed_wheels.py",
        build_context_dir / "normalize_installed_wheels.py",
    )

    lock_paths = [Path(p) for p in args.lock]
    lock = load_image_inputs_lock(lock_paths)
    python_runtime_image = resolve_python_runtime(lock, args.platform, args.python_tag)

    platform_slug = docker_platform_to_slug(args.platform)
    image_tag = app_image_tag(args.app, release_info.version, platform_slug, variant)
    image_filename = app_image_filename(args.app, release_info.version, platform_slug, variant)

    if variant == "gpu":
        assert_present = strategy.gpu_assert_present
        assert_absent: list[str] = []
    elif variant == "cpu":
        assert_present = []
        assert_absent = strategy.cpu_assert_absent
    else:
        assert_present = []
        assert_absent = []

    # 只有当前 variant 在 startup-strategies.toml 里显式声明了对应 pip extra 时才
    # 安装 "{app_wheel}[{extra}]"；否则安装 app_wheel 本身，不追加任何 extra
    # （例如 discovery-server 的 variant=cpu 没有对应 pip extra，cpu 依赖就是 base
    # 依赖）。这是 用 discovery-server gpu 真实包端到端构建时发现的问题：
    # 单纯 `pip install app_wheel` 不会拉入 optional-dependencies 里的 torch/
    # FlagEmbedding，即使它们已经在 wheelhouse/ 中。
    app_wheel_extra = strategy.variant_pip_extras.get(variant, "") if variant else ""

    lines = [
        _sh_assign("APP_VERSION", release_info.version),
        _sh_assign("APP_WHEEL", release_info.app_wheel),
        _sh_assign("APP_WHEEL_EXTRA", app_wheel_extra),
        _sh_assign("APP_RELEASE_SHA256", release_info.tar_sha256),
        _sh_assign("PYTHON_RUNTIME_IMAGE", python_runtime_image),
        _sh_assign("PLATFORM_SLUG", platform_slug),
        _sh_assign("IMAGE_TAG", image_tag),
        _sh_assign("IMAGE_FILENAME", image_filename),
        _sh_assign("IMPORT_CHECKS", " ".join(strategy.import_checks)),
        _sh_assign("ASSERT_PRESENT", " ".join(assert_present)),
        _sh_assign("ASSERT_ABSENT", " ".join(assert_absent)),
        _sh_assign("COMPONENT_IDS", " ".join(c.id for c in components)),
    ]
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_cmd = subparsers.add_parser("prepare", help="生成 build context 并输出构建参数")
    prepare_cmd.add_argument("--app", required=True)
    prepare_cmd.add_argument("--platform", required=True)
    prepare_cmd.add_argument("--python-tag", required=True)
    prepare_cmd.add_argument("--variant", default="")
    prepare_cmd.add_argument("--package", required=True)
    prepare_cmd.add_argument("--strategy", required=True)
    prepare_cmd.add_argument("--lock", action="append", required=True)
    prepare_cmd.add_argument("--build-context", required=True)
    prepare_cmd.add_argument("--app-dockerfile-dir", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)


if __name__ == "__main__":
    main()
