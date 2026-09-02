#!/usr/bin/env python3
"""业务验收步骤 D — AMP 可观测性收敛。

D1 Monitor（禁止 /health、禁止 heartbeat）：
  查询 audit / access / message / metrics / system — 五类均须在时间窗口内
  返回与 demo Leader + Partner AIC 相关的记录。

D2 Discovery（禁止 Monitor heartbeat API）：
  轮询 Discovery alive-sync 状态 + Leader 与各 Partner 的 per-AIC aliveMap；
  超时 → 失败并给出 Forwarder / Relay / alive-sync 指引。

``--d2-only``：只跑 D2（跳过 D1）。用于 day2 / 强 recreate 后、``business.yml``
之前的就绪门禁（见 playbooks/wait-discovery-alive.yml）；不改变 A–D 产品语义。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class StepDError(RuntimeError):
    pass


# 稳定 Monitor Query API（Core Profile）。不用 /health，不用 /heartbeat/*。
_MONITOR_QUERIES: dict[str, dict[str, Any]] = {
    "audit": {
        "path": "/acps-amp-v1/audit/records/query",
        "needs_time_range": True,
        # Monitor Audit silently ignores op=in — Step D queries per-AIC with eq.
        "hint": "Forwarder→amp.audit→Monitor AuditWriter",
    },
    "access": {
        "path": "/acps-amp-v1/access/events/query",
        "needs_time_range": True,
        "hint": "Forwarder→amp.access→Monitor AccessWriter (Step B/C traffic)",
    },
    "message": {
        "path": "/acps-amp-v1/message/events/query",
        "needs_time_range": True,
        "hint": "Forwarder→amp.message→Monitor MessageWriter (Step C group MessageEmitter)",
    },
    "metrics": {
        "path": "/acps-amp-v1/metrics/snapshots/query",
        "needs_time_range": False,
        "hint": "Forwarder→amp.metrics→Monitor MetricsWriter (periodic MetricsEmitter)",
    },
    "system": {
        "path": "/acps-amp-v1/system/events/query",
        "needs_time_range": True,
        "hint": "Forwarder→amp.system→Monitor SystemWriter (lifecycle events)",
    },
}

_D1_TYPES = ("audit", "access", "message", "metrics", "system")


def _log(msg: str) -> None:
    print(f"[biz-accept-D] {msg}", flush=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_token(token_file: Path) -> str:
    data = json.loads(token_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StepDError(f"token file is not an object: {token_file}")
    token = (
        data.get("access_token")
        or data.get("accessToken")
        or (data.get("tokens") or {}).get("access_token")
        or (data.get("tokens") or {}).get("accessToken")
    )
    if not token:
        raise StepDError(f"token file missing access_token: {token_file}")
    return str(token)


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise StepDError(f"HTTP {exc.code} {method} {url}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise StepDError(f"network error {method} {url}: {exc}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StepDError(f"non-JSON response {method} {url}: {raw[:400]}") from exc


def _load_acs(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StepDError(f"ACS is not an object: {path}")
    return data


def enumerate_partners(partner_root: Path) -> list[dict[str, Any]]:
    online = partner_root / "partners" / "online"
    if not online.is_dir():
        raise StepDError(
            f"partner staging missing: {online} "
            "(expected after demo T4 cert_provision / site phases 12–13)"
        )
    partners: list[dict[str, Any]] = []
    for acs_path in sorted(online.glob("*/acs.json")):
        data = _load_acs(acs_path)
        aic = str(data.get("aic") or "").strip()
        if not aic:
            raise StepDError(f"partner ACS missing AIC: {acs_path}")
        partners.append(
            {
                "slug": acs_path.parent.name,
                "aic": aic,
                "name": str(data.get("name") or "").strip(),
                "role": "partner",
            }
        )
    if not partners:
        raise StepDError(f"no partner ACS under {online}")
    return partners


def load_leader(leader_root: Path) -> dict[str, Any]:
    acs_path = leader_root / "leader" / "atr" / "acs.json"
    if not acs_path.is_file():
        raise StepDError(f"leader ACS missing: {acs_path}")
    data = _load_acs(acs_path)
    aic = str(data.get("aic") or "").strip()
    if not aic:
        raise StepDError(f"leader ACS missing AIC: {acs_path}")
    return {
        "slug": "demo-leader",
        "aic": aic,
        "name": str(data.get("name") or "").strip(),
        "role": "leader",
    }


def _aic_filter_in(aics: list[str]) -> dict[str, Any]:
    return {
        "logic": "and",
        "conditions": [{"field": "aic", "op": "in", "value": list(aics)}],
    }


def _aic_filter_eq(aic: str) -> dict[str, Any]:
    """Single-AIC eq — required for Monitor Audit (records/query silently ignores op=in)."""
    return {
        "logic": "and",
        "conditions": [{"field": "aic", "op": "eq", "value": aic}],
    }


def _time_range(lookback_seconds: float) -> dict[str, str]:
    end = _utc_now() + timedelta(seconds=60)
    start = _utc_now() - timedelta(seconds=lookback_seconds)
    return {"startAt": _iso(start), "endAt": _iso(end)}


def _query_body(
    kind: str,
    *,
    aics: list[str] | None = None,
    aic: str | None = None,
    lookback_seconds: float,
) -> dict[str, Any]:
    meta = _MONITOR_QUERIES[kind]
    if aic is not None:
        filt = _aic_filter_eq(aic)
    else:
        filt = _aic_filter_in(list(aics or []))
    body: dict[str, Any] = {
        "filter": filt,
        "page": {"limit": 20},
    }
    if meta["needs_time_range"]:
        body["timeRange"] = _time_range(lookback_seconds)
    return body


def _items_of(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _item_aic(item: dict[str, Any]) -> str:
    for key in ("aic", "callerAic", "calleeAic"):
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def _related_hits(items: list[dict[str, Any]], aics: set[str]) -> list[dict[str, Any]]:
    """Keep items that belong to demo AICs (server filter + client check)."""
    if not items:
        return []
    matched = [item for item in items if _item_aic(item) in aics]
    if matched:
        return matched
    # Trust server-side aic filter when items omit aic on the wire.
    if all(not _item_aic(item) for item in items):
        return items
    return []


def _post_monitor_query(
    *,
    base_url: str,
    kind: str,
    body: dict[str, Any],
    token: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    meta = _MONITOR_QUERIES[kind]
    url = f"{base_url.rstrip('/')}{meta['path']}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, payload = _http_json(
        "POST",
        url,
        headers=headers or None,
        body=body,
        timeout=timeout,
    )
    if status != 200:
        raise StepDError(f"Monitor {kind} unexpected status {status}")
    return _items_of(payload)


def query_monitor_once(
    *,
    base_url: str,
    kind: str,
    aics: list[str],
    lookback_seconds: float,
    token: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    # Audit records/query: op=in is silently ignored by Monitor (only eq/ne/... applied).
    # Query each demo AIC with eq so D1 cannot miss under mixed traffic / small page.
    if kind == "audit":
        merged: list[dict[str, Any]] = []
        for aic in aics:
            items = _post_monitor_query(
                base_url=base_url,
                kind=kind,
                body=_query_body(kind, aic=aic, lookback_seconds=lookback_seconds),
                token=token,
                timeout=timeout,
            )
            merged.extend(items)
            if merged:
                # One related hit is enough for D1 type coverage.
                break
        return _related_hits(merged, set(aics))

    items = _post_monitor_query(
        base_url=base_url,
        kind=kind,
        body=_query_body(kind, aics=aics, lookback_seconds=lookback_seconds),
        token=token,
        timeout=timeout,
    )
    return _related_hits(items, set(aics))

def run_d1(
    *,
    monitor_base: str,
    aics: list[str],
    lookback_seconds: float,
    token: str | None,
    timeout_s: float,
    interval_s: float,
    http_timeout: float,
) -> dict[str, int]:
    """Poll Monitor five types until each has ≥1 related record or timeout."""
    _log(
        f"D1 Monitor base={monitor_base} aics={len(aics)} "
        f"lookback={lookback_seconds}s timeout={timeout_s}s "
        f"auth={'bearer' if token else 'local'}"
    )
    pending = set(_D1_TYPES)
    counts: dict[str, int] = {k: 0 for k in _D1_TYPES}
    deadline = time.monotonic() + timeout_s
    last_err: dict[str, str] = {}

    while pending and time.monotonic() < deadline:
        for kind in list(pending):
            try:
                hits = query_monitor_once(
                    base_url=monitor_base,
                    kind=kind,
                    aics=aics,
                    lookback_seconds=lookback_seconds,
                    token=token,
                    timeout=http_timeout,
                )
                counts[kind] = len(hits)
                if hits:
                    sample_aic = _item_aic(hits[0]) or "?"
                    _log(f"D1 HIT {kind} count>={len(hits)} sample_aic={sample_aic}")
                    pending.discard(kind)
                else:
                    last_err[kind] = "items empty for demo AICs"
                    _log(f"D1 miss {kind}: no related records yet")
            except StepDError as exc:
                last_err[kind] = str(exc)
                _log(f"D1 error {kind}: {exc}")
        if pending:
            time.sleep(interval_s)

    if pending:
        details = []
        for kind in _D1_TYPES:
            if kind in pending:
                hint = _MONITOR_QUERIES[kind]["hint"]
                details.append(f"{kind}: {last_err.get(kind, 'timeout')} (check {hint})")
        raise StepDError(
            "D1 Monitor incomplete — missing types: "
            + ", ".join(sorted(pending))
            + ". "
            + "; ".join(details)
            + ". Not using /health or heartbeat as pass condition."
        )

    _log("D1 PASS — audit/access/message/metrics/system all hit")
    return counts


def _cli_raw(cli_bin: str, config: str, *args: str) -> str:
    cmd = [cli_bin, "--config", config, *args]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or f"exit={proc.returncode}"
        raise StepDError(f"cli failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return out


def _cli_json(cli_bin: str, config: str, *args: str) -> dict[str, Any]:
    out = _cli_raw(cli_bin, config, *args)
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise StepDError(f"cli returned non-JSON: {out[:800]}") from exc
    if not isinstance(payload, dict):
        raise StepDError(f"cli JSON root must be object: {type(payload).__name__}")
    return payload


def discovery_alive_status(discovery_base: str, *, timeout: float) -> dict[str, Any]:
    """GET Discovery /admin/alive-sync/status — never Monitor heartbeat API."""
    url = f"{discovery_base.rstrip('/')}/admin/alive-sync/status"
    status, payload = _http_json("GET", url, timeout=timeout)
    if status != 200:
        raise StepDError(f"Discovery alive-sync status HTTP {status}")
    if not isinstance(payload, dict):
        raise StepDError("Discovery alive-sync status is not an object")
    return payload


def discover_filtered(cli_bin: str, config: str, aic: str) -> dict[str, Any]:
    filter_json = json.dumps(
        {"conditions": [{"field": "aic", "op": "eq", "value": aic}]},
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


def _alive_map(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    alive_map = result.get("aliveMap") or result.get("alive_map")
    if not isinstance(alive_map, dict):
        return {}
    return alive_map


def agent_alive_view(payload: dict[str, Any], aic: str) -> dict[str, Any] | None:
    alive_map = _alive_map(payload)
    view = alive_map.get(aic)
    if isinstance(view, dict):
        return view
    return None


def run_d2(
    *,
    discovery_base: str,
    cli_bin: str,
    cli_config: str,
    agents: list[dict[str, Any]],
    timeout_s: float,
    interval_s: float,
    http_timeout: float,
    min_alive_count: int,
) -> None:
    """Assert Leader + all Partners alive on Discovery (poll + timeout)."""
    _log(
        f"D2 Discovery base={discovery_base} agents={len(agents)} "
        f"timeout={timeout_s}s (alive-sync status + discover aliveMap only)"
    )
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, Any] = {}
    pending = {a["aic"]: a for a in agents}
    last_err: dict[str, str] = {}

    while pending and time.monotonic() < deadline:
        try:
            last_status = discovery_alive_status(discovery_base, timeout=http_timeout)
        except StepDError as exc:
            _log(f"D2 status error: {exc}")
            time.sleep(interval_s)
            continue

        running = bool(last_status.get("running"))
        checkpoint_count = int(last_status.get("checkpointCount") or 0)
        alive_count = int(last_status.get("aliveCount") or 0)
        _log(
            f"D2 status running={running} checkpointCount={checkpoint_count} "
            f"aliveCount={alive_count}"
        )
        if not running or checkpoint_count < 1:
            last_err["status"] = (
                f"running={running} checkpointCount={checkpoint_count} — "
                "check Discovery [alive_sync] enabled + Monitor Relay "
                "(sync_enabled, amp.heartbeat.alive-delta)"
            )
            time.sleep(interval_s)
            continue
        if alive_count < min_alive_count:
            last_err["status"] = (
                f"aliveCount={alive_count} < {min_alive_count} — "
                "check Forwarder→amp.heartbeat→Monitor Writer→Relay→"
                "alive-delta→Discovery consumer"
            )
            # still try per-AIC; rows may exist but count lagging

        for aic, agent in list(pending.items()):
            try:
                payload = discover_filtered(cli_bin, cli_config, aic)
            except StepDError as exc:
                last_err[aic] = str(exc)
                _log(f"D2 discover error {agent['role']}/{agent['slug']}: {exc}")
                continue
            view = agent_alive_view(payload, aic)
            if view is None:
                last_err[aic] = "aliveMap missing AIC (unknown to alive-sync store)"
                _log(f"D2 miss {agent['role']}/{agent['slug']} aic={aic}: no aliveMap entry")
                continue
            alive = bool(view.get("alive"))
            seen = view.get("aliveLastSeenAt") or view.get("alive_last_seen_at")
            if alive:
                _log(
                    f"D2 HIT {agent['role']}/{agent['slug']} aic={aic} "
                    f"alive=true lastSeen={seen}"
                )
                pending.pop(aic, None)
            else:
                last_err[aic] = f"alive=false lastSeen={seen}"
                _log(
                    f"D2 miss {agent['role']}/{agent['slug']} aic={aic}: "
                    f"alive=false lastSeen={seen}"
                )

        if pending:
            time.sleep(interval_s)

    if pending:
        missing = [
            f"{pending[a]['role']}/{pending[a]['slug']} aic={a} ({last_err.get(a, 'timeout')})"
            for a in sorted(pending)
        ]
        status_hint = last_err.get("status", "")
        raise StepDError(
            "D2 Discovery alive incomplete for Leader+Partners: "
            + "; ".join(missing)
            + (f". status: {status_hint}" if status_hint else "")
            + ". Check Forwarder→amp.heartbeat→Monitor Writer→Relay→"
            "amp.heartbeat.alive-delta→Discovery alive-sync. "
            "D2 never uses Monitor heartbeat API as pass condition."
        )

    _log(f"D2 PASS — {len(agents)} agents alive on Discovery")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--d2-only",
        action="store_true",
        help="Skip D1; only wait for Discovery aliveMap (Leader+Partners)",
    )
    parser.add_argument(
        "--monitor-base-url",
        default="",
        help="http://host:9009 (required unless --d2-only)",
    )
    parser.add_argument("--discovery-base-url", required=True, help="http://host:9005")
    parser.add_argument("--cli-bin", required=True, help="Path to acps-cli")
    parser.add_argument("--cli-config", required=True, help="Path to acps-cli.toml")
    parser.add_argument(
        "--partner-root",
        required=True,
        help="Control staging demo-partner root",
    )
    parser.add_argument(
        "--leader-root",
        required=True,
        help="Control staging demo-leader root",
    )
    parser.add_argument(
        "--monitor-token-file",
        default="",
        help="OIDC access_token JSON (Keycloak on); omit for local auth / --d2-only",
    )
    parser.add_argument("--lookback-seconds", type=float, default=7200.0)
    parser.add_argument("--d1-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--d2-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    if not args.d2_only and not args.monitor_base_url.strip():
        raise StepDError("--monitor-base-url is required unless --d2-only")

    # Hard guard: refuse paths that would probe health/heartbeat as pass gates.
    for bad in ("/health", "/heartbeat"):
        if bad in args.monitor_base_url.rstrip("/"):
            raise StepDError(f"monitor base must not embed {bad}")

    partners = enumerate_partners(Path(args.partner_root))
    leader = load_leader(Path(args.leader_root))
    agents = [leader, *partners]
    aics = [a["aic"] for a in agents]
    mode = "D2-only" if args.d2_only else "D1+D2"
    _log(f"enumerated leader=1 partners={len(partners)} aics={len(aics)} mode={mode}")
    for a in agents:
        _log(f"  {a['role']} {a['slug']}: aic={a['aic']}")

    if not Path(args.cli_bin).is_file():
        raise StepDError(f"acps-cli missing: {args.cli_bin}")
    if not Path(args.cli_config).is_file():
        raise StepDError(f"acps-cli.toml missing: {args.cli_config}")

    d1_counts: dict[str, Any] | None = None
    if not args.d2_only:
        token: str | None = None
        if args.monitor_token_file:
            token_path = Path(args.monitor_token_file)
            if not token_path.is_file():
                raise StepDError(f"monitor token file missing: {token_path}")
            token = _load_token(token_path)

        d1_counts = run_d1(
            monitor_base=args.monitor_base_url,
            aics=aics,
            lookback_seconds=args.lookback_seconds,
            token=token,
            timeout_s=args.d1_timeout_seconds,
            interval_s=args.poll_interval_seconds,
            http_timeout=args.http_timeout_seconds,
        )

    run_d2(
        discovery_base=args.discovery_base_url,
        cli_bin=args.cli_bin,
        cli_config=args.cli_config,
        agents=agents,
        timeout_s=args.d2_timeout_seconds,
        interval_s=args.poll_interval_seconds,
        http_timeout=args.http_timeout_seconds,
        min_alive_count=len(agents),
    )

    if args.d2_only:
        summary = {
            "mode": "d2-only",
            "d2_agents": len(agents),
            "d2": {
                "status": "/admin/alive-sync/status",
                "aliveMap": "acps-cli discover query --type filtered (result.aliveMap)",
            },
        }
        _log(f"D2-only PASS — {json.dumps(summary, ensure_ascii=False)}")
    else:
        summary = {
            "d1": d1_counts,
            "d2_agents": len(agents),
            "apis": {k: _MONITOR_QUERIES[k]["path"] for k in _D1_TYPES},
            "d2": {
                "status": "/admin/alive-sync/status",
                "aliveMap": "acps-cli discover query --type filtered (result.aliveMap)",
            },
        }
        _log(f"Step D PASS — {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepDError as exc:
        _log(f"FAIL: {exc}")
        raise SystemExit(1) from exc
