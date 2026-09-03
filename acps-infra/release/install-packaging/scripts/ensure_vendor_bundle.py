#!/usr/bin/env python3
"""Ensure host-mode vendor artifacts exist in a cache directory.

Reads baseline-matrix.toml [vendor.*]: for each required artifact, if the
expected file is present and sha256 matches → keep; else download from url
(with optional fetch transform), write to cache, verify sha256.

Placeholders in url/file/glob: {arch} (amd64|arm64), {platform} (linux-amd64),
{opensearch_arch} (x64|arm64), {deb_arch} (amd64|arm64).

fetch modes:
  direct (default)  — save download bytes as file
  clickhouse_flat   — from common-static tgz → flat clickhouse{,-server,-client}
  minio_wrap        — wrap bare binary into minio-…/{arch}/minio tarball
  fluentbit_deb     — extract .deb → bin/ etc/ lib/ layout tarball

Offline: --offline refuses network; missing/bad cache → non-zero exit.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


def _arch_tokens(arch: str) -> dict[str, str]:
    if arch not in ("amd64", "arm64"):
        raise SystemExit(f"[ERROR] arch 须为 amd64 或 arm64，收到：{arch}")
    return {
        "arch": arch,
        "platform": f"linux-{arch}",
        "opensearch_arch": "x64" if arch == "amd64" else "arm64",
        "deb_arch": arch,
    }


def _expand(s: str, tokens: dict[str, str]) -> str:
    out = s
    for k, v in tokens.items():
        out = out.replace("{" + k + "}", v)
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_sha256(meta: dict, arch: str) -> str:
    specific = str(meta.get(f"sha256_{arch}", "") or "").strip()
    if specific:
        return specific
    return str(meta.get("sha256", "") or "").strip()


def _resolve_url(meta: dict, tokens: dict[str, str]) -> str:
    arch = tokens["arch"]
    specific = str(meta.get(f"url_{arch}", "") or "").strip()
    if specific:
        return _expand(specific, tokens)
    url = str(meta.get("url", "") or "").strip()
    if not url:
        return ""
    return _expand(url, tokens)


def _resolve_expected_name(meta: dict, tokens: dict[str, str]) -> str:
    file_pat = str(meta.get("file", "") or "").strip()
    if file_pat:
        return _expand(file_pat, tokens)
    glob_pat = str(meta.get("glob", "") or "").strip()
    if glob_pat:
        # Prefer expanded glob as a concrete name when it has no wildcards left.
        expanded = _expand(glob_pat, tokens)
        if "*" not in expanded and "?" not in expanded and "[" not in expanded:
            return expanded
    raise SystemExit(
        f"[ERROR] [vendor] 无法推导缓存文件名：需 file= 或无通配的 glob=（当前 arch={tokens['arch']}）"
    )


def _download(url: str, dest: Path, *, timeout: int = 600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "acps-ensure-vendor-bundle/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 1024)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[ERROR] 下载失败 HTTP {e.code}：{url}") from e
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[ERROR] 下载失败：{url}（{e}）") from e
    # Reject obvious HTML error pages saved as "success".
    head = tmp.read_bytes()[:200].lstrip()
    if head.startswith(b"<!DOCTYPE") or head.startswith(b"<html"):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[ERROR] 下载结果是 HTML 而非制品：{url}")
    tmp.replace(dest)


def _deterministic_targz(out_path: Path, members: list[tuple[str, Path]]) -> None:
    """Write gzip tar with fixed mtime/uid and sorted names (reproducible sha256)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for arcname, src in sorted(members, key=lambda x: x[0]):
            if src.is_symlink():
                info = tarfile.TarInfo(name=arcname)
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(src)
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                info.mtime = 0
                tar.addfile(info)
            elif src.is_file():
                info = tar.gettarinfo(str(src), arcname=arcname)
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                info.mtime = 0
                with src.open("rb") as f:
                    tar.addfile(info, f)
            else:
                raise SystemExit(f"[ERROR] deterministic tar 仅支持文件/符号链接：{src}")
    raw = buf.getvalue()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out_path.open("wb"), mtime=0) as gz:
        gz.write(raw)


def _fetch_direct(url: str, dest: Path) -> None:
    _download(url, dest)


def _fetch_clickhouse_flat(url: str, dest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="acps-ch-") as td:
        td_path = Path(td)
        upstream = td_path / "upstream.tgz"
        _download(url, upstream)
        extract = td_path / "extract"
        extract.mkdir()
        with tarfile.open(upstream, "r:*") as tar:
            tar.extractall(extract)
        binary = None
        for p in extract.rglob("clickhouse"):
            if p.is_file() and not p.is_symlink() and p.name == "clickhouse":
                # Prefer usr/bin/clickhouse
                binary = p
                if "usr/bin" in str(p).replace("\\", "/"):
                    break
        if binary is None:
            raise SystemExit(f"[ERROR] clickhouse common-static 中未找到 clickhouse 二进制：{url}")
        stage = td_path / "stage"
        stage.mkdir()
        shutil.copy2(binary, stage / "clickhouse")
        (stage / "clickhouse").chmod(0o755)
        for name in ("clickhouse-server", "clickhouse-client"):
            link = stage / name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to("clickhouse")
        _deterministic_targz(
            dest,
            [
                ("clickhouse", stage / "clickhouse"),
                ("clickhouse-server", stage / "clickhouse-server"),
                ("clickhouse-client", stage / "clickhouse-client"),
            ],
        )


def _fetch_minio_wrap(url: str, dest: Path, *, arch: str, version_label: str) -> None:
    with tempfile.TemporaryDirectory(prefix="acps-minio-") as td:
        td_path = Path(td)
        binary = td_path / "minio"
        _download(url, binary)
        binary.chmod(0o755)
        top = f"minio-{version_label}-{arch}"
        inner = td_path / top
        inner.mkdir()
        shutil.copy2(binary, inner / "minio")
        (inner / "minio").chmod(0o755)
        _deterministic_targz(
            dest,
            [
                (f"{top}/minio", inner / "minio"),
            ],
        )


def _fetch_fluentbit_deb(url: str, dest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="acps-fb-") as td:
        td_path = Path(td)
        deb = td_path / "fluent-bit.deb"
        _download(url, deb)
        extract = td_path / "debroot"
        extract.mkdir()
        # Prefer dpkg-deb; fall back to ar+tar.
        if shutil.which("dpkg-deb"):
            subprocess.run(["dpkg-deb", "-x", str(deb), str(extract)], check=True)
        else:
            subprocess.run(["ar", "x", str(deb)], cwd=td_path, check=True)
            data = next(td_path.glob("data.tar.*"))
            with tarfile.open(data, "r:*") as tar:
                tar.extractall(extract)
        bin_src = extract / "opt" / "fluent-bit" / "bin" / "fluent-bit"
        lib_src = extract / "lib" / "fluent-bit" / "libfluent-bit.so"
        etc_src = extract / "etc" / "fluent-bit"
        if not bin_src.is_file():
            raise SystemExit(f"[ERROR] fluent-bit deb 缺少 {bin_src}")
        if not lib_src.is_file():
            raise SystemExit(f"[ERROR] fluent-bit deb 缺少 {lib_src}")
        stage = td_path / "stage"
        (stage / "bin").mkdir(parents=True)
        (stage / "etc").mkdir(parents=True)
        (stage / "lib").mkdir(parents=True)
        shutil.copy2(bin_src, stage / "bin" / "fluent-bit")
        (stage / "bin" / "fluent-bit").chmod(0o755)
        shutil.copy2(lib_src, stage / "lib" / "libfluent-bit.so")
        for name in ("fluent-bit.conf", "parsers.conf", "plugins.conf"):
            src = etc_src / name
            if src.is_file():
                shutil.copy2(src, stage / "etc" / name)
        members: list[tuple[str, Path]] = []
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                members.append((str(p.relative_to(stage)).replace("\\", "/"), p))
        _deterministic_targz(dest, members)


def _variant_meta(meta: dict, *, suffix: str) -> dict | None:
    """Build a synthetic meta for a secondary artifact (e.g. glibc228).

    Expects file_<suffix>, url_<suffix>, and sha256 / sha256_<arch>_<suffix>.
    Returns None if file_<suffix> is absent.
    """
    file_key = f"file_{suffix}"
    file_pat = str(meta.get(file_key, "") or "").strip()
    if not file_pat:
        return None
    out = {
        "file": file_pat,
        "url": str(meta.get(f"url_{suffix}", "") or "").strip(),
        "fetch": str(meta.get("fetch", "direct") or "direct").strip(),
        "version": meta.get("version"),
    }
    # Prefer arch-specific sha256_<arch>_<suffix>, then sha256_<suffix>.
    for arch in ("amd64", "arm64"):
        specific = str(meta.get(f"sha256_{arch}_{suffix}", "") or "").strip()
        if specific:
            out[f"sha256_{arch}"] = specific
    generic = str(meta.get(f"sha256_{suffix}", "") or "").strip()
    if generic:
        out["sha256"] = generic
    return out


def ensure_one(
    key: str,
    meta: dict,
    *,
    cache_dir: Path,
    tokens: dict[str, str],
    offline: bool,
) -> Path:
    arch = tokens["arch"]
    expected_name = _resolve_expected_name(meta, tokens)
    dest = cache_dir / expected_name
    want_sha = _resolve_sha256(meta, arch)
    url = _resolve_url(meta, tokens)
    fetch = str(meta.get("fetch", "direct") or "direct").strip().lower()

    if dest.is_file():
        if want_sha:
            got = _sha256_file(dest)
            if got == want_sha:
                print(f"[ok] {key}: cache hit {dest.name}", file=sys.stderr)
                return dest
            print(
                f"[warn] {key}: cache sha256 不匹配（期望 {want_sha}，实得 {got}），将重新获取",
                file=sys.stderr,
            )
            dest.unlink()
        else:
            print(f"[ok] {key}: cache hit {dest.name}（未配置 sha256，跳过校验）", file=sys.stderr)
            return dest

    if offline:
        raise SystemExit(
            f"[ERROR] [vendor.{key}] 缓存缺失或校验失败，且 --offline：{dest}"
        )
    if not url:
        raise SystemExit(
            f"[ERROR] [vendor.{key}] 缓存缺失且未配置 url / url_{arch}：期望文件 {expected_name}"
        )

    print(f"[fetch] {key}: {url} → {dest.name} (fetch={fetch})", file=sys.stderr)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_prefix = f"acps-vendor-{key.replace('/', '-')}-"
    with tempfile.TemporaryDirectory(prefix=tmp_prefix) as td:
        tmp_dest = Path(td) / expected_name
        if fetch in ("", "direct", "rename"):
            _fetch_direct(url, tmp_dest)
        elif fetch == "clickhouse_flat":
            _fetch_clickhouse_flat(url, tmp_dest)
        elif fetch == "minio_wrap":
            version_label = str(meta.get("version", "unknown") or "unknown").strip()
            _fetch_minio_wrap(url, tmp_dest, arch=arch, version_label=version_label)
        elif fetch in ("fluentbit_deb", "fluent_bit_deb"):
            _fetch_fluentbit_deb(url, tmp_dest)
        else:
            raise SystemExit(f"[ERROR] [vendor.{key}] 未知 fetch={fetch}")

        got = _sha256_file(tmp_dest)
        if want_sha and got != want_sha:
            raise SystemExit(
                f"[ERROR] [vendor.{key}] 获取后 sha256 不匹配：期望 {want_sha}，实得 {got}（{tmp_dest.name}）"
            )
        if not want_sha:
            print(
                f"[warn] {key}: baseline 未钉 sha256_{arch}/sha256；本次制品 sha256={got}",
                file=sys.stderr,
            )
        shutil.move(str(tmp_dest), str(dest))
    print(f"[ok] {key}: wrote {dest}", file=sys.stderr)
    return dest


def ensure_vendor_entry(
    key: str,
    meta: dict,
    *,
    cache_dir: Path,
    tokens: dict[str, str],
    offline: bool,
) -> None:
    """Ensure primary artifact and optional file_glibc228 secondary."""
    ensure_one(key, meta, cache_dir=cache_dir, tokens=tokens, offline=offline)
    secondary = _variant_meta(meta, suffix="glibc228")
    if secondary is not None:
        ensure_one(
            f"{key}/glibc228",
            secondary,
            cache_dir=cache_dir,
            tokens=tokens,
            offline=offline,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matrix", type=Path, required=True, help="baseline-matrix.toml")
    ap.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    ap.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="vendor 缓存目录（默认由调用方设为 install-packaging/.vendor-bundle）",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="禁止下载；缓存缺失或 sha 不匹配则失败",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="仅处理指定 vendor key（可重复）；默认全部",
    )
    args = ap.parse_args(argv)

    matrix = tomllib.loads(args.matrix.read_text(encoding="utf-8"))
    vendors = matrix.get("vendor") or {}
    if not vendors:
        print(f"[ERROR] {args.matrix} 无 [vendor.*]", file=sys.stderr)
        return 2

    tokens = _arch_tokens(args.arch)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    keys = list(vendors.keys())
    if args.only:
        unknown = [k for k in args.only if k not in vendors]
        if unknown:
            print(f"[ERROR] --only 未知 key：{unknown}", file=sys.stderr)
            return 2
        keys = list(args.only)

    for key in keys:
        ensure_vendor_entry(
            key,
            vendors[key],
            cache_dir=args.cache_dir,
            tokens=tokens,
            offline=args.offline,
        )

    print(str(args.cache_dir.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
