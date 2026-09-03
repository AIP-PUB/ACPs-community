"""单元测试 — keys 模块（密钥生成、保存、加载、CSR 生成）。"""

import os
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acps_cli.ca.keys import (
    generate_csr,
    generate_private_key,
    load_private_key,
    private_key_type_name,
    save_private_key,
)


@pytest.mark.unit
class TestGeneratePrivateKey:
    def test_ed25519_key_default(self):
        key = generate_private_key()
        assert isinstance(key, Ed25519PrivateKey)

    def test_ec_key_explicit(self):
        key = generate_private_key("ec")
        assert isinstance(key, ec.EllipticCurvePrivateKey)

    def test_rsa_key(self):
        key = generate_private_key("rsa")
        assert isinstance(key, rsa.RSAPrivateKey)
        assert key.key_size == 2048

    def test_ed25519_key_explicit(self):
        key = generate_private_key("ed25519")
        assert isinstance(key, Ed25519PrivateKey)


@pytest.mark.unit
class TestSaveAndLoadPrivateKey:
    def test_save_creates_file_with_600_permissions(self, tmp_path):
        key = generate_private_key("ec")
        key_path = str(tmp_path / "test.key")
        save_private_key(key, key_path)

        assert os.path.exists(key_path)
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600

    def test_save_creates_parent_directories(self, tmp_path):
        key = generate_private_key("ec")
        key_path = str(tmp_path / "deep" / "nested" / "test.key")
        save_private_key(key, key_path)
        assert os.path.exists(key_path)

    def test_roundtrip_ec_key(self, tmp_path):
        key = generate_private_key("ec")
        key_path = str(tmp_path / "ec.key")
        save_private_key(key, key_path)
        loaded = load_private_key(key_path)

        assert isinstance(loaded, ec.EllipticCurvePrivateKey)
        # 比较公钥的序列化结果确认一致
        orig_pub = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        loaded_pub = loaded.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert orig_pub == loaded_pub

    def test_roundtrip_rsa_key(self, tmp_path):
        key = generate_private_key("rsa")
        key_path = str(tmp_path / "rsa.key")
        save_private_key(key, key_path)
        loaded = load_private_key(key_path)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_roundtrip_ed25519_key(self, tmp_path):
        key = generate_private_key("ed25519")
        key_path = str(tmp_path / "ed25519.key")
        save_private_key(key, key_path)
        loaded = load_private_key(key_path)
        assert isinstance(loaded, Ed25519PrivateKey)


@pytest.mark.unit
class TestPrivateKeyTypeName:
    def test_detects_supported_key_types(self):
        assert private_key_type_name(generate_private_key("ed25519")) == "ed25519"
        assert private_key_type_name(generate_private_key("ec")) == "ec"
        assert private_key_type_name(generate_private_key("rsa")) == "rsa"


@pytest.mark.unit
class TestGenerateCSR:
    def test_csr_subject_matches_aic(self, tmp_path, sample_aic):
        key = generate_private_key("ec")
        csr_path = str(tmp_path / "test.csr")
        csr = generate_csr(key, sample_aic, csr_path)

        # CSR 的 CN 应等于 AIC
        cn = csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        assert cn == sample_aic

    def test_csr_file_written(self, tmp_path, sample_aic):
        key = generate_private_key("ec")
        csr_path = str(tmp_path / "test.csr")
        generate_csr(key, sample_aic, csr_path)
        assert os.path.exists(csr_path)
        assert os.path.getsize(csr_path) > 0

    def test_csr_file_is_valid_pem(self, tmp_path, sample_aic):
        key = generate_private_key("ec")
        csr_path = str(tmp_path / "test.csr")
        generate_csr(key, sample_aic, csr_path)

        with open(csr_path, "rb") as f:
            loaded_csr = x509.load_pem_x509_csr(f.read())
        assert loaded_csr.is_signature_valid

    def test_csr_creates_parent_directories(self, tmp_path, sample_aic):
        key = generate_private_key("ec")
        csr_path = str(tmp_path / "sub" / "dir" / "test.csr")
        generate_csr(key, sample_aic, csr_path)
        assert os.path.exists(csr_path)

    def test_csr_with_rsa_key(self, tmp_path, sample_aic):
        key = generate_private_key("rsa")
        csr_path = str(tmp_path / "rsa.csr")
        csr = generate_csr(key, sample_aic, csr_path)
        assert csr.is_signature_valid

    def test_csr_with_ed25519_key(self, tmp_path, sample_aic):
        key = generate_private_key("ed25519")
        csr_path = str(tmp_path / "ed25519.csr")
        csr = generate_csr(key, sample_aic, csr_path)
        assert csr.is_signature_valid
