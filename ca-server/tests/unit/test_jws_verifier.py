"""测试 JWS 验证器（app.acme.jws_verifier）。"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.acme.exception import AcmeError, AcmeException
from app.acme.jws_verifier import JWSVerifier, get_jws_verifier

# ---------- 测试辅助函数 ----------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_rsa_jws(protected: dict, payload: dict | str, private_key: rsa.RSAPrivateKey, alg: str = "RS256") -> str:
    """用 RSA 私钥生成一条合法 JWS 字符串。"""
    protected_b64 = _b64url_encode(json.dumps(protected, separators=(",", ":")).encode())
    if isinstance(payload, dict):
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    else:
        payload_b64 = payload

    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    hash_map = {"RS256": hashes.SHA256(), "RS384": hashes.SHA384(), "RS512": hashes.SHA512()}
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hash_map[alg])
    return f"{protected_b64}.{payload_b64}.{_b64url_encode(signature)}"


def _make_ec_jws(protected: dict, payload: dict, private_key: ec.EllipticCurvePrivateKey, alg: str = "ES256") -> str:
    """用 EC 私钥生成一条合法 JWS 字符串。"""
    protected_b64 = _b64url_encode(json.dumps(protected, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    hash_map = {"ES256": hashes.SHA256(), "ES384": hashes.SHA384(), "ES512": hashes.SHA512()}
    der_sig = private_key.sign(signing_input, ec.ECDSA(hash_map[alg]))
    r, s = decode_dss_signature(der_sig)
    coord_len = 32  # P-256
    raw_sig = r.to_bytes(coord_len, "big") + s.to_bytes(coord_len, "big")
    return f"{protected_b64}.{payload_b64}.{_b64url_encode(raw_sig)}"


def _make_eddsa_jws(
    protected: dict,
    payload: dict,
    private_key: ed25519.Ed25519PrivateKey,
) -> str:
    """用 Ed25519 私钥生成一条合法 JWS 字符串。"""
    protected_b64 = _b64url_encode(json.dumps(protected, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{protected_b64}.{payload_b64}.{_b64url_encode(signature)}"


# ---------- base64url_decode / encode ----------


class TestBase64UrlDecodeEncode:
    def test_encode_decode_roundtrip(self) -> None:
        verifier = JWSVerifier()
        original = b"hello world"
        encoded = verifier.base64url_encode(original)
        assert verifier.base64url_decode(encoded) == original

    def test_decode_no_padding_needed(self) -> None:
        verifier = JWSVerifier()
        # "hello" -> aGVsbG8 (长度 7，padding_needed=1，不补充)
        result = verifier.base64url_decode("aGVsbG8")
        assert result == b"hello"

    def test_decode_padding_2_chars(self) -> None:
        verifier = JWSVerifier()
        # "hi" -> aGk (长度 3，需补 1 个 =)
        result = verifier.base64url_decode("aGk")
        assert result == b"hi"

    def test_decode_padding_4_chars_no_op(self) -> None:
        # 长度刚好是 4 的倍数，不补 =
        verifier = JWSVerifier()
        result = verifier.base64url_decode("dGVzdA")  # "test"
        assert result == b"test"

    def test_decode_invalid_raises_acme_exception(self) -> None:
        verifier = JWSVerifier()
        # 非 ASCII 字符将导致 encode("ascii") 失败，进而触发 AcmeException
        with pytest.raises(AcmeException) as exc_info:
            verifier.base64url_decode("非法字符")
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_encode_strips_padding(self) -> None:
        verifier = JWSVerifier()
        result = verifier.base64url_encode(b"hi")
        assert "=" not in result

    def test_encode_no_plus_or_slash(self) -> None:
        verifier = JWSVerifier()
        for byte_val in range(256):
            result = verifier.base64url_encode(bytes([byte_val]))
            assert "+" not in result
            assert "/" not in result


# ---------- parse_jws ----------


class TestParseJWS:
    def test_parse_valid_jws(self) -> None:
        verifier = JWSVerifier()
        header = {"alg": "RS256", "jwk": {"kty": "RSA"}}
        payload = {"test": "value"}
        header_b64 = _b64url_encode(json.dumps(header).encode())
        payload_b64 = _b64url_encode(json.dumps(payload).encode())
        jws = f"{header_b64}.{payload_b64}.fakesig"

        parsed_header, parsed_payload, sig = verifier.parse_jws(jws)
        assert parsed_header["alg"] == "RS256"
        assert parsed_payload["test"] == "value"
        assert sig == "fakesig"

    def test_parse_empty_payload_returns_empty_dict(self) -> None:
        verifier = JWSVerifier()
        header = {"alg": "RS256", "jwk": {"kty": "RSA"}}
        header_b64 = _b64url_encode(json.dumps(header).encode())
        jws = f"{header_b64}..fakesig"

        _, payload, _ = verifier.parse_jws(jws)
        assert payload == {}

    def test_parse_wrong_number_of_parts_raises(self) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException) as exc_info:
            verifier.parse_jws("only.two")
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_parse_four_parts_raises(self) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException) as exc_info:
            verifier.parse_jws("a.b.c.d")
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_parse_invalid_json_header_raises(self) -> None:
        verifier = JWSVerifier()
        bad_header = _b64url_encode(b"not json {")
        with pytest.raises(AcmeException) as exc_info:
            verifier.parse_jws(f"{bad_header}.payload.sig")
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_parse_invalid_json_payload_raises(self) -> None:
        verifier = JWSVerifier()
        good_header = _b64url_encode(json.dumps({"alg": "RS256"}).encode())
        bad_payload = _b64url_encode(b"{broken json")
        with pytest.raises(AcmeException) as exc_info:
            verifier.parse_jws(f"{good_header}.{bad_payload}.sig")
        assert exc_info.value.error_name == AcmeError.MALFORMED


# ---------- _verify_protected_header ----------


class TestVerifyProtectedHeader:
    def setup_method(self) -> None:
        self.verifier = JWSVerifier()
        self.dummy_jwk = {"kty": "RSA", "n": "abc", "e": "AQAB"}

    def test_missing_alg_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header({"jwk": self.dummy_jwk}, self.dummy_jwk)
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_unsupported_alg_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header({"alg": "HS256", "jwk": self.dummy_jwk}, self.dummy_jwk)
        assert exc_info.value.error_name == AcmeError.UNSUPPORTED_ALGORITHM

    def test_valid_rsa_algs_pass(self) -> None:
        for alg in ["RS256", "RS384", "RS512"]:
            self.verifier._verify_protected_header({"alg": alg, "jwk": self.dummy_jwk}, self.dummy_jwk)

    def test_valid_ec_algs_pass(self) -> None:
        for alg in ["ES256", "ES384", "ES512"]:
            self.verifier._verify_protected_header({"alg": alg, "jwk": self.dummy_jwk}, self.dummy_jwk)

    def test_valid_eddsa_alg_passes(self) -> None:
        self.verifier._verify_protected_header({"alg": "EdDSA", "jwk": self.dummy_jwk}, self.dummy_jwk)

    def test_nonce_missing_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header(
                {"alg": "RS256", "jwk": self.dummy_jwk},
                self.dummy_jwk,
                expected_nonce="expected-nonce",
            )
        assert exc_info.value.error_name == AcmeError.BAD_NONCE

    def test_nonce_mismatch_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header(
                {"alg": "RS256", "jwk": self.dummy_jwk, "nonce": "wrong"},
                self.dummy_jwk,
                expected_nonce="expected-nonce",
            )
        assert exc_info.value.error_name == AcmeError.BAD_NONCE

    def test_nonce_match_passes(self) -> None:
        self.verifier._verify_protected_header(
            {"alg": "RS256", "jwk": self.dummy_jwk, "nonce": "correct-nonce"},
            self.dummy_jwk,
            expected_nonce="correct-nonce",
        )

    def test_url_missing_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header(
                {"alg": "RS256", "jwk": self.dummy_jwk},
                self.dummy_jwk,
                expected_url="http://example.com",
            )
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_url_mismatch_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header(
                {"alg": "RS256", "jwk": self.dummy_jwk, "url": "http://wrong.com"},
                self.dummy_jwk,
                expected_url="http://expected.com",
            )
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_url_match_passes(self) -> None:
        self.verifier._verify_protected_header(
            {"alg": "RS256", "jwk": self.dummy_jwk, "url": "http://example.com"},
            self.dummy_jwk,
            expected_url="http://example.com",
        )

    def test_jwk_mismatch_raises(self) -> None:
        different_jwk = {"kty": "RSA", "n": "different", "e": "AQAB"}
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header({"alg": "RS256", "jwk": different_jwk}, self.dummy_jwk)
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_jwk_with_private_material_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header(
                {"alg": "RS256", "jwk": {**self.dummy_jwk, "d": "secret"}},
                self.dummy_jwk,
            )
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_kid_accepted_without_jwk_check(self) -> None:
        # kid 存在时不要求 jwk 匹配
        self.verifier._verify_protected_header(
            {"alg": "RS256", "kid": "https://example.com/acme/acct/1"},
            self.dummy_jwk,
        )

    def test_neither_jwk_nor_kid_raises(self) -> None:
        with pytest.raises(AcmeException) as exc_info:
            self.verifier._verify_protected_header({"alg": "RS256"}, self.dummy_jwk)
        assert exc_info.value.error_name == AcmeError.MALFORMED


# ---------- _jwk_to_public_key ----------


class TestJWKToPublicKey:
    def test_rsa_key_conversion(self, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        key = verifier._jwk_to_public_key(rsa_public_jwk)
        assert isinstance(key, rsa.RSAPublicKey)

    def test_ec_p256_key_conversion(self, ec_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        key = verifier._jwk_to_public_key(ec_public_jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)

    def test_okp_ed25519_key_conversion(self, ed25519_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        key = verifier._jwk_to_public_key(ed25519_public_jwk)
        assert isinstance(key, ed25519.Ed25519PublicKey)

    def test_okp_missing_curve_raises(self) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException) as exc_info:
            verifier._jwk_to_public_key({"kty": "OKP", "x": "abc"})
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_okp_private_material_raises(self, ed25519_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException) as exc_info:
            verifier._jwk_to_public_key({**ed25519_public_jwk, "d": "secret"})
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_invalid_rsa_n_raises(self) -> None:
        verifier = JWSVerifier()
        # n=1（模数过小）将导致 RSA 公鑰创建失败
        tiny_n = base64.urlsafe_b64encode(b"\x01").rstrip(b"=").decode()
        with pytest.raises(AcmeException) as exc_info:
            verifier._jwk_to_public_key({"kty": "RSA", "n": tiny_n, "e": "AQAB"})
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_ec_unsupported_curve_raises(self) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException) as exc_info:
            verifier._jwk_to_public_key(
                {
                    "kty": "EC",
                    "crv": "brainpoolP256r1",
                    "x": _b64url_encode(b"\x01" * 32),
                    "y": _b64url_encode(b"\x01" * 32),
                }
            )
        assert exc_info.value.error_name == AcmeError.UNSUPPORTED_ALGORITHM

    def test_ec_p384_key_conversion(self) -> None:
        priv = ec.generate_private_key(ec.SECP384R1())
        pub = priv.public_key()
        nums = pub.public_numbers()
        coord_len = 48  # P-384
        jwk = {
            "kty": "EC",
            "crv": "P-384",
            "x": _b64url_encode(nums.x.to_bytes(coord_len, "big")),
            "y": _b64url_encode(nums.y.to_bytes(coord_len, "big")),
        }
        verifier = JWSVerifier()
        key = verifier._jwk_to_public_key(jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)

    def test_ec_p521_key_conversion(self) -> None:
        priv = ec.generate_private_key(ec.SECP521R1())
        pub = priv.public_key()
        nums = pub.public_numbers()
        coord_len = 66  # P-521
        jwk = {
            "kty": "EC",
            "crv": "P-521",
            "x": _b64url_encode(nums.x.to_bytes(coord_len, "big")),
            "y": _b64url_encode(nums.y.to_bytes(coord_len, "big")),
        }
        verifier = JWSVerifier()
        key = verifier._jwk_to_public_key(jwk)
        assert isinstance(key, ec.EllipticCurvePublicKey)


# ---------- compute_jwk_thumbprint ----------


class TestComputeJWKThumbprint:
    def test_rsa_thumbprint_is_deterministic(self, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        t1 = verifier.compute_jwk_thumbprint(rsa_public_jwk)
        t2 = verifier.compute_jwk_thumbprint(rsa_public_jwk)
        assert t1 == t2

    def test_ec_thumbprint_is_deterministic(self, ec_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        t1 = verifier.compute_jwk_thumbprint(ec_public_jwk)
        t2 = verifier.compute_jwk_thumbprint(ec_public_jwk)
        assert t1 == t2

    def test_okp_thumbprint_is_deterministic(self, ed25519_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        t1 = verifier.compute_jwk_thumbprint(ed25519_public_jwk)
        t2 = verifier.compute_jwk_thumbprint(ed25519_public_jwk)
        assert t1 == t2

    def test_rsa_ec_thumbprints_differ(self, rsa_public_jwk: dict, ec_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        t_rsa = verifier.compute_jwk_thumbprint(rsa_public_jwk)
        t_ec = verifier.compute_jwk_thumbprint(ec_public_jwk)
        assert t_rsa != t_ec

    def test_unsupported_kty_raises(self) -> None:
        verifier = JWSVerifier()
        with pytest.raises(AcmeException):
            verifier.compute_jwk_thumbprint({"kty": "oct"})

    def test_thumbprint_is_base64url(self, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        thumbprint = verifier.compute_jwk_thumbprint(rsa_public_jwk)
        assert "=" not in thumbprint
        assert "+" not in thumbprint
        assert "/" not in thumbprint

    def test_thumbprint_not_empty(self, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        thumbprint = verifier.compute_jwk_thumbprint(rsa_public_jwk)
        assert len(thumbprint) > 0


# ---------- verify_jws_signature (端到端) ----------


class TestVerifyJWSSignature:
    def test_valid_rsa_jws(self, rsa_private_key: rsa.RSAPrivateKey, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "RS256", "jwk": rsa_public_jwk}
        payload = {"key": "value"}
        jws_string = _make_rsa_jws(protected, payload, rsa_private_key, "RS256")

        result = verifier.verify_jws_signature(jws_string, rsa_public_jwk)
        assert result["key"] == "value"

    def test_valid_ec_jws(self, ec_private_key: ec.EllipticCurvePrivateKey, ec_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "ES256", "jwk": ec_public_jwk}
        payload = {"ec": "test"}
        jws_string = _make_ec_jws(protected, payload, ec_private_key, "ES256")

        result = verifier.verify_jws_signature(jws_string, ec_public_jwk)
        assert result["ec"] == "test"

    def test_valid_eddsa_jws(self, ed25519_private_key: ed25519.Ed25519PrivateKey, ed25519_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "EdDSA", "jwk": ed25519_public_jwk}
        payload = {"okp": "test"}
        jws_string = _make_eddsa_jws(protected, payload, ed25519_private_key)

        result = verifier.verify_jws_signature(jws_string, ed25519_public_jwk)
        assert result["okp"] == "test"

    def test_invalid_signature_raises(self, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "RS256", "jwk": rsa_public_jwk}
        header_b64 = _b64url_encode(json.dumps(protected).encode())
        payload_b64 = _b64url_encode(json.dumps({"x": 1}).encode())
        fake_sig = _b64url_encode(b"\x00" * 256)
        bad_jws = f"{header_b64}.{payload_b64}.{fake_sig}"

        with pytest.raises(AcmeException) as exc_info:
            verifier.verify_jws_signature(bad_jws, rsa_public_jwk)
        assert exc_info.value.error_name == AcmeError.MALFORMED

    def test_nonce_validation_in_full_verify(self, rsa_private_key: rsa.RSAPrivateKey, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "RS256", "jwk": rsa_public_jwk, "nonce": "my-nonce"}
        payload = {"data": 1}
        jws_string = _make_rsa_jws(protected, payload, rsa_private_key, "RS256")

        result = verifier.verify_jws_signature(jws_string, rsa_public_jwk, expected_nonce="my-nonce")
        assert result["data"] == 1

    def test_wrong_nonce_raises(self, rsa_private_key: rsa.RSAPrivateKey, rsa_public_jwk: dict) -> None:
        verifier = JWSVerifier()
        protected = {"alg": "RS256", "jwk": rsa_public_jwk, "nonce": "correct"}
        jws_string = _make_rsa_jws(protected, {"x": 1}, rsa_private_key, "RS256")

        with pytest.raises(AcmeException) as exc_info:
            verifier.verify_jws_signature(jws_string, rsa_public_jwk, expected_nonce="wrong")
        assert exc_info.value.error_name == AcmeError.BAD_NONCE


# ---------- get_jws_verifier singleton ----------


def test_get_jws_verifier_returns_same_instance() -> None:
    v1 = get_jws_verifier()
    v2 = get_jws_verifier()
    assert v1 is v2


def test_get_jws_verifier_is_jws_verifier_type() -> None:
    verifier = get_jws_verifier()
    assert isinstance(verifier, JWSVerifier)
