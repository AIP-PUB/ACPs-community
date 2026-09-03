#!/usr/bin/env python3
"""基础设施镜像单目标构建准备。

不消费应用最终发布包；只从 image-inputs.lock 解析上游基础镜像 digest，决定要
使用哪个 Dockerfile 目录（`infra/<id>/` 如果存在，否则退回通用 `infra/wrapper/`），
并输出编排 shell 脚本（build-infra-image.sh）需要的构建参数、镜像 tag、文件名、
smoke 命令。

用法：
  python3 build_infra_image.py prepare \
      --id postgres-pgvector --kind derived \
      --upstream-version 17-bookworm --acps-version 2.2.0 --platform linux/arm64 \
      --lock <image-inputs.lock> [--lock <path> ...] \
      --infra-dir <image-packaging/infra 目录> \
      --smoke-commands <image-packaging/infra/smoke-commands.toml> \
      > vars.sh
  source vars.sh
"""

from __future__ import annotations

import argparse
import shlex
import tomllib
from pathlib import Path

from common import (
    KNOWN_DOCKER_PLATFORMS,
    docker_platform_to_slug,
    infra_image_filename,
    infra_image_tag,
    is_safe_path_component,
    validate_id,
)
from image_inputs import load_image_inputs_lock, resolve_infra_base


def _sh_assign(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def _split_image_ref(image_ref: str) -> tuple[str, str]:
    """把 "repo:tag@sha256:xxx" 拆成 (repo:tag, sha256:xxx)；lock 文件里的值已经在
     由 image_inputs.validate_lock 校验过这个形状，这里不重复做格式校验。"""
    name_part, _, digest_part = image_ref.partition("@")
    return name_part, digest_part


def _resolve_smoke_command(smoke_commands_path: Path, infra_id: str) -> str:
    if not smoke_commands_path.is_file():
        raise SystemExit(f"[ERROR] 未找到 smoke-commands.toml：{smoke_commands_path}")
    data = tomllib.loads(smoke_commands_path.read_text(encoding="utf-8"))
    command = data.get("commands", {}).get(infra_id)
    if not command:
        raise SystemExit(f"[ERROR] smoke-commands.toml 中没有 infra id={infra_id!r} 的显式声明")
    return command


def prepare(args: argparse.Namespace) -> None:
    if args.platform not in KNOWN_DOCKER_PLATFORMS:
        raise SystemExit(f"[ERROR] 未知的 Docker platform：{args.platform!r}")
    # --id / --upstream-version / --acps-version 都会直接拼进镜像 tag 和输出文件名
    # （见 common.infra_image_filename）；即便是本工具自己的 CLI 参数，也在这里做
    # 一次校验，防止误传路径穿越字符（upstream_version/acps_version 允许出现
    # 大小写字母、数字、点号等——例如 minio 的 "2025-04-22T22-12-26Z"——所以只做
    # "安全路径 segment" 校验，不套用更严格的 id 标识符正则）。
    validate_id("--id", args.id)
    if not is_safe_path_component(args.upstream_version):
        raise SystemExit(f"[ERROR] --upstream-version 包含非法字符（可能是目录穿越）：{args.upstream_version!r}")
    if not is_safe_path_component(args.acps_version):
        raise SystemExit(f"[ERROR] --acps-version 包含非法字符（可能是目录穿越）：{args.acps_version!r}")

    lock_paths = [Path(p) for p in args.lock]
    lock = load_image_inputs_lock(lock_paths)
    upstream_image = resolve_infra_base(lock, args.id, args.platform)
    upstream_image_name, upstream_digest = _split_image_ref(upstream_image)

    infra_dir = Path(args.infra_dir)
    per_id_dir = infra_dir / args.id
    if (per_id_dir / "Dockerfile").is_file():
        dockerfile_dir = per_id_dir
    else:
        if args.kind == "derived":
            raise SystemExit(
                f"[ERROR] infra id={args.id!r} 声明为 derived，但找不到专属 Dockerfile：{per_id_dir}/Dockerfile"
                "（derived 镜像必须真正改动内容，不能退回通用 label-only wrapper）"
            )
        dockerfile_dir = infra_dir / "wrapper"
    if not (dockerfile_dir / "Dockerfile").is_file():
        raise SystemExit(f"[ERROR] 找不到 infra id={args.id!r} 对应的 Dockerfile：{dockerfile_dir}/Dockerfile")

    platform_slug = docker_platform_to_slug(args.platform)
    image_tag = infra_image_tag(args.id, args.upstream_version, args.acps_version, platform_slug)
    image_filename = infra_image_filename(args.id, args.upstream_version, args.acps_version, platform_slug)

    smoke_command = _resolve_smoke_command(Path(args.smoke_commands), args.id)

    lines = [
        _sh_assign("UPSTREAM_IMAGE", upstream_image),
        _sh_assign("UPSTREAM_IMAGE_NAME", upstream_image_name),
        _sh_assign("UPSTREAM_DIGEST", upstream_digest),
        _sh_assign("DOCKERFILE_DIR", str(dockerfile_dir)),
        _sh_assign("PLATFORM_SLUG", platform_slug),
        _sh_assign("IMAGE_TAG", image_tag),
        _sh_assign("IMAGE_FILENAME", image_filename),
        _sh_assign("SMOKE_COMMAND", smoke_command),
    ]
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_cmd = subparsers.add_parser("prepare", help="解析构建参数、tag、文件名、smoke 命令")
    prepare_cmd.add_argument("--id", required=True)
    prepare_cmd.add_argument("--kind", choices=("wrapper", "derived"), required=True)
    prepare_cmd.add_argument("--upstream-version", required=True)
    prepare_cmd.add_argument("--acps-version", required=True)
    prepare_cmd.add_argument("--platform", required=True)
    prepare_cmd.add_argument("--lock", action="append", required=True)
    prepare_cmd.add_argument("--infra-dir", required=True)
    prepare_cmd.add_argument("--smoke-commands", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)


if __name__ == "__main__":
    main()
