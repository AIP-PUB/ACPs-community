"""单元测试公共 fixtures。"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from faker import Faker


@pytest.fixture(scope="session")
def fake() -> Faker:
    """faker 实例（中英文混合）。"""
    return Faker(["zh_CN", "en_US"])


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    """生成 RSA-2048 私钥供测试使用。"""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )


@pytest.fixture(scope="session")
def ec_private_key() -> ec.EllipticCurvePrivateKey:
    """生成 P-256 EC 私钥供测试使用。"""
    return ec.generate_private_key(ec.SECP256R1())


def _int_to_b64url(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).decode("ascii").rstrip("=")


@pytest.fixture(scope="session")
def rsa_public_jwk(rsa_private_key: rsa.RSAPrivateKey) -> dict:
    """RSA 公钥 JWK（对应 rsa_private_key）。"""
    pub = rsa_private_key.public_key()
    pub_numbers = pub.public_numbers()
    return {
        "kty": "RSA",
        "n": _int_to_b64url(pub_numbers.n, (pub_numbers.n.bit_length() + 7) // 8),
        "e": _int_to_b64url(pub_numbers.e, (pub_numbers.e.bit_length() + 7) // 8),
    }


@pytest.fixture(scope="session")
def ec_public_jwk(ec_private_key: ec.EllipticCurvePrivateKey) -> dict:
    """EC P-256 公钥 JWK（对应 ec_private_key）。"""
    pub = ec_private_key.public_key()
    pub_numbers = pub.public_numbers()
    coord_len = 32  # P-256 = 32 bytes
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _int_to_b64url(pub_numbers.x, coord_len),
        "y": _int_to_b64url(pub_numbers.y, coord_len),
    }


@pytest.fixture(scope="session")
def ed25519_private_key() -> ed25519.Ed25519PrivateKey:
    """生成 Ed25519 私钥供测试使用。"""
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture(scope="session")
def ed25519_public_jwk(ed25519_private_key: ed25519.Ed25519PrivateKey) -> dict:
    """Ed25519 公钥 JWK（对应 ed25519_private_key）。"""
    pub = ed25519_private_key.public_key()
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(
            pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        .decode("ascii")
        .rstrip("="),
    }
