"""app.common.ocsp_service 核心逻辑单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.common.ocsp_model import OCSPResponder
from app.common.ocsp_service import OCSPService, build_ocsp_responder_certificate
from app.core.crypto_utils import x509_signature_algorithm_for


def _build_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False)
        .sign(private_key=private_key, algorithm=x509_signature_algorithm_for(private_key))
    )
    return certificate, private_key


def _build_leaf(ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey) -> x509.Certificate:
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(private_key=ca_key, algorithm=x509_signature_algorithm_for(ca_key))
    )


def test_build_ocsp_responder_certificate_profile() -> None:
    ca_cert, ca_key = _build_ca()
    responder_key = ed25519.Ed25519PrivateKey.generate()

    responder_cert = build_ocsp_responder_certificate(responder_key.public_key(), ca_cert, ca_key)

    basic_constraints = responder_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    key_usage = responder_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    eku = responder_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value

    assert basic_constraints.ca is False
    assert key_usage.digital_signature is True
    assert ExtendedKeyUsageOID.OCSP_SIGNING in eku


def test_validate_responder_profile_rejects_missing_ocsp_eku() -> None:
    ca_cert, ca_key = _build_ca()
    responder_key = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    bad_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bad responder")]))
        .issuer_name(ca_cert.subject)
        .public_key(responder_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=ca_key, algorithm=x509_signature_algorithm_for(ca_key))
    )

    service = OCSPService(cast("Any", SimpleNamespace()))
    with pytest.raises(ValueError, match="OCSP responder certificate must include"):
        service._validate_responder_profile(bad_cert, responder_key)


def test_sign_ocsp_response_with_ed25519_responder_returns_algorithm_name() -> None:
    ca_cert, ca_key = _build_ca()
    leaf_cert = _build_leaf(ca_cert, ca_key)
    responder_key = ed25519.Ed25519PrivateKey.generate()
    responder_cert = build_ocsp_responder_certificate(responder_key.public_key(), ca_cert, ca_key)

    builder = x509.ocsp.OCSPResponseBuilder()
    now = datetime.now(UTC)
    builder = builder.add_response(
        cert=leaf_cert,
        issuer=ca_cert,
        algorithm=hashes.SHA1(),
        cert_status=x509.ocsp.OCSPCertStatus.GOOD,
        this_update=now,
        next_update=now + timedelta(hours=24),
        revocation_time=None,
        revocation_reason=None,
    )

    responder = OCSPResponder(
        name="responder",
        certificate_pem=responder_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8"),
        private_key_pem=responder_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8"),
        certificate_serial=format(responder_cert.serial_number, "x"),
        is_active=True,
        endpoints={"primary": "https://example.test/ocsp"},
        supported_extensions=["nonce"],
    )

    service = OCSPService(cast("Any", SimpleNamespace()))
    ocsp_response, response_der, algorithm_name = service._sign_ocsp_response(builder, responder)

    assert algorithm_name == "Ed25519"
    assert ocsp_response.responder_key_hash is not None
    assert len(response_der) > 0
