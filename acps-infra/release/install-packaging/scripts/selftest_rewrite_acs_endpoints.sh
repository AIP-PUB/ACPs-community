#!/usr/bin/env bash
# Self-test rewrite_acs_endpoints.py + assert_acs_endpoints.py (four quadrants).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REWRITE="$ROOT/scripts/rewrite_acs_endpoints.py"
ASSERT="$ROOT/scripts/assert_acs_endpoints.py"
TD="$(mktemp -d)"
trap 'rm -rf "$TD"' EXIT

python3 - "$TD" "$REWRITE" "$ASSERT" <<'PY'
import json, subprocess, sys
from pathlib import Path

td, rewrite, assert_s = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

sample = {
    "aic": "AIC-TEST",
    "active": False,
    "endPoints": [
        {"url": "https://localhost:9021/rpc", "transport": "JSONRPC"},
        {"url": "amqps://rabbitmq:5671/acps?inbox=x", "transport": "AMQP"},
        {"url": "https://localhost:9025/stream", "transport": "SSE"},
    ],
    "certificate": {"altNames": {"dns": ["localhost"]}},
}

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"FAIL {cmd}\n{r.stdout}\n{r.stderr}")
    return r

# image multi
acs = td / "acs.json"
acs.write_text(json.dumps(sample, indent=2), encoding="utf-8")
run(["python3", rewrite, str(acs), "--advertise-host", "docker2.acps.local", "--amqp-host", "docker.acps.local"])
data = json.loads(acs.read_text())
assert data["endPoints"][0]["url"] == "https://docker2.acps.local:9021/rpc"
assert data["endPoints"][1]["url"].startswith("amqps://docker.acps.local:5671/")
run(["python3", assert_s, str(acs), "--advertise-host", "docker2.acps.local"])

# image colocated amqp=rabbitmq
acs.write_text(json.dumps(sample, indent=2), encoding="utf-8")
run(["python3", rewrite, str(acs), "--advertise-host", "docker.acps.local", "--amqp-host", "rabbitmq"])
data = json.loads(acs.read_text())
assert data["endPoints"][1]["url"].startswith("amqps://rabbitmq:5671/")
assert data["endPoints"][0]["url"] == "https://docker.acps.local:9021/rpc"

# host single advertise=127.0.0.1
acs.write_text(json.dumps(sample, indent=2), encoding="utf-8")
run(["python3", rewrite, str(acs), "--advertise-host", "127.0.0.1", "--amqp-host", "127.0.0.1"])
data = json.loads(acs.read_text())
assert data["endPoints"][0]["url"] == "https://127.0.0.1:9021/rpc"
run(["python3", assert_s, str(acs), "--advertise-host", "127.0.0.1", "--forbid-compose-dns"])

# host forbid compose DNS residual
bad = td / "bad.json"
bad.write_text(json.dumps({
    "endPoints": [
        {"url": "amqps://rabbitmq:5671/acps", "transport": "AMQP"},
        {"url": "https://host.example:9021/rpc", "transport": "JSONRPC"},
    ],
}, indent=2), encoding="utf-8")
r = subprocess.run(["python3", assert_s, str(bad), "--forbid-compose-dns"], capture_output=True, text=True)
assert r.returncode == 1, "expected compose DNS assert failure"

# leader scenario glob
root = td / "leader"
(root / "atr").mkdir(parents=True)
(root / "scenario/expert/tour").mkdir(parents=True)
(root / "atr" / "acs.json").write_text(json.dumps({
    "aic": "L", "active": True,
    "endPoints": [{"url": "amqps://localhost:5671/acps", "transport": "AMQP"}],
    "certificate": {"altNames": {"dns": []}},
}, indent=2), encoding="utf-8")
(root / "scenario/expert/tour" / "beijing_food.json").write_text(json.dumps({
    "endPoints": [
        {"url": "https://localhost:9021/rpc", "transport": "JSONRPC"},
        {"url": "amqps://rabbitmq:5671/acps", "transport": "AMQP"},
    ],
}, indent=2), encoding="utf-8")
run(["python3", rewrite, str(root / "scenario"), "--advertise-host", "biz-2.local", "--amqp-host", "biz-1.local"])
food = json.loads((root / "scenario/expert/tour" / "beijing_food.json").read_text())
assert food["endPoints"][0]["url"] == "https://biz-2.local:9021/rpc"
assert food["endPoints"][1]["url"].startswith("amqps://biz-1.local:5671/")

print("rewrite_acs_endpoints self-test OK")
PY
