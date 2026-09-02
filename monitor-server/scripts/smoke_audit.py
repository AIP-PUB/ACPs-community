#!/usr/bin/env python3
"""Audit 开发模式冒烟测试：CA-based 审计日志签名 + 验签全流程（不依赖 Kafka/Fluent Bit）。

验证以下流程：
  1. 用 CertificateAuditSigner（leader/atr/client.pem + client.key）对审计日志签名
  2. 签名后写入 integrity.kid（证书序列号）、integrity.alg（RS256）、integrity.sig
  3. 用 CAKeyResolver 调用 ca-server GET /acps-atr-v2/ca/keys/{kid} 获取公钥
  4. 用获取到的公钥验签
  5. 期望：KeyResolver 返回 RSA 公钥，签名验证成功

用法：
    cd monitor-server
    APP_ENV=development uv run python scripts/smoke_audit.py

前提：
    - ca-server 正在运行（http://localhost:9003）
    - demo-leader cert 已导入 ca-server DB（serial=5FCB77CA23BBA4A402358721986C16C7626269B）
    - APP_ENV=development（确保 [atr].mock_mode=false 生效）
"""

import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


async def main() -> None:
    # ── 1. 构造签名器 ─────────────────────────────────────────────────────────
    from acps_sdk.amp.signer import CertificateAuditSigner

    cert_file = ROOT / "demo-leader" / "leader" / "atr" / "client.pem"
    key_file = ROOT / "demo-leader" / "leader" / "atr" / "client.key"

    if not cert_file.exists() or not key_file.exists():
        print(f"[ERROR] 找不到 cert/key: {cert_file}", file=sys.stderr)
        sys.exit(1)

    signer = CertificateAuditSigner(
        private_key_pem=key_file.read_text(),
        cert_pem=cert_file.read_text(),
    )
    print(f"[OK] 签名器: kid={signer.kid}, alg={signer.alg}")

    # ── 2. 构造待签 LogRecord dict（不含 integrity 字段）─────────────────────
    import time
    import uuid

    aic = "1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"
    raw_dict = {
        "spec_version": "AMP-1.0",
        "log_id": str(uuid.uuid4()),
        "log_type": "audit",
        "timestamp": f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "aic": aic,
        "session_id": str(uuid.uuid4()),
        "body": {
            "event_type": "e2e_test",
            "actor": {"aic": aic, "type": "agent"},
            "action": {"type": "test", "description": "E2E CA 验签测试"},
            "target": {"type": "system", "id": "monitor-server"},
            "result": {"status": "success", "details": "E2E signing test"},
        },
    }

    # ── 3. JCS 规范化 + 签名 ────────────────────────────────────────────────
    import jcs

    canonical = jcs.canonicalize(raw_dict)
    sig_bytes = signer.sign(canonical)
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()

    # 拼上 integrity 字段（仅用于下面验证步骤展示，不重新签名）
    raw_with_integrity = dict(raw_dict)
    raw_with_integrity["integrity"] = {
        "alg": signer.alg,
        "kid": signer.kid,
        "sig": sig_b64,
    }
    print(f"[OK] 签名完成: sig={sig_b64[:32]}..., kid={signer.kid}")

    # ── 4. 构造 CAKeyResolver 并获取公钥 ─────────────────────────────────────
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from app.audit.key_resolver import ATRUnavailableError, CAKeyResolver, KeyNotFoundError
    from app.core.config import settings

    ca_base_url = settings.atr_ca_base_url
    mock_mode = settings.atr_mock_mode
    print(f"[INFO] 配置: ca_base_url={ca_base_url}, mock_mode={mock_mode}")

    resolver = CAKeyResolver(
        ca_base_url=ca_base_url,
        cache_ttl_seconds=settings.atr_key_cache_ttl_seconds,
    )
    try:
        pub_key = await resolver.resolve(aic=aic, kid=signer.kid)
        print(f"[OK] CA 公钥查询成功: type={type(pub_key).__name__}")
    except KeyNotFoundError as e:
        print(f"[FAIL] 公钥未找到: {e}", file=sys.stderr)
        sys.exit(1)
    except ATRUnavailableError as e:
        print(f"[FAIL] CA 服务不可达: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 5. 验签 ──────────────────────────────────────────────────────────────
    from cryptography.exceptions import InvalidSignature

    from app.audit.writer import _verify_signature

    try:
        _verify_signature(pub_key=pub_key, alg=signer.alg, raw_log=raw_dict, sig_b64=sig_b64)
        print("[OK] 签名验证成功 ✓")
    except InvalidSignature:
        print("[FAIL] 签名验证失败！", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[FAIL] 签名验证错误: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=== E2E 验证结果 ===")
    print(f"  kid    = {signer.kid}")
    print(f"  alg    = {signer.alg}")
    print(f"  CA URL = {ca_base_url}")
    print("  result = signature_verified = True")
    print()
    print("[PASS] CA-based 审计日志签名 + 验签 E2E 验证通过！")


if __name__ == "__main__":
    asyncio.run(main())
