#!/usr/bin/env python3
"""Assert ACS public HTTPS endpoints are not loopback; optional host-mode AMQP ban on compose DNS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
COMPOSE_DNS = frozenset({"rabbitmq", "postgresql", "redis", "registry_server", "mq_auth_server"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument(
        "--advertise-host",
        default="",
        help="Allowed public-plane host (127.0.0.1 ok only when this matches)",
    )
    p.add_argument(
        "--forbid-compose-dns",
        action="store_true",
        help="Fail if any endPoint host is a Compose service name (host-mode)",
    )
    p.add_argument(
        "--allow-amqp-loopback",
        action="store_true",
        help="Do not fail on AMQP loopback hosts (rare; prefer explicit amqp host)",
    )
    args = p.parse_args(argv)
    advertise = (args.advertise_host or "").strip().lower()

    errors: list[str] = []
    checked = 0
    for root in args.paths:
        files: list[Path] = []
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = (
                sorted(root.glob("**/acs.json"))
                + sorted(root.glob("scenario/expert/*/*.json"))
                + sorted(root.glob("expert/*/*.json"))
            )
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or "endPoints" not in data:
                continue
            checked += 1
            for ep in data.get("endPoints") or []:
                if not isinstance(ep, dict):
                    continue
                url = str(ep.get("url") or "")
                if not url:
                    continue
                transport = str(ep.get("transport") or "").upper()
                host = (urlsplit(url).hostname or "").lower()
                scheme = urlsplit(url).scheme.lower()
                is_amqp = transport == "AMQP" or scheme in ("amqp", "amqps")
                if not is_amqp:
                    # localhost hostname never OK for public ACS; 127.0.0.1 only if advertise says so.
                    if host == "localhost" or host == "::1":
                        errors.append(f"{path}: public endpoint uses loopback hostname: {url}")
                    elif host == "127.0.0.1" and host != advertise:
                        errors.append(f"{path}: public endpoint uses 127.0.0.1 but advertise is {advertise!r}: {url}")
                if is_amqp and host in LOOPBACK and not args.allow_amqp_loopback:
                    if not (advertise and host == advertise):
                        errors.append(f"{path}: AMQP endpoint still loopback: {url}")
                if args.forbid_compose_dns and host in COMPOSE_DNS:
                    errors.append(f"{path}: compose DNS forbidden: {url}")

    if checked == 0:
        print("assert_acs: no ACS files found", file=sys.stderr)
        return 2
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        print(f"assert_acs_failed count={len(errors)} checked={checked}", file=sys.stderr)
        return 1
    print(f"assert_acs_ok checked={checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
