"""ACME JOSE/JWK helper。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .exception import AcmeError, AcmeException

type JWKPublicKey = rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey


def base64url_encode(data: bytes) -> str:
    """Base64URL 编码。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def base64url_decode(data: str) -> bytes:
    """Base64URL 解码。"""
    padding_needed = 4 - (len(data) % 4)
    if padding_needed != 4:
        data += "=" * padding_needed

    try:
        return base64.urlsafe_b64decode(data.encode("ascii"))
    except Exception as exc:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED,
            error_msg=f"Invalid base64url encoding: {exc!s}",
        ) from exc


def _require_string_field(jwk: dict[str, Any], field: str) -> str:
    value = jwk.get(field)
    if not isinstance(value, str) or not value:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED,
            error_msg=f"Missing or invalid JWK field: {field}",
        )
    return value


def _reject_private_jwk_material(jwk: dict[str, Any], private_fields: set[str]) -> None:
    if any(field in jwk for field in private_fields):
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED,
            error_msg="ACME public JWK must not contain private key material",
        )


def public_jwk_projection(jwk: dict[str, Any]) -> dict[str, str]:
    """提取公开 JWK 投影，统一 canonical 形状。"""
    kty = jwk.get("kty")

    if kty == "RSA":
        _reject_private_jwk_material(jwk, {"d", "p", "q", "dp", "dq", "qi", "oth"})
        return {
            "kty": "RSA",
            "n": _require_string_field(jwk, "n"),
            "e": _require_string_field(jwk, "e"),
        }

    if kty == "EC":
        _reject_private_jwk_material(jwk, {"d"})
        crv = _require_string_field(jwk, "crv")
        if crv not in {"P-256", "P-384", "P-521"}:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported curve: {crv}",
            )
        return {
            "kty": "EC",
            "crv": crv,
            "x": _require_string_field(jwk, "x"),
            "y": _require_string_field(jwk, "y"),
        }

    if kty == "OKP":
        _reject_private_jwk_material(jwk, {"d"})
        crv = _require_string_field(jwk, "crv")
        if crv != "Ed25519":
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported OKP curve: {crv}",
            )
        x_value = _require_string_field(jwk, "x")
        x_bytes = base64url_decode(x_value)
        if len(x_bytes) != 32:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg="Invalid Ed25519 public key length",
            )
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": x_value,
        }

    raise AcmeException(
        status_code=400,
        error_name=AcmeError.UNSUPPORTED_ALGORITHM,
        error_msg=f"Unsupported key type: {kty}",
    )


def jwk_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """比较两个 JWK 的公开投影是否一致。"""
    return public_jwk_projection(left) == public_jwk_projection(right)


def jwk_to_public_key(jwk: dict[str, Any]) -> JWKPublicKey:
    """将 JWK 转换为 cryptography 公钥对象。"""
    projected = public_jwk_projection(jwk)
    kty = projected["kty"]

    if kty == "RSA":
        try:
            n = int.from_bytes(base64url_decode(projected["n"]), byteorder="big")
            e = int.from_bytes(base64url_decode(projected["e"]), byteorder="big")
            return rsa.RSAPublicNumbers(e, n).public_key(backend=default_backend())
        except Exception as exc:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg=f"Invalid RSA JWK format: {exc!s}",
            ) from exc

    if kty == "EC":
        try:
            curve_name = projected["crv"]
            if curve_name == "P-256":
                curve: ec.EllipticCurve = ec.SECP256R1()
            elif curve_name == "P-384":
                curve = ec.SECP384R1()
            else:
                curve = ec.SECP521R1()

            x = int.from_bytes(base64url_decode(projected["x"]), byteorder="big")
            y = int.from_bytes(base64url_decode(projected["y"]), byteorder="big")
            return ec.EllipticCurvePublicNumbers(x, y, curve).public_key(backend=default_backend())
        except AcmeException:
            raise
        except Exception as exc:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg=f"Invalid EC JWK format: {exc!s}",
            ) from exc

    try:
        return ed25519.Ed25519PublicKey.from_public_bytes(base64url_decode(projected["x"]))
    except AcmeException:
        raise
    except Exception as exc:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED,
            error_msg=f"Invalid OKP JWK format: {exc!s}",
        ) from exc


def public_key_to_jwk(public_key: JWKPublicKey) -> dict[str, str]:
    """将 cryptography 公钥对象转换为公开 JWK。"""
    if isinstance(public_key, rsa.RSAPublicKey):
        rsa_numbers = public_key.public_numbers()
        modulus_length = (rsa_numbers.n.bit_length() + 7) // 8
        exponent_length = (rsa_numbers.e.bit_length() + 7) // 8
        return {
            "kty": "RSA",
            "n": base64url_encode(rsa_numbers.n.to_bytes(modulus_length, "big")),
            "e": base64url_encode(rsa_numbers.e.to_bytes(exponent_length, "big")),
        }

    if isinstance(public_key, ec.EllipticCurvePublicKey):
        ec_numbers = public_key.public_numbers()
        coordinate_length = (public_key.curve.key_size + 7) // 8
        if isinstance(public_key.curve, ec.SECP256R1):
            curve_name = "P-256"
        elif isinstance(public_key.curve, ec.SECP384R1):
            curve_name = "P-384"
        elif isinstance(public_key.curve, ec.SECP521R1):
            curve_name = "P-521"
        else:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported certificate key curve: {type(public_key.curve).__name__}",
            )
        return {
            "kty": "EC",
            "crv": curve_name,
            "x": base64url_encode(ec_numbers.x.to_bytes(coordinate_length, "big")),
            "y": base64url_encode(ec_numbers.y.to_bytes(coordinate_length, "big")),
        }

    if isinstance(public_key, ed25519.Ed25519PublicKey):
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": base64url_encode(
                public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ),
        }

    raise AcmeException(
        status_code=400,
        error_name=AcmeError.UNSUPPORTED_ALGORITHM,
        error_msg=f"Unsupported certificate key type: {type(public_key).__name__}",
    )


def compute_jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """计算 JWK thumbprint。"""
    canonical_json = json.dumps(public_jwk_projection(jwk), separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).digest()
    return base64url_encode(digest)


def verify_compact_jws_signature(
    protected_b64: str,
    payload_b64: str,
    signature_b64: str,
    public_key_jwk: dict[str, Any],
    alg: str,
) -> None:
    """验证 compact JWS 的签名部分。"""
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    signature = base64url_decode(signature_b64)
    public_key = jwk_to_public_key(public_key_jwk)

    try:
        if alg == "EdDSA":
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED,
                    error_msg="Key type mismatch: expected Ed25519 key",
                )
            public_key.verify(signature, signing_input)
            return

        if alg in {"RS256", "ES256"}:
            hash_alg: hashes.HashAlgorithm = hashes.SHA256()
        elif alg in {"RS384", "ES384"}:
            hash_alg = hashes.SHA384()
        elif alg in {"RS512", "ES512"}:
            hash_alg = hashes.SHA512()
        else:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported algorithm: {alg}",
            )

        if alg.startswith("RS"):
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED,
                    error_msg="Key type mismatch: expected RSA key",
                )
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hash_alg)
            return

        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg="Key type mismatch: expected EC key",
            )

        coordinate_length = (public_key.curve.key_size + 7) // 8
        expected_signature_length = coordinate_length * 2
        if len(signature) != expected_signature_length:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg="Invalid EC signature length",
            )

        r = int.from_bytes(signature[:coordinate_length], byteorder="big")
        s = int.from_bytes(signature[coordinate_length:], byteorder="big")
        der_signature = encode_dss_signature(r, s)
        public_key.verify(der_signature, signing_input, ec.ECDSA(hash_alg))
    except AcmeException:
        raise
    except Exception as exc:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED,
            error_msg=f"Invalid signature: {exc!s}",
        ) from exc
