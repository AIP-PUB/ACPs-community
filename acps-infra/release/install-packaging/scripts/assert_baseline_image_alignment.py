#!/usr/bin/env python3
"""Fail-closed: host baseline-matrix.toml pins must align with image-side locks.

Design refs (S12 / §8.2.1 / §12):
  - baseline-matrix [vendor.*] / [infra.*] / [python] ↔ image-inputs.lock tags
  - amp_forwarder (fluent-bit) ↔ release-manifest.toml [images.fluent_bit]
  - ansible group_vars / role defaults must not drift from baseline-matrix [python]
    and [infra.postgresql]

Exit 0 on pass; 1 on mismatch (suitable for CI / syntax-check gate).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

INSTALL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = INSTALL_ROOT / "baseline-matrix.toml"
DEFAULT_IMAGE_LOCK = INSTALL_ROOT.parent / "image-packaging" / "image-inputs.lock"
DEFAULT_RELEASE_MANIFEST = INSTALL_ROOT / "release-manifest.toml"
DEFAULT_GROUP_VARS = INSTALL_ROOT / "ansible" / "inventories" / "group_vars" / "all.yml"
DEFAULT_PG_DEFAULTS = INSTALL_ROOT / "ansible" / "roles" / "postgresql" / "defaults" / "main.yml"

# baseline-matrix [vendor.*] → image-inputs.lock [infra_base] infra_id
VENDOR_INFRA_IDS: dict[str, str] = {
    "keycloak": "keycloak",
    "redpanda": "redpanda",
    "victoria_metrics": "victoria-metrics",
    "clickhouse": "clickhouse",
    "minio": "minio",
    "opensearch": "opensearch",
}

# S12 lightweight AMP scope (subset of vendor.*)
S12_VENDOR_KEYS = frozenset({"redpanda", "victoria_metrics", "minio", "amp_forwarder"})

# baseline-matrix [infra.*] → image-inputs.lock [infra_base] infra_id
INFRA_OS_IDS: dict[str, str] = {
    "postgresql": "postgres-pgvector",
    "redis": "redis",
    "rabbitmq": "rabbitmq",
}

_IMAGE_REF_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_FLOATING_UV_RE = re.compile(r"\d+\.x(?:\.\d+)?|\d+\.\d+\.x", re.I)


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"[ERROR] missing file: {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[ERROR] cannot parse TOML {path}: {exc}") from exc


def _image_tag_from_ref(image_ref: str) -> str:
    ref = image_ref.split("@", 1)[0]
    if ":" not in ref:
        raise ValueError(f"no tag in image ref: {image_ref!r}")
    return ref.rsplit(":", 1)[-1]


def _normalize_version_token(version: str) -> str:
    v = version.strip().lstrip("vV")
    m = re.match(r"RELEASE\.(\d{4}-\d{2}-\d{2})", v, re.I)
    if m:
        return m.group(1)
    return v


def _major_prefix(version: str) -> str:
    """Leading numeric dotted prefix (e.g. 26.1.9, 2025-04-22, 17)."""
    v = _normalize_version_token(version)
    m = re.match(r"^(\d+(?:\.\d+)*)", v)
    return m.group(1) if m else v


def _cpython_family_aligns(baseline_version: str, image_tag: str) -> bool:
    """baseline 3.14.2 aligns with image python:3.14-slim (same 3.14 line)."""
    b = _normalize_version_token(baseline_version)
    i = _normalize_version_token(image_tag)
    bm = re.match(r"^(\d+)\.(\d+)", b)
    im = re.match(r"^(\d+)\.(\d+)", i)
    if bm and im:
        return bm.group(1) == im.group(1) and bm.group(2) == im.group(2)
    return _versions_align(baseline_version, image_tag)


def _versions_align(baseline_version: str, image_tag: str) -> bool:
    """True when image tag carries the same pinned main version as baseline."""
    b = _normalize_version_token(baseline_version)
    i = _normalize_version_token(image_tag)
    if i == b:
        return True
    if i.startswith(b + "-") or i.startswith(b + "."):
        return True
    if re.match(r"RELEASE\.", image_tag, re.I) and b in image_tag:
        return True
    # Fallback: compare leading numeric components (17 vs 17-bookworm).
    return _major_prefix(b) == _major_prefix(i) and _major_prefix(b) != ""


def _min_version_satisfied(min_version: str, image_tag: str) -> bool:
    b = _major_prefix(min_version)
    i = _major_prefix(image_tag)
    try:
        b_parts = [int(x) for x in b.split(".")]
        i_parts = [int(x) for x in i.split(".")]
    except ValueError:
        return _versions_align(min_version, image_tag)
    length = min(len(b_parts), len(i_parts))
    return i_parts[:length] >= b_parts[:length]


def _resolve_infra_tag(lock: dict, infra_id: str, platform: str = "linux/amd64") -> str:
    section = lock.get("infra_base", {})
    for plat in (platform, "linux/arm64"):
        key = f"{infra_id},{plat}"
        if key in section:
            ref = section[key]
            if not isinstance(ref, str) or not _IMAGE_REF_RE.match(ref):
                raise SystemExit(f"[ERROR] invalid infra_base {key!r}: {ref!r}")
            return _image_tag_from_ref(ref)
    raise SystemExit(f"[ERROR] image-inputs.lock missing infra_base key for {infra_id!r}")


def _resolve_python_tag(lock: dict, platform: str = "linux/amd64") -> str:
    section = lock.get("python_runtime", {})
    for plat in (platform, "linux/arm64"):
        key = f"{plat},cp314"
        if key in section:
            ref = section[key]
            if not isinstance(ref, str) or not _IMAGE_REF_RE.match(ref):
                raise SystemExit(f"[ERROR] invalid python_runtime {key!r}: {ref!r}")
            return _image_tag_from_ref(ref)
    raise SystemExit("[ERROR] image-inputs.lock missing python_runtime cp314 entry")


def _manifest_image_tag(manifest: dict, image_key: str) -> str:
    images = manifest.get("images", {})
    entry = images.get(image_key)
    if not isinstance(entry, dict):
        raise SystemExit(f"[ERROR] release-manifest missing [images.{image_key}]")
    tag = str(entry.get("tag", "")).strip()
    if not tag:
        raise SystemExit(f"[ERROR] release-manifest [images.{image_key}] missing tag")
    # acps/fluent-bit:3.2-2.2.0-linux-arm64 → 3.2-2.2.0-linux-arm64
    if ":" in tag:
        tag = tag.rsplit(":", 1)[-1]
    return tag


def _yaml_scalar(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(key)}:\s*\"?([^\"#\n]+?)\"?\s*(?:#.*)?$", text, re.M)
    return m.group(1).strip() if m else None


def _check_python(matrix: dict, lock: dict, errors: list[str]) -> None:
    py = matrix.get("python", {})
    baseline_py = str(py.get("version", "")).strip()
    baseline_uv = str(py.get("uv_version", "")).strip()
    if not baseline_py or not baseline_uv:
        errors.append("[python] baseline-matrix missing version / uv_version")
        return
    if _FLOATING_UV_RE.search(baseline_uv):
        errors.append(f"[python] uv_version must be exact pin, got {baseline_uv!r}")

    image_py_tag = _resolve_python_tag(lock)
    # python:3.14-slim → expect baseline 3.14.x
    if not re.match(r"3\.14(?:\.\d+)?", baseline_py):
        errors.append(f"[python] unexpected baseline CPython pin {baseline_py!r} (expected 3.14.x)")
    if not re.match(r"3\.14", image_py_tag):
        errors.append(
            f"[python] image-inputs cp314 tag {image_py_tag!r} does not match baseline {baseline_py!r}"
        )
    elif not _cpython_family_aligns(baseline_py, image_py_tag):
        errors.append(
            f"[python] baseline-matrix version={baseline_py!r} "
            f"≠ image-inputs tag={image_py_tag!r}"
        )

    gv_py = _yaml_scalar(DEFAULT_GROUP_VARS, "acps_python_version")
    gv_uv = _yaml_scalar(DEFAULT_GROUP_VARS, "acps_uv_version")
    if gv_py != baseline_py:
        errors.append(
            f"[python] group_vars acps_python_version={gv_py!r} "
            f"≠ baseline-matrix {baseline_py!r}"
        )
    if gv_uv != baseline_uv:
        errors.append(
            f"[python] group_vars acps_uv_version={gv_uv!r} "
            f"≠ baseline-matrix {baseline_uv!r}"
        )


def _check_vendor(
    matrix: dict,
    lock: dict,
    manifest: dict,
    errors: list[str],
    *,
    only_s12: bool,
) -> None:
    vendors = matrix.get("vendor", {})
    if not isinstance(vendors, dict):
        errors.append("[vendor] baseline-matrix missing [vendor.*] table")
        return

    for vendor_key, spec in sorted(vendors.items()):
        if only_s12 and vendor_key not in S12_VENDOR_KEYS:
            continue
        if not isinstance(spec, dict):
            continue
        baseline_ver = str(spec.get("version", "")).strip()
        if not baseline_ver:
            errors.append(f"[vendor.{vendor_key}] missing version pin")
            continue

        # host-only runtime deps (e.g. Temurin JRE for Keycloak) — no image twin
        if spec.get("align_with_image") is False:
            continue

        if vendor_key == "amp_forwarder":
            image_tag = _manifest_image_tag(manifest, "fluent_bit")
            # acps tag: 3.2-2.2.0-linux-arm64 → compare leading 3.2
            image_ver = image_tag.split("-", 1)[0]
        else:
            infra_id = VENDOR_INFRA_IDS.get(vendor_key)
            if not infra_id:
                errors.append(f"[vendor.{vendor_key}] no image-inputs infra_id mapping")
                continue
            image_ver = _resolve_infra_tag(lock, infra_id)

        if not _versions_align(baseline_ver, image_ver):
            errors.append(
                f"[vendor.{vendor_key}] baseline-matrix version={baseline_ver!r} "
                f"≠ image-side tag={image_ver!r}"
            )


def _check_infra_os(matrix: dict, lock: dict, errors: list[str]) -> None:
    infra = matrix.get("infra", {})
    if not isinstance(infra, dict):
        errors.append("[infra] baseline-matrix missing [infra.*] table")
        return

    pg = infra.get("postgresql", {})
    pg_ver = str(pg.get("version", "")).strip() if isinstance(pg, dict) else ""
    if pg_ver:
        image_tag = _resolve_infra_tag(lock, INFRA_OS_IDS["postgresql"])
        if not _versions_align(pg_ver, image_tag):
            errors.append(
                f"[infra.postgresql] baseline-matrix version={pg_ver!r} "
                f"≠ image-inputs tag={image_tag!r}"
            )
        role_ver = _yaml_scalar(DEFAULT_PG_DEFAULTS, "postgresql_os_major_version")
        if role_ver != pg_ver:
            errors.append(
                f"[infra.postgresql] postgresql/defaults version={role_ver!r} "
                f"≠ baseline-matrix {pg_ver!r}"
            )

    redis = infra.get("redis", {})
    redis_min = str(redis.get("min_version", "")).strip() if isinstance(redis, dict) else ""
    if redis_min:
        image_tag = _resolve_infra_tag(lock, INFRA_OS_IDS["redis"])
        if not _min_version_satisfied(redis_min, image_tag):
            errors.append(
                f"[infra.redis] min_version={redis_min!r} not satisfied by "
                f"image-inputs tag={image_tag!r}"
            )

    rmq = infra.get("rabbitmq", {})
    rmq_min = str(rmq.get("min_version", "")).strip() if isinstance(rmq, dict) else ""
    if rmq_min:
        image_tag = _resolve_infra_tag(lock, INFRA_OS_IDS["rabbitmq"])
        if not _min_version_satisfied(rmq_min, image_tag):
            errors.append(
                f"[infra.rabbitmq] min_version={rmq_min!r} not satisfied by "
                f"image-inputs tag={image_tag!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--image-lock", type=Path, default=DEFAULT_IMAGE_LOCK)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST)
    parser.add_argument(
        "--s12-only",
        action="store_true",
        help="only assert S12 vendor pins (redpanda, victoria_metrics, minio, amp_forwarder)",
    )
    args = parser.parse_args()

    matrix = _load_toml(args.baseline)
    lock = _load_toml(args.image_lock)
    manifest = _load_toml(args.release_manifest)

    errors: list[str] = []
    if not args.s12_only:
        _check_python(matrix, lock, errors)
        _check_infra_os(matrix, lock, errors)
    _check_vendor(matrix, lock, manifest, errors, only_s12=args.s12_only)

    if errors:
        print("[ERROR] baseline ↔ image version alignment failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    scope = "S12 vendor" if args.s12_only else "python + infra + vendor"
    print(f"[OK] baseline-matrix aligns with image-side pins ({scope})")


if __name__ == "__main__":
    main()
