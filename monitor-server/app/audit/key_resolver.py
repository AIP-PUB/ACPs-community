"""Audit 密钥解析：从 CA 服务获取签名验证公钥。

提供 KeyResolver 协议与两个实现：
- CAKeyResolver：调用 CA 服务 /acps-atr-v2/ca/keys/{serial} 解析公钥，含 TTL 缓存。
- MockKeyResolver：测试/开发用，从内存配置返回固定公钥。

设计约束：
- 缓存以 (aic, kid) 为 key，避免跨 Agent 污染。
- CA 不可达时抛出 CAUnavailableError，允许 Writer 按"不可达"语义处理（仍入库，标记 missing_public_key）。
- CA 返回 status != "valid" 时抛出 KeyNotFoundError（证书已吊销或过期，拒绝信任）。
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

PublicKey = Ed25519PublicKey | RSAPublicKey

logger = structlog.get_logger(__name__)


class ATRUnavailableError(Exception):
    """CA 服务不可达或返回错误时抛出（保留原名兼容现有 writer.py 捕获逻辑）。"""


# 向后兼容别名
CAUnavailableError = ATRUnavailableError


class KeyNotFoundError(Exception):
    """指定 kid（证书序列号）在 CA 中未找到，或证书状态非 valid 时抛出。"""


@runtime_checkable
class KeyResolver(Protocol):
    """密钥解析器协议：根据 AIC 和 kid 返回公钥。"""

    async def resolve(self, aic: str, kid: str) -> PublicKey:
        """解析公钥。

        Args:
            aic: Agent Identity Code 标识符。
            kid: 签名密钥 ID（来自 LogRecord.integrity.kid，即证书序列号大写十六进制）。

        Returns:
            已加载的公钥对象（Ed25519PublicKey 或 RSAPublicKey）。

        Raises:
            ATRUnavailableError: CA 服务不可达。
            KeyNotFoundError: 指定 kid 对应证书不存在，或证书状态非 valid。
        """
        ...


class _CacheEntry:
    """公钥缓存条目。"""

    __slots__ = ("expires_at", "key")

    def __init__(self, key: PublicKey, ttl_seconds: int) -> None:
        self.key = key
        self.expires_at = time.monotonic() + ttl_seconds


class CAKeyResolver:
    """从 CA 服务解析公钥，含基于 (aic, kid) 的 TTL 内存缓存。

    kid 为 AMP LogRecord.integrity.kid，即 X.509 证书序列号（大写十六进制）。
    调用 GET {ca_base_url}/acps-atr-v2/ca/keys/{kid} 获取 SPKI PEM 公钥。

    对已吊销（revoked）或已过期（expired）证书同样拒绝信任，抛出 KeyNotFoundError。

    Args:
        ca_base_url: CA 服务根地址（如 http://localhost:9003）。
        cache_ttl_seconds: 缓存 TTL（秒），默认 300 秒。
    """

    def __init__(self, ca_base_url: str, cache_ttl_seconds: int = 300) -> None:
        self._base_url = ca_base_url.rstrip("/")
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    async def resolve(self, aic: str, kid: str) -> PublicKey:
        """解析 (aic, kid) 对应的公钥，优先返回缓存结果。"""
        cache_key = (aic, kid)
        entry = self._cache.get(cache_key)
        if entry is not None and time.monotonic() < entry.expires_at:
            return entry.key

        pub_key = await self._fetch_from_ca(aic, kid)
        self._cache[cache_key] = _CacheEntry(pub_key, self._cache_ttl)
        return pub_key

    async def _fetch_from_ca(self, aic: str, kid: str) -> PublicKey:
        """向 CA 服务发起 HTTP 请求，获取 kid（证书序列号）对应的 SPKI PEM 公钥。

        Raises:
            ATRUnavailableError: 请求失败或 HTTP 错误。
            KeyNotFoundError: 证书不存在（404），或证书状态非 valid。
        """
        import httpx  # 延迟导入，避免在无 CA 环境下引入依赖

        url = f"{self._base_url}/acps-atr-v2/ca/keys/{kid}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
        except Exception as exc:
            raise ATRUnavailableError(f"CA 服务请求失败: {exc}") from exc

        if response.status_code == 404:
            raise KeyNotFoundError(f"kid={kid!r} 对应证书在 CA 中不存在")
        if response.status_code != 200:
            raise ATRUnavailableError(f"CA 服务返回 {response.status_code}: {response.text[:200]}")

        data = response.json()

        # 检查证书状态：已吊销或已过期的证书不可信任
        status = str(data.get("status") or "").lower()
        if status != "valid":
            raise KeyNotFoundError(f"kid={kid!r} 证书状态为 {status!r}（aic={aic}），拒绝信任非 valid 状态证书")

        pem: str = data.get("publicKey", "") or data.get("public_key", "")
        if not pem:
            raise ATRUnavailableError(f"CA 响应缺少 publicKey 字段: {data}")

        pub_key = load_pem_public_key(pem.encode())
        if not isinstance(pub_key, (Ed25519PublicKey, RSAPublicKey)):
            raise ATRUnavailableError(f"CA 返回不支持的密钥类型: {type(pub_key)}")

        logger.info("CA 公钥查询成功", kid=kid, aic=aic, status=status)
        return pub_key


# 向后兼容别名（旧代码或测试中引用 ATRKeyResolver 时不需要修改）
ATRKeyResolver = CAKeyResolver


class MockKeyResolver:
    """测试/开发用密钥解析器：从内存 dict 返回预设公钥。

    Args:
        keys: {kid: PEM 公钥字符串} 映射。
    """

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys: dict[str, PublicKey] = {}
        for kid, pem in keys.items():
            loaded = load_pem_public_key(pem.encode())
            if not isinstance(loaded, (Ed25519PublicKey, RSAPublicKey)):
                raise ValueError(f"MockKeyResolver: kid={kid!r} 密钥类型不支持")
            self._keys[kid] = loaded

    async def resolve(self, aic: str, kid: str) -> PublicKey:
        """返回预设公钥，aic 参数忽略（mock 不区分 Agent）。"""
        if kid not in self._keys:
            raise KeyNotFoundError(f"MockKeyResolver: kid={kid!r} 未配置")
        return self._keys[kid]
