"""demo-leader AMP Emitter 单例初始化。

从 atr/acs.json 读取 LEADER_AIC，按日志类型构造各 Emitter 并暴露为模块级常量。
凡需要发射 AMP 日志的模块，直接 import 本文件中对应的 Emitter 即可，
无需重复构造 Path / AIC。

签名密钥优先级：
  1. leader/atr/client.pem + client.key（由 acps-cli bootstrap 签发的 mTLS 证书）
     kid = 证书序列号（大写十六进制），alg 由密钥类型自动决定（EdDSA 或 RS256）。
     monitor-server 用 kid 向 ca-server 查询公钥完成验签。
  2. monitor-server/config/audit_keys.json（开发 mock 模式回退）
     文件不存在时 Emitter 以未签名模式运行，monitor-server 将日志标记为
     verification_failure_type="missing_public_key" 入库，不影响功能。

可通过环境变量覆盖证书路径：
  AMP_AUDIT_CERT_FILE  — 证书 PEM 文件路径（默认 leader/atr/client.pem）
  AMP_AUDIT_KEY_FILE   — 私钥 PEM 文件路径（默认 leader/atr/client.key）
  AMP_AUDIT_KEYS_FILE  — audit_keys.json 路径（回退模式，默认 monitor-server/config/audit_keys.json）
"""

import json
import logging
import os
from pathlib import Path

from acps_sdk.amp import AuditEmitter
from acps_sdk.amp.signer import CertificateAuditSigner, load_signer_from_keys_json
from assistant.amp_paths import resolve_amp_log_dir, resolve_leader_acs_file

_logger = logging.getLogger(__name__)

_ACS_FILE = resolve_leader_acs_file()
LEADER_AIC: str = json.loads(_ACS_FILE.read_text(encoding="utf-8"))["aic"]

_AUDIT_LOG_FILE = resolve_amp_log_dir() / "amp_audit.jsonl"

# ── 签名器初始化：优先使用 CA 证书，回退到 audit_keys.json ────────────────────

_CERT_FILE = Path(os.environ.get("AMP_AUDIT_CERT_FILE", str(Path(__file__).parent.parent / "atr" / "client.pem")))
_KEY_FILE = Path(os.environ.get("AMP_AUDIT_KEY_FILE", str(Path(__file__).parent.parent / "atr" / "client.key")))

if _CERT_FILE.exists() and _KEY_FILE.exists():
    try:
        _signer = CertificateAuditSigner(
            private_key_pem=_KEY_FILE.read_text(encoding="utf-8"),
            cert_pem=_CERT_FILE.read_text(encoding="utf-8"),
        )
        _logger.info(
            "AMP 审计签名器：使用 CA 证书模式（kid=%s, alg=%s）",
            _signer.kid,
            _signer.alg,
        )
    except Exception as exc:
        _logger.warning("CA 证书签名器初始化失败，回退到 audit_keys.json 模式: %s", exc)
        _signer = None  # type: ignore[assignment]
else:
    _signer = None  # type: ignore[assignment]

if _signer is None:
    _DEFAULT_KEYS_FILE = Path(__file__).parent.parent.parent.parent / "monitor-server" / "config" / "audit_keys.json"
    _AUDIT_KEYS_FILE = Path(os.environ.get("AMP_AUDIT_KEYS_FILE", str(_DEFAULT_KEYS_FILE)))
    _signer = load_signer_from_keys_json(_AUDIT_KEYS_FILE, LEADER_AIC)  # type: ignore[assignment]

LEADER_EMITTER = AuditEmitter(_AUDIT_LOG_FILE, aic=LEADER_AIC, signer=_signer)
