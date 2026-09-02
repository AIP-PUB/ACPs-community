"""测试 ACME 工具函数（app.acme.utils）。"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.acme.exception import AcmeError, AcmeException
from app.acme.jwk import public_jwk_projection
from app.acme.utils import (
    base64url_decode,
    base64url_encode,
    compute_jwk_thumbprint,
    jwk_to_public_key,
    parse_protected_header,
    verify_jws_signature,
)

# ---------- base64url_decode / encode ----------


class TestBase64UrlUtils:
    def test_encode_decode_roundtrip(self) -> None:
        original = b"hello world"
        assert base64url_decode(base64url_encode(original)) == original

    def test_encode_no_padding(self) -> None:
        result = base64url_encode(b"hi")
        assert "=" not in result

    def test_encode_url_safe(self) -> None:
        # 使用会产生 +/ 的字节
        result = base64url_encode(b"\xfb\xff\xfe")
        assert "+" not in result
        assert "/" not in result

    def test_decode_no_padding_input(self) -> None:
        # "hello" → aGVsbG8 (无末尾 =)
        result = base64url_decode("aGVsbG8")
        assert result == b"hello"

    def test_decode_with_padding_input(self) -> None:
        # 也兼容含 padding 的输入
        result = base64url_decode("aGVsbG8=")
        assert result == b"hello"


# ---------- jwk_to_public_key ----------


class TestJWKToPublicKey:
    def test_rsa_key_conversion(self, rsa_public_jwk: dict) -> None:
        key = jwk_to_public_key(rsa_public_jwk)
        assert isinstance(key, rsa.RSAPublicKey)

    def test_ec_p256_key_conversion(self, ec_public_jwk: dict) -> None:
        key = jwk_to_public_key(ec_public_jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)

    def test_ec_p384_key_conversion(self) -> None:
        priv = ec.generate_private_key(ec.SECP384R1())
        pub = priv.public_key()
        nums = pub.public_numbers()
        coord_len = 48
        jwk = {
            "kty": "EC",
            "crv": "P-384",
            "x": base64url_encode(nums.x.to_bytes(coord_len, "big")),
            "y": base64url_encode(nums.y.to_bytes(coord_len, "big")),
        }
        key = jwk_to_public_key(jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)

    def test_ec_p521_key_conversion(self) -> None:
        priv = ec.generate_private_key(ec.SECP521R1())
        pub = priv.public_key()
        nums = pub.public_numbers()
        coord_len = 66
        jwk = {
            "kty": "EC",
            "crv": "P-521",
            "x": base64url_encode(nums.x.to_bytes(coord_len, "big")),
            "y": base64url_encode(nums.y.to_bytes(coord_len, "big")),
        }
        key = jwk_to_public_key(jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)

    def test_okp_ed25519_key_conversion(self, ed25519_public_jwk: dict) -> None:
        key = jwk_to_public_key(ed25519_public_jwk)
        assert isinstance(key, ed25519.Ed25519PublicKey)

    def test_unsupported_kty_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            jwk_to_public_key({"kty": "oct", "k": "abc"})
        assert exc_info.value.error_name == AcmeError.UNSUPPORTED_ALGORITHM

    def test_ec_unsupported_curve_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            jwk_to_public_key(
                {
                    "kty": "EC",
                    "crv": "brainpoolP256r1",
                    "x": base64url_encode(b"\x01" * 32),
                    "y": base64url_encode(b"\x01" * 32),
                }
            )
        assert exc_info.value.error_name == AcmeError.UNSUPPORTED_ALGORITHM


# ---------- compute_jwk_thumbprint ----------


class TestComputeJWKThumbprint:
    def test_rsa_thumbprint_deterministic(self, rsa_public_jwk: dict) -> None:
        t1 = compute_jwk_thumbprint(rsa_public_jwk)
        t2 = compute_jwk_thumbprint(rsa_public_jwk)
        assert t1 == t2

    def test_rsa_thumbprint_is_base64url(self, rsa_public_jwk: dict) -> None:
        thumbprint = compute_jwk_thumbprint(rsa_public_jwk)
        assert "=" not in thumbprint
        assert "+" not in thumbprint
        assert "/" not in thumbprint

    def test_rsa_thumbprint_not_empty(self, rsa_public_jwk: dict) -> None:
        thumbprint = compute_jwk_thumbprint(rsa_public_jwk)
        assert len(thumbprint) > 0

    def test_ec_thumbprint_supported(self, ec_public_jwk: dict) -> None:
        thumbprint = compute_jwk_thumbprint(ec_public_jwk)
        assert len(thumbprint) > 0

    def test_okp_thumbprint_supported(self, ed25519_public_jwk: dict) -> None:
        thumbprint = compute_jwk_thumbprint(ed25519_public_jwk)
        assert len(thumbprint) > 0


class TestPublicJWKProjection:
    def test_rsa_private_fields_rejected(self, rsa_public_jwk: dict) -> None:
        with pytest.raises(AcmeException) as exc_info:
            public_jwk_projection({**rsa_public_jwk, "d": "secret"})
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_ec_private_fields_rejected(self, ec_public_jwk: dict) -> None:
        with pytest.raises(AcmeException) as exc_info:
            public_jwk_projection({**ec_public_jwk, "d": "secret"})
        assert exc_info.value.error_name == AcmeError.MALFORMED


# ---------- parse_protected_header ----------


class TestParseProtectedHeader:
    def test_valid_header(self) -> None:
        header = {"alg": "RS256", "jwk": {"kty": "RSA"}}
        encoded = base64url_encode(json.dumps(header).encode())
        result = parse_protected_header(encoded)
        assert result["alg"] == "RS256"

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            parse_protected_header("invalid!!base64!!")
        assert exc_info.value.status_code == 400

    def test_invalid_json_raises(self) -> None:
        encoded = base64url_encode(b"not json {")
        with pytest.raises(AcmeException) as exc_info:
            parse_protected_header(encoded)
        assert exc_info.value.status_code == 400

    def test_non_dict_raises(self) -> None:
        encoded = base64url_encode(json.dumps([1, 2, 3]).encode())
        with pytest.raises(AcmeException) as exc_info:
            parse_protected_header(encoded)
        assert exc_info.value.status_code == 400


# ---------- verify_jws_signature ----------


class TestVerifyJWSSignature:
    def test_valid_rsa_signature_returns_true(self, rsa_private_key: rsa.RSAPrivateKey, rsa_public_jwk: dict) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        protected_b64 = base64url_encode(json.dumps({"alg": "RS256"}).encode())
        payload_b64 = base64url_encode(json.dumps({"x": 1}).encode())
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        signature_bytes = rsa_private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        signature_b64 = base64url_encode(signature_bytes)

        result = verify_jws_signature(protected_b64, payload_b64, signature_b64, rsa_public_jwk)
        assert result is True

    def test_invalid_signature_returns_false(self, rsa_public_jwk: dict) -> None:
        protected_b64 = base64url_encode(json.dumps({"alg": "RS256"}).encode())
        payload_b64 = base64url_encode(json.dumps({"x": 1}).encode())
        fake_sig = base64url_encode(b"\x00" * 256)

        result = verify_jws_signature(protected_b64, payload_b64, fake_sig, rsa_public_jwk)
        assert result is False

    def test_valid_ec_signature_returns_true(
        self, ec_private_key: ec.EllipticCurvePrivateKey, ec_public_jwk: dict
    ) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as ec_module

        protected_b64 = base64url_encode(json.dumps({"alg": "ES256"}).encode())
        payload_b64 = base64url_encode(json.dumps({"y": 2}).encode())
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        der_sig = ec_private_key.sign(signing_input, ec_module.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sig_b64 = base64url_encode(raw_sig)

        result = verify_jws_signature(protected_b64, payload_b64, sig_b64, ec_public_jwk)
        assert result is True

    def test_valid_eddsa_signature_returns_true(
        self, ed25519_private_key: ed25519.Ed25519PrivateKey, ed25519_public_jwk: dict
    ) -> None:
        protected_b64 = base64url_encode(json.dumps({"alg": "EdDSA"}).encode())
        payload_b64 = base64url_encode(json.dumps({"z": 3}).encode())
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        signature_bytes = ed25519_private_key.sign(signing_input)
        signature_b64 = base64url_encode(signature_bytes)

        result = verify_jws_signature(protected_b64, payload_b64, signature_b64, ed25519_public_jwk)
        assert result is True
