#!/usr/bin/env python3
"""解析镜像内用户的数字 uid:gid。切勿假设为 1000。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def _image_config_user(image: str) -> str:
    """返回镜像的 Config.User（可为 uid、uid:gid 或名称）。"""
    try:
        out = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Config.User}}"],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] docker image inspect failed for {image}: {exc.stderr}", file=sys.stderr)
        return ""
    return (out or "").strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True)
    p.add_argument("--user", default="")
    args = p.parse_args()
    user = (args.user or "").strip()
    # Config.User 可为 "uid:gid"、"user" 或 "uid"
    if re.fullmatch(r"\d+:\d+", user):
        uid, gid = user.split(":")
        print(json.dumps({"uid": int(uid), "gid": int(gid), "user": user}))
        return 0
    if re.fullmatch(r"\d+", user):
        print(json.dumps({"uid": int(user), "gid": int(user), "user": user}))
        return 0
    if not user:
        # 空 --user 表示「镜像默认用户」（Config.User），非 root。
        # 许多 AMP 镜像（opensearch 等）以非 root 数字 uid 运行。
        user = _image_config_user(args.image)
        if re.fullmatch(r"\d+:\d+", user):
            uid, gid = user.split(":")
            print(json.dumps({"uid": int(uid), "gid": int(gid), "user": user}))
            return 0
        if re.fullmatch(r"\d+", user):
            print(json.dumps({"uid": int(user), "gid": int(user), "user": user}))
            return 0
        if not user:
            user = "root"

    # Config.User / --user 可为 "name:group"（Docker USER 形式）。按名称解析。
    user_name = user.split(":", 1)[0] if ":" in user and not re.fullmatch(r"\d+:\d+", user) else user

    # 优先在镜像内读取 /etc/passwd，不启动应用 entrypoint。
    cmd = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "cat",
        args.image,
        "/etc/passwd",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] failed to read /etc/passwd from {args.image}: {exc.stderr}", file=sys.stderr)
        return 2

    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == user_name:
            print(json.dumps({"uid": int(parts[2]), "gid": int(parts[3]), "user": user_name}))
            return 0

    # 回退：id -u / id -g
    try:
        id_out = subprocess.check_output(
            ["docker", "run", "--rm", "--entrypoint", "id", args.image, user_name],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"[ERROR] cannot resolve user {user_name!r} in image {args.image}: {exc.stderr}",
            file=sys.stderr,
        )
        return 2
    m = re.search(r"uid=(\d+).*gid=(\d+)", id_out)
    if not m:
        print(f"[ERROR] unexpected id output: {id_out!r}", file=sys.stderr)
        return 2
    print(json.dumps({"uid": int(m.group(1)), "gid": int(m.group(2)), "user": user_name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
