#!/usr/bin/env bash
# Export locally loaded acps/* images to artifacts/images/*.image.tar.gz for stage_artifact.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/artifacts/images"
mkdir -p "$OUT"
PY="${ROOT}/.venv-tools/bin/python"
MANIFEST="${ROOT}/release-manifest.toml"

"$PY" - <<'PY' "$MANIFEST" "$OUT"
import sys, tomllib, subprocess
from pathlib import Path
manifest = Path(sys.argv[1])
out = Path(sys.argv[2])
data = tomllib.loads(manifest.read_text())
for key, meta in data.get("images", {}).items():
    tag = meta["tag"]
    dest = out / meta["file"]
    # skip if exists and non-empty
    if dest.exists() and dest.stat().st_size > 0:
        print(f"skip existing {dest.name}")
        continue
    print(f"export {tag} -> {dest.name}")
    # docker save then gzip
    tmp = dest.with_suffix(".tar")
    subprocess.check_call(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL)
    with open(tmp, "wb") as fh:
        subprocess.check_call(["docker", "save", tag], stdout=fh)
    subprocess.check_call(["gzip", "-f", str(tmp)])
    # gzip renames to.tar.gz; rename to.image.tar.gz
    gz = Path(str(tmp) + ".gz")
    gz.rename(dest)
    print(f"wrote {dest}")
PY
