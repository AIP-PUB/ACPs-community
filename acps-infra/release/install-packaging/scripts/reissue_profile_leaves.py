#!/usr/bin/env python3
"""Reissue all leaf certs for a cert_provision profile without changing AICs.

Static profiles read AICs from summary.json (then PEM URI).
Demo profiles read AICs from staging ACS trees.

Fail-closed if any AIC cannot be resolved or any leaf/key is missing.
Never calls agent save/delete.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from reissue_leaf_for_aic import ReissueError, extract_aic_from_pem, reissue_leaf_for_aic  # noqa: E402

PLACEHOLDER_AICS = frozenset({"", "CHANGE_ME", "TODO"})


@dataclass(frozen=True)
class LeafJob:
    role: str
    aic: str
    usage: str
    cert_path: Path
    key_path: Path
    trust_bundle_path: Path | None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _real_aic(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aic = value.strip()
    if not aic or aic in PLACEHOLDER_AICS:
        return None
    return aic


def _aic_from_summary_or_pem(summary: dict[str, Any], key: str, pem: Path) -> str:
    aic = _real_aic(summary.get(key))
    if aic:
        return aic
    if pem.is_file():
        return extract_aic_from_pem(pem)
    raise ReissueError(f"cannot resolve AIC for {key}: no summary field and no PEM {pem}")


def _require_files(*paths: Path) -> None:
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise ReissueError(f"incomplete leaf materials (missing): {', '.join(missing)}")


def plan_static_registry(profile_dir: Path) -> list[LeafJob]:
    summary_path = profile_dir / "summary.json"
    summary: dict[str, Any] = _load_json(summary_path) if summary_path.is_file() else {}
    server_pem = profile_dir / "server.pem"
    client_pem = profile_dir / "client.pem"
    service_aic = _aic_from_summary_or_pem(summary, "service_aic", server_pem)
    probe_aic = _aic_from_summary_or_pem(summary, "probe_aic", client_pem)
    trust = profile_dir / "trust-bundle.pem"
    _require_files(
        server_pem,
        profile_dir / "server.key",
        client_pem,
        profile_dir / "client.key",
    )
    return [
        LeafJob("service", service_aic, "serverAuth", server_pem, profile_dir / "server.key", trust),
        LeafJob("probe", probe_aic, "clientAuth", client_pem, profile_dir / "client.key", trust),
    ]


def plan_static_mq(profile_dir: Path) -> list[LeafJob]:
    summary_path = profile_dir / "summary.json"
    summary: dict[str, Any] = _load_json(summary_path) if summary_path.is_file() else {}
    server_pem = profile_dir / "server.pem"
    client_pem = profile_dir / "client.pem"
    service_aic = _aic_from_summary_or_pem(summary, "service_aic", server_pem)
    # mq summary uses service_aic + probe_aic (healthcheck)
    probe_aic = _aic_from_summary_or_pem(summary, "probe_aic", client_pem)
    if not _real_aic(summary.get("probe_aic")) and "aic" in summary:
        # older shapes
        pass
    ca_bundle = profile_dir / "acps-root-ca.pem"
    trust = ca_bundle if ca_bundle.is_file() else profile_dir / "trust-bundle.pem"
    _require_files(
        server_pem,
        profile_dir / "server.key",
        client_pem,
        profile_dir / "client.key",
    )
    return [
        LeafJob("service", service_aic, "serverAuth", server_pem, profile_dir / "server.key", trust),
        LeafJob("healthcheck", probe_aic, "clientAuth", client_pem, profile_dir / "client.key", trust),
    ]


def plan_static_rabbitmq(profile_dir: Path) -> list[LeafJob]:
    summary_path = profile_dir / "summary.json"
    summary: dict[str, Any] = _load_json(summary_path) if summary_path.is_file() else {}
    server_pem = profile_dir / "rabbitmq-server.pem"
    aic = _aic_from_summary_or_pem(summary, "aic", server_pem)
    ca_bundle = profile_dir / "acps-root-ca.pem"
    trust = ca_bundle if ca_bundle.is_file() else None
    _require_files(
        server_pem,
        profile_dir / "rabbitmq-server.key",
        profile_dir / "rabbitmq-client.pem",
        profile_dir / "rabbitmq-client.key",
    )
    return [
        LeafJob(
            "server",
            aic,
            "serverAuth",
            server_pem,
            profile_dir / "rabbitmq-server.key",
            trust,
        ),
        LeafJob(
            "client",
            aic,
            "clientAuth",
            profile_dir / "rabbitmq-client.pem",
            profile_dir / "rabbitmq-client.key",
            trust,
        ),
    ]


def plan_static_redis(profile_dir: Path) -> list[LeafJob]:
    summary_path = profile_dir / "summary.json"
    summary: dict[str, Any] = _load_json(summary_path) if summary_path.is_file() else {}
    server_pem = profile_dir / "redis-server.pem"
    aic = _aic_from_summary_or_pem(summary, "aic", server_pem)
    ca_bundle = profile_dir / "acps-root-ca.pem"
    trust = ca_bundle if ca_bundle.is_file() else None
    _require_files(server_pem, profile_dir / "redis-server.key")
    return [
        LeafJob(
            "server",
            aic,
            "serverAuth",
            server_pem,
            profile_dir / "redis-server.key",
            trust,
        ),
    ]


def plan_demo_partner(stage_root: Path) -> list[LeafJob]:
    online = stage_root / "partners" / "online"
    if not online.is_dir():
        raise ReissueError(f"demo-partner staging missing partners/online: {online}")
    jobs: list[LeafJob] = []
    partners = sorted(p for p in online.iterdir() if p.is_dir() and not p.name.startswith("."))
    if len(partners) < 1:
        raise ReissueError(f"no partner dirs under {online}")
    for partner_dir in partners:
        acs_path = partner_dir / "acs.json"
        if not acs_path.is_file():
            raise ReissueError(f"missing ACS: {acs_path}")
        aic = _real_aic(_load_json(acs_path).get("aic"))
        if not aic:
            raise ReissueError(f"partner ACS has no real AIC: {acs_path}")
        trust = partner_dir / "trust-bundle.pem"
        _require_files(
            partner_dir / "server.pem",
            partner_dir / "server.key",
            partner_dir / "client.pem",
            partner_dir / "client.key",
        )
        jobs.append(
            LeafJob(
                f"partner:{partner_dir.name}:server",
                aic,
                "serverAuth",
                partner_dir / "server.pem",
                partner_dir / "server.key",
                trust if trust.is_file() else None,
            )
        )
        jobs.append(
            LeafJob(
                f"partner:{partner_dir.name}:client",
                aic,
                "clientAuth",
                partner_dir / "client.pem",
                partner_dir / "client.key",
                trust if trust.is_file() else None,
            )
        )
    return jobs


def plan_demo_leader(stage_root: Path) -> list[LeafJob]:
    atr = stage_root / "leader" / "atr"
    acs_path = atr / "acs.json"
    if not acs_path.is_file():
        # some layouts use atr/acs.json at stage root
        acs_path = stage_root / "atr" / "acs.json"
        atr = stage_root / "atr"
    if not acs_path.is_file():
        raise ReissueError(f"demo-leader ACS missing under {stage_root}")
    aic = _real_aic(_load_json(acs_path).get("aic"))
    if not aic:
        raise ReissueError(f"leader ACS has no real AIC: {acs_path}")
    trust = atr / "trust-bundle.pem"
    _require_files(atr / "client.pem", atr / "client.key")
    return [
        LeafJob(
            "leader:client",
            aic,
            "clientAuth",
            atr / "client.pem",
            atr / "client.key",
            trust if trust.is_file() else None,
        ),
    ]


STATIC_PLANNERS = {
    "registry-9002": plan_static_registry,
    "mq-auth-server": plan_static_mq,
    "rabbitmq": plan_static_rabbitmq,
    "redis": plan_static_redis,
}

DEMO_PLANNERS = {
    "demo-partner": plan_demo_partner,
    "demo-leader": plan_demo_leader,
}

# control work/certs outdir names
STATIC_OUTDIRS = {
    "registry-9002": "registry-server-9002",
    "mq-auth-server": "mq-auth-server",
    "rabbitmq": "rabbitmq",
    "redis": "redis",
}


def _write_summary_aics(profile: str, profile_dir: Path, jobs: list[LeafJob]) -> None:
    """Refresh AIC fields in summary.json without inventing new schema keys blindly."""
    summary_path = profile_dir / "summary.json"
    data: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            data = _load_json(summary_path)
        except json.JSONDecodeError:
            data = {}
    data["profile"] = data.get("profile") or profile
    data["output_dir"] = str(profile_dir)
    by_role = {j.role: j.aic for j in jobs}
    if profile == "registry-9002":
        data["service_aic"] = by_role.get("service", data.get("service_aic"))
        data["probe_aic"] = by_role.get("probe", data.get("probe_aic"))
    elif profile == "mq-auth-server":
        data["service_aic"] = by_role.get("service", data.get("service_aic"))
        data["probe_aic"] = by_role.get("healthcheck", data.get("probe_aic"))
    elif profile in ("rabbitmq", "redis"):
        # both leaves share one AIC
        data["aic"] = jobs[0].aic if jobs else data.get("aic")
    summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reissue_jobs(
    jobs: list[LeafJob],
    *,
    cli_bin: Path,
    cli_config: Path,
    work_dir: Path,
) -> list[str]:
    if not jobs:
        raise ReissueError("no leaf jobs planned")
    aics: list[str] = []
    for job in jobs:
        print(f"reissue_begin role={job.role} aic={job.aic} usage={job.usage}")
        aic = reissue_leaf_for_aic(
            aic=job.aic,
            usage=job.usage,
            cert_path=job.cert_path,
            key_path=job.key_path,
            trust_bundle_path=job.trust_bundle_path,
            cli_bin=cli_bin,
            cli_config=cli_config,
            work_dir=work_dir / job.role.replace(":", "_"),
        )
        aics.append(aic)
    return aics


def collect_demo_aics(jobs: list[LeafJob]) -> list[str]:
    seen: list[str] = []
    for j in jobs:
        if j.aic not in seen:
            seen.append(j.aic)
    return seen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", required=True, choices=sorted({*STATIC_PLANNERS, *DEMO_PLANNERS}))
    p.add_argument(
        "--profile-dir",
        type=Path,
        help="Static: work/certs/<outdir>. Demo: unused if --stage-dir set.",
    )
    p.add_argument("--stage-dir", type=Path, help="Demo staging root (control work/staging/demo-*)")
    p.add_argument("--certs-root", type=Path, help="Default work/certs root for static outdir resolve")
    p.add_argument("--cli-bin", required=True, type=Path)
    p.add_argument("--cli-config", required=True, type=Path)
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument(
        "--print-aics-only",
        action="store_true",
        help="Plan jobs and print unique AICs as JSON; do not renew",
    )
    args = p.parse_args(argv)

    profile = args.profile
    try:
        if profile in STATIC_PLANNERS:
            profile_dir = args.profile_dir
            if profile_dir is None:
                if args.certs_root is None:
                    raise ReissueError("--profile-dir or --certs-root required for static profiles")
                profile_dir = args.certs_root / STATIC_OUTDIRS[profile]
            jobs = STATIC_PLANNERS[profile](profile_dir)
        else:
            stage = args.stage_dir
            if stage is None:
                raise ReissueError("--stage-dir required for demo profiles")
            jobs = DEMO_PLANNERS[profile](stage)
            profile_dir = stage

        if args.print_aics_only:
            print(json.dumps({"profile": profile, "aics": collect_demo_aics(jobs), "leaves": len(jobs)}))
            return 0

        work = args.work_dir or (profile_dir / ".reissue-work")
        work.mkdir(parents=True, exist_ok=True)
        aics = reissue_jobs(jobs, cli_bin=args.cli_bin, cli_config=args.cli_config, work_dir=work)
        if profile in STATIC_PLANNERS:
            assert args.profile_dir is not None or args.certs_root is not None
            out_dir = args.profile_dir or (args.certs_root / STATIC_OUTDIRS[profile])
            _write_summary_aics(profile, out_dir, jobs)
            # registry probe sidecar copy left to ansible (same as bootstrap)
        print(
            json.dumps(
                {
                    "profile": profile,
                    "reissued_leaves": len(jobs),
                    "aics": sorted(set(aics)),
                    "status": "ok",
                }
            )
        )
    except ReissueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
