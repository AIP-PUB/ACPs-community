"""app.core.crypto_utils 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID

from app.core.crypto_utils import (
    X509BuilderAlgorithm,
    assert_private_key_matches_certificate,
    ocsp_responder_key_hash,
    signature_algorithm_name,
    verify_certificate_signature,
    x509_signature_algorithm_for,
)


def _signing_algorithm(
    private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey,
) -> X509BuilderAlgorithm:
    return x509_signature_algorithm_for(private_key)


def _build_self_signed_ca(
    private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey,
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=private_key, algorithm=_signing_algorithm(private_key))
    )


def _build_leaf_cert(
    issuer_cert: x509.Certificate,
    issuer_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey,
    leaf_public_key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey,
) -> x509.Certificate:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
        .issuer_name(issuer_cert.subject)
        .public_key(leaf_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(private_key=issuer_key, algorithm=_signing_algorithm(issuer_key))
    )


def test_x509_signature_algorithm_for_key_types() -> None:
    assert isinstance(
        x509_signature_algorithm_for(rsa.generate_private_key(public_exponent=65537, key_size=2048)), hashes.SHA256
    )
    assert isinstance(x509_signature_algorithm_for(ec.generate_private_key(ec.SECP256R1())), hashes.SHA256)
    assert x509_signature_algorithm_for(ed25519.Ed25519PrivateKey.generate()) is None


def test_signature_algorithm_name_for_key_types() -> None:
    assert signature_algorithm_name(rsa.generate_private_key(public_exponent=65537, key_size=2048)) == "SHA256withRSA"
    assert signature_algorithm_name(ec.generate_private_key(ec.SECP256R1())) == "ECDSAwithSHA256"
    assert signature_algorithm_name(ed25519.Ed25519PrivateKey.generate()) == "Ed25519"


def test_assert_private_key_matches_certificate_success_and_failure() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    certificate = _build_self_signed_ca(private_key)

    assert_private_key_matches_certificate(private_key, certificate)

    with pytest.raises(ValueError, match="does not match"):
        assert_private_key_matches_certificate(ed25519.Ed25519PrivateKey.generate(), certificate)


def test_verify_certificate_signature_supports_ed25519_issuer() -> None:
    issuer_key = ed25519.Ed25519PrivateKey.generate()
    issuer_cert = _build_self_signed_ca(issuer_key)
    leaf_key = ed25519.Ed25519PrivateKey.generate()
    leaf_cert = _build_leaf_cert(issuer_cert, issuer_key, leaf_key.public_key())

    verify_certificate_signature(leaf_cert, issuer_cert)


def test_ocsp_responder_key_hash_matches_ski_digest() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    assert ocsp_responder_key_hash(public_key) == x509.SubjectKeyIdentifier.from_public_key(public_key).digest.hex()
