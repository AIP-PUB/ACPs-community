"""AMP Audit 签名器协议与实现。

提供 AuditSigner 协议与两种开箱即用的实现：
- Ed25519AuditSigner：用于 audit_keys.json（开发/mock）流程，显式指定 kid。
- CertificateAuditSigner：用于生产流程，从 X.509 证书自动提取序列号作为 kid，
  并自动检测私钥类型（Ed25519 → EdDSA，RSA → RS256）。

安装签名额外依赖：pip install "acps-sdk[amp-sign]"

典型使用（生产/CA 流程）：
    from acps_sdk.amp.signer import CertificateAuditSigner
    signer = CertificateAuditSigner(
        private_key_pem=Path("client.key").read_text(),
        cert_pem=Path("client.pem").read_text(),
    )
    emitter = AuditEmitter(log_file, aic=aic, signer=signer)

典型使用（开发/mock 流程）：
    from acps_sdk.amp.signer import load_signer_from_keys_json
    signer = load_signer_from_keys_json("/path/to/audit_keys.json", aic)
    emitter = AuditEmitter(log_file, aic=aic, signer=signer)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

_logger = logging.getLogger(__name__)


@runtime_checkable
class AuditSigner(Protocol):
    """AMP 日志签名器协议。

    实现此协议的对象可传入 AuditEmitter 构造函数，
    在写入日志前对记录内容（不含 integrity 字段）进行签名。
    """

    @property
    def kid(self) -> str:
        """密钥 ID，写入 integrity.kid 字段。"""
        ...

    @property
    def alg(self) -> str:
        """签名算法标识，写入 integrity.alg 字段（'EdDSA' / 'ES256' / 'RS256'）。"""
        ...

    def sign(self, data: bytes) -> bytes:
        """对 data 签名，返回原始签名字节（Emitter 负责 Base64url 编码）。"""
        ...


class Ed25519AuditSigner:
    """Ed25519 签名器，从 PKCS8 PEM 私钥构造。

    依赖 cryptography 库（包含在 amp-sign extra 中）。

    Args:
        private_key_pem: PKCS8 PEM 格式的 Ed25519 私钥字符串。
        kid: 密钥 ID，写入 integrity.kid 字段。

    Raises:
        ImportError: 未安装 cryptography 库。
        TypeError: 私钥类型不是 Ed25519PrivateKey。
    """

    def __init__(self, private_key_pem: str, kid: str) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as exc:
            raise ImportError(
                "Ed25519AuditSigner 需要 cryptography 库。"
                "请安装：pip install 'acps-sdk[amp-sign]'"
            ) from exc

        loaded = load_pem_private_key(private_key_pem.encode(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise TypeError(
                f"期望 Ed25519PrivateKey，实际类型: {type(loaded).__name__}。"
                "请确保私钥由 gen_audit_keys.py 或 ed25519 算法生成。"
            )
        self._key = loaded
        self._kid = kid

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def alg(self) -> str:
        return "EdDSA"

    def sign(self, data: bytes) -> bytes:
        """使用 Ed25519 私钥对 data 签名，返回 64 字节原始签名。"""
        return self._key.sign(data)


class CertificateAuditSigner:
    """从 X.509 证书 + 私钥构造的签名器，自动以证书序列号（uppercase hex）为 kid。

    按 AMP spec §5.1.2 规定，kid 应为签名证书的 X.509 serialNumber（大写十六进制字符串），
    验签方通过 kid 向 CA 服务查询对应公钥。

    支持的私钥类型：
    - Ed25519PrivateKey → alg = "EdDSA"（优先，AMP spec 推荐）
    - RSAPrivateKey → alg = "RS256"

    依赖 cryptography 库（包含在 amp-sign extra 中）。

    Args:
        private_key_pem: PKCS8 PEM 格式私钥（Ed25519 或 RSA）。
        cert_pem: PEM 格式 X.509 证书，用于提取序列号作为 kid。

    Raises:
        ImportError: 未安装 cryptography 库。
        TypeError: 私钥类型不受支持（非 Ed25519 / RSA）。
    """

    def __init__(self, private_key_pem: str, cert_pem: str) -> None:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as exc:
            raise ImportError(
                "CertificateAuditSigner 需要 cryptography 库。"
                "请安装：pip install 'acps-sdk[amp-sign]'"
            ) from exc

        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        # AMP spec §5.1.2: kid 为证书序列号大写十六进制字符串（RFC 5280 §4.1.2.2）
        self._kid = format(cert.serial_number, "X")

        key = load_pem_private_key(private_key_pem.encode(), password=None)
        if isinstance(key, Ed25519PrivateKey):
            self._alg = "EdDSA"
        elif isinstance(key, RSAPrivateKey):
            self._alg = "RS256"
        else:
            raise TypeError(
                f"不支持的私钥类型: {type(key).__name__}。"
                "CertificateAuditSigner 仅支持 Ed25519 和 RSA 私钥。"
            )
        self._key = key

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def alg(self) -> str:
        return self._alg

    def sign(self, data: bytes) -> bytes:
        """对 data 签名，返回原始签名字节（Emitter 负责 Base64url 编码）。"""
        if self._alg == "EdDSA":
            return self._key.sign(data)  # type: ignore[union-attr]
        # RS256: PKCS#1 v1.5 + SHA-256
        from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
        from cryptography.hazmat.primitives.hashes import SHA256
        return self._key.sign(data, PKCS1v15(), SHA256())  # type: ignore[union-attr]


def load_signer_from_keys_json(
    keys_json_path: str | Path,
    aic: str,
) -> Ed25519AuditSigner | None:
    """从 audit_keys.json 文件按 AIC 查找私钥，构造 Ed25519AuditSigner。

    文件格式（由 monitor-server/scripts/gen_audit_keys.py 生成）：
        {
          "agent_name": {
            "aic": "1.2.156.3088...",
            "kid": "audit-key-agent_name",
            "public_key": "-----BEGIN PUBLIC KEY-----\\n...",
            "private_key": "-----BEGIN PRIVATE KEY-----\\n..."
          },
          ...
        }

    文件不存在或找不到匹配 AIC 时静默返回 None，AuditEmitter 将以未签名模式运行。

    Args:
        keys_json_path: audit_keys.json 文件路径。
        aic: Agent Instance Context 标识符，用于在文件中定位条目。

    Returns:
        Ed25519AuditSigner 实例，失败时返回 None。
    """
    path = Path(keys_json_path)
    if not path.exists():
        _logger.warning(
            "audit_keys.json 不存在，AuditEmitter 将以未签名模式运行 (path=%s)。"
            "运行 uv run python monitor-server/scripts/gen_audit_keys.py 生成。",
            path,
        )
        return None

    try:
        entries: dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _logger.warning("读取 audit_keys.json 失败，AuditEmitter 将以未签名模式运行: %s", exc)
        return None

    for entry in entries.values():
        if entry.get("aic") == aic:
            try:
                return Ed25519AuditSigner(
                    private_key_pem=entry["private_key"],
                    kid=entry["kid"],
                )
            except Exception as exc:
                _logger.warning("构造 Ed25519AuditSigner 失败，AuditEmitter 将以未签名模式运行: %s", exc)
                return None

    _logger.warning(
        "audit_keys.json 中未找到 aic=%s 的条目，AuditEmitter 将以未签名模式运行。"
        "如需签名，请在 gen_audit_keys.py 的 AGENTS 字典中添加该 Agent。",
        aic,
    )
    return None
