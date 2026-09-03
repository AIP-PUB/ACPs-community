#!/usr/bin/env python3
"""修正已安装 wheel 的已知平台 tag 元数据异常。

当前只处理 discovery-server gpu-arm64 链路里实际命中的一个上游问题：

- `nvidia-cusparselt-cu13` 的 aarch64 wheel 文件名是
  `...manylinux2014_aarch64.whl`
- 但其内嵌的 `.dist-info/WHEEL` 却写成了
  `Tag: py3-none-manylinux2014_sbsa`

`pip install` 依据文件名仍能安装成功，但 `pip check` 会读取安装后 dist-info/WHEEL
里的 Tag，并据此把它判定成"当前平台不支持"。这里在离线安装完成后、执行
`pip check` 之前，把这个 vendor wheel 的内部 Tag 规范化为与文件名一致的
`manylinux2014_aarch64`，同时同步更新 RECORD 中 WHEEL 条目的 hash/size。
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import sys
import sysconfig
from pathlib import Path

BROKEN_TAG = "Tag: py3-none-manylinux2014_sbsa"
FIXED_TAG = "Tag: py3-none-manylinux2014_aarch64"


def _default_purelib() -> Path:
    return Path(sysconfig.get_paths()["purelib"])


def _is_linux_aarch64() -> bool:
    platform_tag = sysconfig.get_platform().replace("-", "_").lower()
    return sys.platform == "linux" and platform_tag.endswith("aarch64")


def _sha256_record_field(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


def _rewrite_record(dist_info_dir: Path, wheel_bytes: bytes) -> None:
    record_path = dist_info_dir / "RECORD"
    if not record_path.is_file():
        return

    wheel_relpath = f"{dist_info_dir.name}/WHEEL"
    record_relpath = f"{dist_info_dir.name}/RECORD"
    rows: list[list[str]] = []
    updated = False

    with record_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            path = row[0]
            if path == wheel_relpath:
                rows.append([path, _sha256_record_field(wheel_bytes), str(len(wheel_bytes))])
                updated = True
            elif path == record_relpath:
                rows.append([path, "", ""])
                updated = True
            else:
                rows.append(row)

    if not updated:
        return

    with record_path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)


def normalize_known_wheels(purelib: Path) -> list[str]:
    patched: list[str] = []

    for dist_info_dir in sorted(purelib.glob("nvidia_cusparselt_cu13-*.dist-info")):
        wheel_path = dist_info_dir / "WHEEL"
        if not wheel_path.is_file():
            continue

        original = wheel_path.read_text(encoding="utf-8")
        if BROKEN_TAG not in original:
            continue

        normalized = original.replace(BROKEN_TAG, FIXED_TAG)
        wheel_path.write_text(normalized, encoding="utf-8")
        _rewrite_record(dist_info_dir, normalized.encode("utf-8"))
        patched.append(dist_info_dir.name)

    return patched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purelib",
        default="",
        help="显式指定 site-packages 目录；默认取当前解释器的 sysconfig purelib",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略当前解释器平台判断，强制执行；仅供本地测试/调试",
    )
    args = parser.parse_args()

    if not args.force and not _is_linux_aarch64():
        return

    purelib = Path(args.purelib) if args.purelib else _default_purelib()
    patched = normalize_known_wheels(purelib)
    if patched:
        print(f"[INFO] 已修正已安装 wheel 元数据：{', '.join(patched)}")


if __name__ == "__main__":
    main()
