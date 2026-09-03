"""app.common.certificate_service 关键密钥默认值单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from app.common.certificate_model import CertificateType
from app.common.certificate_service import CertificateService


def test_generate_certificate_pair_defaults_to_ed25519() -> None:
    service = CertificateService(cast("Any", SimpleNamespace()))

    cert_pem, key_pem = service.generate_certificate_pair("test-root", CertificateType.ROOT)

    certificate = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)

    assert isinstance(private_key, ed25519.Ed25519PrivateKey)
    assert isinstance(certificate.public_key(), ed25519.Ed25519PublicKey)
    assert certificate.signature_hash_algorithm is None


def test_generate_certificate_pair_supports_rsa_override() -> None:
    service = CertificateService(cast("Any", SimpleNamespace()))

    cert_pem, key_pem = service.generate_certificate_pair("test-root", CertificateType.ROOT, key_type="rsa")

    certificate = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)

    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert isinstance(certificate.public_key(), rsa.RSAPublicKey)
    assert certificate.signature_hash_algorithm is not None
