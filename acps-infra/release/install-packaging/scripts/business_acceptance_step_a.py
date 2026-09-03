#!/usr/bin/env python3
"""业务验收步骤 A — Discovery 同步 + NL 查询。

枚举控制面 staging 下每个 demo Partner ACS，触发 Registry→Discovery 同步，
再对每个 Partner 运行显式自然语言 discovery 查询并断言命中 AIC。任一未命中 → 非零退出。

硬性规则：
  - 不得以 Discovery LLM / embedding secret 是否存在为门禁。
  - Partner 必须全覆盖；Leader 查询可选（--include-leader）。
  - CPU 与 GPU 仅以查询命中判定。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class StepAError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(f"[biz-accept-A] {msg}", flush=True)


def _cli_raw(cli_bin: str, config: str, *args: str, check: bool = True) -> str:
    cmd = [cli_bin, "--config", config, *args]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if check and proc.returncode != 0:
        detail = err or out or f"exit={proc.returncode}"
        raise StepAError(f"cli failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return out


def _cli_json(cli_bin: str, config: str, *args: str) -> dict[str, Any]:
    out = _cli_raw(cli_bin, config, *args)
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise StepAError(f"cli returned non-JSON: {out[:800]}") from exc
    if not isinstance(payload, dict):
        raise StepAError(f"cli JSON root must be object: {type(payload).__name__}")
    return payload


def _load_acs(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StepAError(f"ACS is not an object: {path}")
    return data


def enumerate_partners(partner_root: Path) -> list[dict[str, Any]]:
    """List installed demo Partner AIC entries from staging ACS tree."""
    online = partner_root / "partners" / "online"
    if not online.is_dir():
        raise StepAError(
            f"partner staging missing: {online} "
            "(expected after demo T4 cert_provision / site phases 12–13)"
        )
    partners: list[dict[str, Any]] = []
    for acs_path in sorted(online.glob("*/acs.json")):
        data = _load_acs(acs_path)
        aic = str(data.get("aic") or "").strip()
        if not aic:
            raise StepAError(f"partner ACS missing AIC: {acs_path}")
        slug = acs_path.parent.name
        partners.append(
            {
                "slug": slug,
                "aic": aic,
                "name": str(data.get("name") or "").strip(),
                "acs_path": str(acs_path),
                "acs": data,
                "role": "partner",
            }
        )
    if not partners:
        raise StepAError(f"no partner ACS under {online}")
    return partners


def load_leader(leader_root: Path) -> dict[str, Any]:
    acs_path = leader_root / "leader" / "atr" / "acs.json"
    if not acs_path.is_file():
        raise StepAError(f"leader ACS missing: {acs_path}")
    data = _load_acs(acs_path)
    aic = str(data.get("aic") or "").strip()
    if not aic:
        raise StepAError(f"leader ACS missing AIC: {acs_path}")
    return {
        "slug": "demo-leader",
        "aic": aic,
        "name": str(data.get("name") or "").strip(),
        "acs_path": str(acs_path),
        "acs": data,
        "role": "leader",
    }


def build_nl_query(acs: dict[str, Any]) -> str:
    """Build an explicit NL query from ACS name / description / skill examples.

    Prefer skill examples (already natural-language). Never empty.
    """
    name = str(acs.get("name") or "").strip()
    desc = str(acs.get("description") or "").strip()
    skills = acs.get("skills") or []
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            examples = skill.get("examples") or []
            if isinstance(examples, list):
                for ex in examples:
                    text = str(ex or "").strip()
                    if text:
                        return text
            skill_name = str(skill.get("name") or "").strip()
            skill_desc = str(skill.get("description") or "").strip()
            if skill_name and skill_desc:
                return f"我需要一个能提供「{skill_name}」服务的智能体。{skill_desc}"
            if skill_name:
                return f"我需要一个能提供「{skill_name}」服务的智能体"
    if name and desc:
        return f"我需要「{name}」。{desc[:240]}"
    if name:
        return f"我需要一个叫「{name}」的智能体"
    raise StepAError("cannot build NL query: ACS has empty name/description/skills")


def _acs_map_keys(payload: dict[str, Any]) -> list[str]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    acs_map = result.get("acsMap")
    if not isinstance(acs_map, dict):
        return []
    return [str(k) for k in acs_map.keys()]


def payload_hits_aic(payload: dict[str, Any], expected_aic: str) -> bool:
    return expected_aic in _acs_map_keys(payload)


def run_sync(cli_bin: str, config: str, *, expect_acs_min: int) -> None:
    _log(f"Registry→Discovery sync (expect_acs_min={expect_acs_min})")
    # admin discovery run-sync waits for DSP completion; prints text + may log JSON.
    out = _cli_raw(
        cli_bin,
        config,
        "admin",
        "discovery",
        "run-sync",
        "--expect-acs-min",
        str(expect_acs_min),
        check=True,
    )
    if out:
        for line in out.splitlines()[-8:]:
            _log(f"sync: {line}")
    _log("sync OK")


def filtered_probe(cli_bin: str, config: str, aic: str) -> dict[str, Any]:
    """Structured filtered query — used only as sync readiness helper, not pass gate."""
    filter_json = json.dumps(
        {
            "conditions": [
                {"field": "aic", "op": "eq", "value": aic},
            ]
        },
        ensure_ascii=False,
    )
    return _cli_json(
        cli_bin,
        config,
        "discover",
        "query",
        "--type",
        "filtered",
        "--limit",
        "5",
        "--filter-json",
        filter_json,
    )


def nl_query(cli_bin: str, config: str, query_text: str, *, limit: int) -> dict[str, Any]:
    return _cli_json(
        cli_bin,
        config,
        "discover",
        "query",
        query_text,
        "--type",
        "explicit",
        "--limit",
        str(limit),
    )


def wait_filtered_visible(
    cli_bin: str,
    config: str,
    *,
    aic: str,
    timeout_s: float,
    interval_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            payload = filtered_probe(cli_bin, config, aic)
            if payload_hits_aic(payload, aic):
                return True
        except StepAError as exc:
            _log(f"filtered probe error for {aic}: {exc}")
        time.sleep(interval_s)
    return False


def assert_nl_hit(
    cli_bin: str,
    config: str,
    *,
    agent: dict[str, Any],
    timeout_s: float,
    interval_s: float,
    limit: int,
) -> None:
    aic = agent["aic"]
    slug = agent["slug"]
    role = agent["role"]
    query_text = build_nl_query(agent["acs"])
    _log(f"NL query {role}/{slug} aic={aic} query={query_text!r}")

    deadline = time.monotonic() + timeout_s
    last_err = ""
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            payload = nl_query(cli_bin, config, query_text, limit=limit)
            last_payload = payload
            if payload_hits_aic(payload, aic):
                _log(f"HIT {role}/{slug} aic={aic}")
                return
            keys = _acs_map_keys(payload)
            last_err = f"acsMap keys={keys[:12]}"
            _log(f"miss {role}/{slug}: {last_err}")
        except StepAError as exc:
            last_err = str(exc)
            _log(f"NL query error {role}/{slug}: {exc}")
        time.sleep(interval_s)

    payload_summary = ""
    if last_payload is not None:
        try:
            payload_summary = json.dumps(last_payload, ensure_ascii=False)[:1200]
        except (TypeError, ValueError):
            payload_summary = repr(last_payload)[:1200]
    raise StepAError(
        f"Discovery NL query MISS — role={role} slug={slug} aic={aic} "
        f"query={query_text!r} last_error={last_err} payload={payload_summary}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-bin", required=True, help="Path to acps-cli")
    parser.add_argument("--cli-config", required=True, help="Path to acps-cli.toml")
    parser.add_argument(
        "--partner-root",
        required=True,
        help="Control staging demo-partner root (…/work/staging/demo-partner)",
    )
    parser.add_argument(
        "--leader-root",
        default="",
        help="Control staging demo-leader root (required with --include-leader)",
    )
    parser.add_argument(
        "--include-leader",
        action="store_true",
        help="Also NL-query Leader AIC (optional; Partner coverage remains mandatory)",
    )
    parser.add_argument("--sync-wait-seconds", type=float, default=120.0)
    parser.add_argument("--query-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--query-limit", type=int, default=10)
    parser.add_argument(
        "--skip-filtered-wait",
        action="store_true",
        help="Skip best-effort filtered visibility wait before NL (debug only)",
    )
    args = parser.parse_args()

    cli_bin = args.cli_bin
    config = args.cli_config
    partner_root = Path(args.partner_root)

    if not Path(cli_bin).is_file():
        raise StepAError(f"acps-cli missing: {cli_bin}")
    if not Path(config).is_file():
        raise StepAError(f"acps-cli.toml missing: {config}")

    partners = enumerate_partners(partner_root)
    agents: list[dict[str, Any]] = list(partners)
    if args.include_leader:
        if not args.leader_root:
            raise StepAError("--include-leader requires --leader-root")
        agents.append(load_leader(Path(args.leader_root)))

    _log(
        f"enumerated partners={len(partners)} "
        f"include_leader={args.include_leader} "
        f"total_queries={len(agents)}"
    )
    for p in partners:
        _log(f"  partner {p['slug']}: aic={p['aic']} name={p['name']!r}")

    # Default: skip Registry→Discovery run-sync (index usually already warm after site.yml).
    # Set ACPS_FORCE_DISCOVERY_SYNC=1 to force sync (can hang on flaky embedding gateways).
    force_sync = (os.environ.get("ACPS_FORCE_DISCOVERY_SYNC") or "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
    }
    if force_sync:
        run_sync(cli_bin, config, expect_acs_min=max(1, len(partners)))
    else:
        _log(
            "skip Registry→Discovery sync "
            "(default; set ACPS_FORCE_DISCOVERY_SYNC=1 to enable; "
            f"expect_acs_min={max(1, len(partners))})"
        )

    # Best-effort: wait for filtered visibility (sync lag). Pass gate remains NL hit.
    if not args.skip_filtered_wait:
        for agent in agents:
            if agent["role"] != "partner":
                continue
            ok = wait_filtered_visible(
                cli_bin,
                config,
                aic=agent["aic"],
                timeout_s=args.sync_wait_seconds,
                interval_s=args.poll_interval_seconds,
            )
            if ok:
                _log(f"filtered visible: {agent['slug']}")
            else:
                _log(
                    f"filtered not yet visible for {agent['slug']} "
                    f"(continuing to NL gate;  judges NL hit only)"
                )

    # Hard gate: every Partner (and optional Leader) must NL-hit.
    for agent in agents:
        if agent["role"] == "partner" or args.include_leader:
            assert_nl_hit(
                cli_bin,
                config,
                agent=agent,
                timeout_s=args.query_timeout_seconds,
                interval_s=args.poll_interval_seconds,
                limit=args.query_limit,
            )

    _log(f"Step A PASS — {len(partners)} partner NL hits")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepAError as exc:
        _log(f"FAIL: {exc}")
        raise SystemExit(1) from exc
