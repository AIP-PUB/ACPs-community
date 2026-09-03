"""app.core.ca_manager 关键 Ed25519 行为单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from app.core.ca_manager import CAManager
from app.core.crypto_utils import x509_signature_algorithm_for


def _build_ed25519_ca() -> tuple[x509.Certificate, ed25519.Ed25519PrivateKey]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Ed25519 CA")])
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


def _write_pem_bundle(
    cert: x509.Certificate, private_key: ed25519.Ed25519PrivateKey, cert_path: Path, key_path: Path
) -> None:
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_load_ca_from_files_accepts_ed25519(tmp_path: Path) -> None:
    cert, private_key = _build_ed25519_ca()
    cert_path = tmp_path / "ca.crt"
    key_path = tmp_path / "ca.key"
    _write_pem_bundle(cert, private_key, cert_path, key_path)

    manager = CAManager.__new__(CAManager)
    manager.settings = cast("Any", SimpleNamespace())
    manager.ca_cert = None
    manager.ca_private_key = None

    manager._load_ca_from_files(cert_path, key_path)

    assert isinstance(manager.ca_private_key, ed25519.Ed25519PrivateKey)
    assert manager.ca_cert is not None


def test_verify_certificate_chain_accepts_ed25519_issuer() -> None:
    ca_cert, ca_key = _build_ed25519_ca()
    leaf_key = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(private_key=ca_key, algorithm=x509_signature_algorithm_for(ca_key))
    )

    manager = CAManager.__new__(CAManager)
    manager.settings = cast("Any", SimpleNamespace())
    manager.ca_cert = ca_cert
    manager.ca_private_key = ca_key

    assert manager.verify_certificate_chain(leaf_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")) is True
