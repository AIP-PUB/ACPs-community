"""tests/unit/test_audit_key_resolver.py — 密钥解析器单元测试。"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.audit.key_resolver import KeyNotFoundError, MockKeyResolver


def _generate_ed25519_pem() -> str:
    """生成测试用 Ed25519 公钥 PEM 字符串。"""
    private_key = Ed25519PrivateKey.generate()
    pub_key = private_key.public_key()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return pub_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


class TestMockKeyResolver:
    @pytest.fixture
    def pem(self) -> str:
        return _generate_ed25519_pem()

    @pytest.fixture
    def resolver(self, pem: str) -> MockKeyResolver:
        return MockKeyResolver({"test-kid": pem})

    @pytest.mark.asyncio
    async def test_returns_key_for_known_kid(self, resolver: MockKeyResolver) -> None:
        key = await resolver.resolve("any-aic", "test-kid")
        assert key is not None

    @pytest.mark.asyncio
    async def test_raises_key_not_found_for_unknown_kid(self, resolver: MockKeyResolver) -> None:
        with pytest.raises(KeyNotFoundError):
            await resolver.resolve("any-aic", "unknown-kid")

    @pytest.mark.asyncio
    async def test_aic_is_ignored_in_mock(self, pem: str) -> None:
        """MockKeyResolver 不区分 AIC，任意 AIC 都能解析到同一 kid 的公钥。"""
        resolver = MockKeyResolver({"k": pem})
        key1 = await resolver.resolve("aic-alice", "k")
        key2 = await resolver.resolve("aic-bob", "k")
        assert key1 is key2 or key1 == key2  # 同一公钥对象或值相等

    def test_empty_keys_dict_is_valid(self) -> None:
        """空 dict 初始化不报错。"""
        r = MockKeyResolver({})
        assert r is not None

    @pytest.mark.asyncio
    async def test_multiple_kids(self, pem: str) -> None:
        pem2 = _generate_ed25519_pem()
        resolver = MockKeyResolver({"kid-1": pem, "kid-2": pem2})
        k1 = await resolver.resolve("aic", "kid-1")
        k2 = await resolver.resolve("aic", "kid-2")
        assert k1 != k2
