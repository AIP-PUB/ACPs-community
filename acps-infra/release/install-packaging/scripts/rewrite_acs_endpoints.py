#!/usr/bin/env python3
"""Rewrite ACS endPoints + certificate.altNames for advertise / AMQP hosts.

Public-plane HTTPS/JSONRPC/SSE use --advertise-host.
Colocated-plane AMQP uses --amqp-host (caller chooses compose DNS vs group addr).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
AMQP_PLACEHOLDER_HOSTS = LOOPBACK_HOSTS | frozenset({"rabbitmq"})
HTTPS_TRANSPORTS = frozenset({"JSONRPC", "HTTPS", "SSE", "HTTP", "NOTIFICATION"})
AMQP_TRANSPORTS = frozenset({"AMQP"})


def _is_ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _replace_netloc(url: str, new_host: str) -> str:
    parts = urlsplit(url)
    port = parts.port
    # Bracket IPv6 if needed (we only emit advertise/amqp hosts as plain today).
    host_part = new_host
    if ":" in new_host and not new_host.startswith("["):
        host_part = f"[{new_host}]"
    netloc = f"{host_part}:{port}" if port else host_part
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"
    return urlunsplit((parts.scheme, userinfo + netloc, parts.path, parts.query, parts.fragment))


def _inject_alt_names(data: dict[str, Any], advertise: str) -> bool:
    if not advertise:
        return False
    cert = data.setdefault("certificate", {})
    if not isinstance(cert, dict):
        return False
    alt = cert.setdefault("altNames", {})
    if not isinstance(alt, dict):
        return False
    dns = list(alt.get("dns") or [])
    ip = list(alt.get("ip") or [])
    dirty = False
    if _is_ipv4(advertise):
        if advertise not in ip:
            ip.append(advertise)
            dirty = True
    else:
        if advertise not in dns:
            dns.append(advertise)
            dirty = True
    for extra_dns in ("localhost",):
        if extra_dns not in dns:
            dns.append(extra_dns)
            dirty = True
    for extra_ip in ("127.0.0.1",):
        if extra_ip not in ip:
            ip.append(extra_ip)
            dirty = True
    if dirty:
        alt["dns"] = dns
        alt["ip"] = ip
    return dirty


def rewrite_acs(
    data: dict[str, Any],
    *,
    advertise_host: str,
    amqp_host: str,
) -> tuple[int, int, int, int]:
    """Returns (https_count, amqp_count, active_count, altnames_count) of changes in this doc."""
    https_n = amqp_n = active_n = alt_n = 0
    advertise = (advertise_host or "").strip()
    amqp = (amqp_host or "").strip()
    endpoints = data.get("endPoints")
    if isinstance(endpoints, list):
        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            url = str(ep.get("url") or "")
            if not url:
                continue
            transport = str(ep.get("transport") or "").upper()
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            # Normalize bracket-stripped already by urlsplit.
            if transport in HTTPS_TRANSPORTS or (
                transport not in AMQP_TRANSPORTS
                and parts.scheme.lower() in ("https", "http")
            ):
                if host in LOOPBACK_HOSTS and advertise:
                    ep["url"] = _replace_netloc(url, advertise)
                    https_n += 1
            elif transport in AMQP_TRANSPORTS or parts.scheme.lower() in ("amqps", "amqp"):
                rewrite_hosts = set(AMQP_PLACEHOLDER_HOSTS)
                if advertise:
                    rewrite_hosts.add(advertise.lower())
                if host in rewrite_hosts and amqp and host != amqp.lower():
                    ep["url"] = _replace_netloc(url, amqp)
                    amqp_n += 1
                elif host in rewrite_hosts and amqp and host == amqp.lower():
                    # Already correct host; still count as touched only if we force-write? skip.
                    pass

    if _inject_alt_names(data, advertise):
        alt_n = 1

    aic = (data.get("aic") or "").strip() if isinstance(data.get("aic"), str) else ""
    if aic and data.get("active") is not True:
        data["active"] = True
        active_n = 1

    return https_n, amqp_n, active_n, alt_n


def _collect_paths(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
            continue
        if not root.is_dir():
            continue
        out.extend(sorted(root.glob("**/acs.json")))
        # Leader static ATR partner snapshots (path may already be .../scenario)
        out.extend(sorted(root.glob("scenario/expert/*/*.json")))
        out.extend(sorted(root.glob("expert/*/*.json")))
        out.extend(sorted(root.glob("**/partners/online/*/acs.json")))
    # de-dupe preserve order
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="ACS JSON files and/or directories to scan",
    )
    parser.add_argument("--advertise-host", required=True, help="Public-plane host for HTTPS endpoints")
    parser.add_argument("--amqp-host", required=True, help="Colocated/cross-host AMQP host (caller-chosen)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute changes without writing files",
    )
    args = parser.parse_args(argv)

    advertise = args.advertise_host.strip()
    amqp = args.amqp_host.strip()
    if not advertise:
        print("error: --advertise-host is empty", file=sys.stderr)
        return 2
    if not amqp:
        print("error: --amqp-host is empty", file=sys.stderr)
        return 2

    files = _collect_paths(args.paths)
    if not files:
        print("rewrote_https=0 rewrote_amqp=0 rewrote_active=0 rewrote_altnames=0 files=0")
        return 0

    tot_h = tot_a = tot_act = tot_alt = 0
    files_changed = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — skip non-ACS JSON
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict) or "endPoints" not in data:
            continue
        h, a, act, alt = rewrite_acs(data, advertise_host=advertise, amqp_host=amqp)
        if h or a or act or alt:
            if not args.dry_run:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            files_changed += 1
            kind = []
            if h:
                kind.append(f"https={h}")
            if a:
                kind.append(f"amqp={a}")
            if act:
                kind.append("active")
            if alt:
                kind.append("altnames")
            print(f"rewrote {' '.join(kind)} {path}")
        tot_h += h
        tot_a += a
        tot_act += act
        tot_alt += alt

    print(
        f"rewrote_https={tot_h} rewrote_amqp={tot_a} "
        f"rewrote_active={tot_act} rewrote_altnames={tot_alt} files={files_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
