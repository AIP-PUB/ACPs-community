"""为所有 demo Agent 生成 Ed25519 审计签名密钥对。

输出 monitor-server/config/audit_keys.json（含公钥 + 私钥，勿提交 Git！）。

用法：
    cd monitor-server
    uv run python scripts/gen_audit_keys.py
"""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

AGENTS = {
    "demo_leader": "1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ",
    "beijing_food": "1.2.156.3088.1.1.D55UOU.NEBZUA.1.0QLD",
    "beijing_rural": "1.2.156.3088.1.1.1Z4AXU.YN86QQ.1.186L",
    "beijing_urban": "1.2.156.3088.1.1.TTLIHU.LW9WCA.1.0N2P",
    "china_hotel": "1.2.156.3088.1.1.CIQJUQ.HELDGD.1.03TO",
    "china_transport": "1.2.156.3088.1.1.8UDX9U.NNVB61.1.13WT",
}

keys: dict[str, dict[str, str]] = {}
for name, aic in AGENTS.items():
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    kid = f"audit-key-{name}"
    keys[name] = {"aic": aic, "kid": kid, "public_key": pub_pem, "private_key": priv_pem}
    print(f"Generated: {name} (kid={kid})")

out = Path(__file__).parent.parent / "config" / "audit_keys.json"
out.write_text(json.dumps(keys, indent=2))
print(f"\nSaved to {out}")
print("⚠️  勿提交 Git！audit_keys.json 已加入 .gitignore")
