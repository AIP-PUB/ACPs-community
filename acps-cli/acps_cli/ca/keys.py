import logging
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


def generate_private_key(key_type="ed25519"):
    logger.debug(f"Generating {key_type.upper()} private key")
    if key_type == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if key_type == "ed25519":
        return Ed25519PrivateKey.generate()
    return ec.generate_private_key(ec.SECP256R1())


def save_private_key(key, path):
    # Ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logger.debug(f"Saving private key to {path}")

    key_format = (
        serialization.PrivateFormat.PKCS8
        if isinstance(key, Ed25519PrivateKey)
        else serialization.PrivateFormat.TraditionalOpenSSL
    )

    with open(path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=key_format,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    # Set permissions to 600
    os.chmod(path, 0o600)


def load_private_key(path):
    logger.debug(f"Loading private key from {path}")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def private_key_type_name(private_key):
    if isinstance(private_key, rsa.RSAPrivateKey):
        return "rsa"
    if isinstance(private_key, Ed25519PrivateKey):
        return "ed25519"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ec"
    raise ValueError(f"Unsupported key type: {type(private_key).__name__}")


def generate_csr(private_key, common_name, path):
    # Ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logger.debug(f"Generating CSR for CN={common_name} at {path}")

    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ]
        )
    )

    # Ed25519 不使用外部哈希算法，cryptography 要求传 None（RFC 8410）；
    # RSA/ECDSA 使用 SHA-256。
    hash_algorithm = None if isinstance(private_key, Ed25519PrivateKey) else hashes.SHA256()
    csr = builder.sign(private_key, hash_algorithm)

    with open(path, "wb") as f:
        f.write(csr.public_bytes(serialization.Encoding.PEM))

    return csr
