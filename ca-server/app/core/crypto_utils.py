"""X.509/PKIX 通用加密 helper。"""

from __future__ import annotations

from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

type PrivateKeyTypes = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey
type PublicKeyTypes = rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey
type X509BuilderAlgorithm = hashes.SHA256 | None


class SupportsPublicBytes(Protocol):
    def public_bytes(self, encoding: serialization.Encoding, public_format: serialization.PublicFormat) -> bytes: ...


def _public_key_spki_bytes(public_key: SupportsPublicBytes) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def require_supported_public_key(public_key: object) -> PublicKeyTypes:
    """收口到当前系统明确支持的证书公钥类型。"""
    if not isinstance(public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey, ed25519.Ed25519PublicKey)):
        raise TypeError(f"Unsupported public key type: {type(public_key).__name__}")
    return public_key


def assert_private_key_matches_certificate(private_key: PrivateKeyTypes, certificate: x509.Certificate) -> None:
    """校验私钥与证书公钥匹配。"""
    if _public_key_spki_bytes(private_key.public_key()) != _public_key_spki_bytes(certificate.public_key()):
        raise ValueError("Private key does not match certificate public key")


def x509_signature_algorithm_for(private_key: PrivateKeyTypes) -> X509BuilderAlgorithm:
    """为 X.509/CRL/OCSP builder 返回合适的签名算法参数。"""
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return None
    return hashes.SHA256()


def signature_algorithm_name(private_key: PrivateKeyTypes) -> str:
    """返回统一的人类可读签名算法名。"""
    if isinstance(private_key, rsa.RSAPrivateKey):
        return "SHA256withRSA"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ECDSAwithSHA256"
    return "Ed25519"


def verify_certificate_signature(certificate: x509.Certificate, issuer_certificate: x509.Certificate) -> None:
    """按 issuer 公钥类型验证子证书签名。"""
    issuer_public_key = issuer_certificate.public_key()

    if isinstance(issuer_public_key, rsa.RSAPublicKey):
        signature_hash_algorithm = certificate.signature_hash_algorithm
        if signature_hash_algorithm is None:
            raise ValueError("RSA certificate signature is missing hash algorithm")
        issuer_public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            signature_hash_algorithm,
        )
        return

    if isinstance(issuer_public_key, ec.EllipticCurvePublicKey):
        signature_hash_algorithm = certificate.signature_hash_algorithm
        if signature_hash_algorithm is None:
            raise ValueError("EC certificate signature is missing hash algorithm")
        issuer_public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(signature_hash_algorithm),
        )
        return

    if isinstance(issuer_public_key, ed25519.Ed25519PublicKey):
        issuer_public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
        )
        return

    raise TypeError(f"Unsupported issuer public key type: {type(issuer_public_key).__name__}")


def ocsp_responder_key_hash(public_key: PublicKeyTypes) -> str:
    """返回 OCSP responder byKey 语义对应的 key hash。"""
    return x509.SubjectKeyIdentifier.from_public_key(public_key).digest.hex()
