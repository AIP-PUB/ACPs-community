#!/usr/bin/env python3
"""Reissue one leaf certificate for an existing AIC (no Registry register/delete).

Used by day2 renew-certs to keep Agent AIC stable. Calls:
  acps-cli cert eab fetch --aic …
  acps-cli cert renew --aic … --force --usage … --cert-path … --key-path …

Does not invoke agent save/delete/submit/approve.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


class ReissueError(RuntimeError):
    pass


_URI_AIC_RE = re.compile(r"URI:acps://([^\s,]+)", re.IGNORECASE)
_CN_RE = re.compile(r"Subject:.*\bCN\s*=\s*([^,\n/]+)", re.IGNORECASE)


def extract_aic_from_pem(pem_path: Path) -> str:
    """Parse AIC from leaf PEM (URI:acps://… preferred, else CN)."""
    if not pem_path.is_file():
        raise ReissueError(f"PEM missing: {pem_path}")
    try:
        text = subprocess.check_output(
            ["openssl", "x509", "-in", str(pem_path), "-noout", "-text"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReissueError(f"openssl failed on {pem_path}: {exc}") from exc
    m = _URI_AIC_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _CN_RE.search(text)
    if m:
        return m.group(1).strip()
    raise ReissueError(f"no AIC URI/CN in {pem_path}")


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip()
        raise ReissueError(f"command failed ({exc.returncode}): {' '.join(cmd)}\n{err}") from exc
    return proc.stdout


def reissue_leaf_for_aic(
    *,
    aic: str,
    usage: str,
    cert_path: Path,
    key_path: Path,
    trust_bundle_path: Path | None,
    cli_bin: Path,
    cli_config: Path,
    work_dir: Path | None = None,
) -> str:
    """Renew leaf for ``aic``; return the same AIC string after success."""
    aic = (aic or "").strip()
    if not aic or aic in ("CHANGE_ME", "TODO"):
        raise ReissueError(f"refusing empty/placeholder AIC: {aic!r}")
    if usage not in ("serverAuth", "clientAuth"):
        raise ReissueError(f"invalid usage: {usage}")

    cert_path = cert_path.resolve()
    key_path = key_path.resolve()
    if not key_path.is_file():
        raise ReissueError(f"private key missing (cannot renew without it): {key_path}")

    before = aic
    if cert_path.is_file():
        try:
            pem_aic = extract_aic_from_pem(cert_path)
        except ReissueError as exc:
            # Unreadable/truncated PEM only: continue with SoT AIC + existing key.
            # Do NOT swallow summary≠PEM AIC mismatch — that must fail-stop.
            print(
                f"warn: cannot parse existing PEM AIC ({exc}); "
                f"continuing with SoT AIC={aic}",
                file=sys.stderr,
            )
        else:
            if pem_aic != aic:
                raise ReissueError(
                    f"AIC mismatch: requested={aic!r} pem={pem_aic!r} path={cert_path}"
                )

    work = work_dir or Path(tempfile.mkdtemp(prefix="acps-reissue-"))
    work.mkdir(parents=True, exist_ok=True)
    eab_path = work / f"{aic.replace('/', '_')}-eab.json"

    cli = str(cli_bin)
    cfg = str(cli_config)
    _run(
        [
            cli,
            "--config",
            cfg,
            "cert",
            "eab",
            "fetch",
            "--aic",
            aic,
            "--output",
            str(eab_path),
            "--json",
        ]
    )

    renew_cmd = [
        cli,
        "--config",
        cfg,
        "cert",
        "renew",
        "--aic",
        aic,
        "--eab-file",
        str(eab_path),
        "--usage",
        usage,
        "--force",
        "--cert-path",
        str(cert_path),
        "--key-path",
        str(key_path),
    ]
    if trust_bundle_path is not None:
        renew_cmd.extend(["--trust-bundle-path", str(trust_bundle_path.resolve())])
    _run(renew_cmd)

    if not cert_path.is_file() or cert_path.stat().st_size < 64:
        raise ReissueError(f"renew produced empty cert: {cert_path}")
    after = extract_aic_from_pem(cert_path)
    if after != before:
        raise ReissueError(f"AIC changed during renew: before={before!r} after={after!r}")
    print(f"reissue_ok aic={after} usage={usage} cert={cert_path}")
    return after


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--aic", required=True)
    p.add_argument("--usage", required=True, choices=("serverAuth", "clientAuth"))
    p.add_argument("--cert-path", required=True, type=Path)
    p.add_argument("--key-path", required=True, type=Path)
    p.add_argument("--trust-bundle-path", type=Path, default=None)
    p.add_argument("--cli-bin", required=True, type=Path)
    p.add_argument("--cli-config", required=True, type=Path)
    p.add_argument("--work-dir", type=Path, default=None)
    args = p.parse_args(argv)
    try:
        reissue_leaf_for_aic(
            aic=args.aic,
            usage=args.usage,
            cert_path=args.cert_path,
            key_path=args.key_path,
            trust_bundle_path=args.trust_bundle_path,
            cli_bin=args.cli_bin,
            cli_config=args.cli_config,
            work_dir=args.work_dir,
        )
    except ReissueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
