"""
OCSP (Online Certificate Status Protocol) 测试

测试OCSP状态查询、响应器信息和统计功能
"""

import base64
import datetime
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import NameOID, ocsp
from fastapi.testclient import TestClient
from sqlalchemy import desc, literal_column
from sqlmodel import Session, select

from app.common import (
    Certificate,
    CertificateStatus,
    CertificateType,
    OCSPRequest,
    OCSPResponder,
    OCSPResponse,
    OCSPResponseStatus,
    OCSPService,
    RevocationReason,
    beijing_now,
)
from app.common.ocsp_service import build_ocsp_responder_certificate
from app.core.db_session import engine

pytestmark = pytest.mark.ocsp


@dataclass
class OCSPCryptoSetup:
    request_der: bytes
    serial_number: str
    issuer_key_hash: str
    issuer_name_hash: str
    hash_algorithm: str
    ca_cert: x509.Certificate
    responder_cert: x509.Certificate
    responder_name: str
    responder_key_hash_hex: str
    responder_key_hash_bytes: bytes
    certificate_id: UUID
    responder_id: UUID


@pytest.fixture
def db_session():
    """数据库会话fixture"""
    with Session(engine) as session:
        yield session


@pytest.fixture
async def ocsp_service(async_db_session):
    """OCSP服务fixture"""
    return OCSPService(async_db_session)


def _build_test_ca(common_name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now_utc = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc - datetime.timedelta(days=1))
        .not_valid_after(now_utc + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    return ca_key, ca_cert


def _build_ocsp_responder(
    *,
    name: str,
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    endpoint: str = "http://ocsp.test",
) -> tuple[OCSPResponder, rsa.RSAPrivateKey, x509.Certificate]:
    responder_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responder_cert = build_ocsp_responder_certificate(responder_key.public_key(), ca_cert, ca_key)
    responder = OCSPResponder(
        name=name,
        certificate_pem=responder_cert.public_bytes(serialization.Encoding.PEM).decode(),
        private_key_pem=responder_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        certificate_serial=format(responder_cert.serial_number, "x"),
        endpoints={"primary": endpoint},
        supported_extensions=["nonce"],
        is_active=True,
    )
    return responder, responder_key, responder_cert


@pytest.fixture
def ocsp_responder(db_session):
    """创建OCSP响应器"""
    ca_key, ca_cert = _build_test_ca("Test CA")
    responder, _, _ = _build_ocsp_responder(
        name="Test OCSP Responder",
        ca_cert=ca_cert,
        ca_key=ca_key,
        endpoint="http://ocsp.example.com",
    )
    db_session.add(responder)
    db_session.commit()
    db_session.refresh(responder)
    return responder


@pytest.fixture
def valid_certificate(db_session):
    """创建有效证书"""
    cert = Certificate(
        certificate_type="user",
        serial_number=f"VALID{uuid4().hex[:16].upper()}",
        subject="CN=valid.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.VALID,
        issued_at=beijing_now(),
        expires_at=beijing_now() + timedelta(days=365),
        certificate_pem="-----BEGIN CERTIFICATE-----\nVALID_CERT\n-----END CERTIFICATE-----",
        public_key="VALID_PUBLIC_KEY",
    )
    db_session.add(cert)
    db_session.commit()
    db_session.refresh(cert)
    return cert


@pytest.fixture
def revoked_certificate(db_session):
    """创建已吊销证书"""
    cert = Certificate(
        certificate_type="user",
        serial_number=f"REVOKED{uuid4().hex[:12].upper()}",
        subject="CN=revoked.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.REVOKED,
        issued_at=beijing_now() - timedelta(days=30),
        expires_at=beijing_now() + timedelta(days=335),
        revoked_at=beijing_now() - timedelta(days=1),
        revocation_reason=RevocationReason.KEY_COMPROMISE,
        certificate_pem="-----BEGIN CERTIFICATE-----\nREVOKED_CERT\n-----END CERTIFICATE-----",
        public_key="REVOKED_PUBLIC_KEY",
    )
    db_session.add(cert)
    db_session.commit()
    db_session.refresh(cert)
    return cert


@pytest.fixture
def expired_certificate(db_session):
    """创建已过期证书"""
    cert = Certificate(
        certificate_type="user",
        serial_number=f"EXPIRED{uuid4().hex[:12].upper()}",
        subject="CN=expired.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.EXPIRED,
        issued_at=beijing_now() - timedelta(days=400),
        expires_at=beijing_now() - timedelta(days=35),
        certificate_pem="-----BEGIN CERTIFICATE-----\nEXPIRED_CERT\n-----END CERTIFICATE-----",
        public_key="EXPIRED_PUBLIC_KEY",
    )
    db_session.add(cert)
    db_session.commit()
    db_session.refresh(cert)
    return cert


@pytest.fixture
def ocsp_crypto_setup(db_session):
    """生成可用于端到端验证的 OCSP 请求和响应环境"""

    ca_key, ca_cert = _build_test_ca("Test CA")
    ca_subject = ca_cert.subject
    now_utc = datetime.datetime.now(datetime.UTC)

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Leaf Cert")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc - datetime.timedelta(days=1))
        .not_valid_after(now_utc + datetime.timedelta(days=365))
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    certificate = Certificate(
        certificate_type=CertificateType.USER,
        serial_number=str(leaf_cert.serial_number),
        subject=leaf_cert.subject.rfc4514_string(),
        issuer=leaf_cert.issuer.rfc4514_string(),
        status=CertificateStatus.VALID,
        issued_at=beijing_now(),
        expires_at=beijing_now() + datetime.timedelta(days=365),
        certificate_pem=leaf_cert.public_bytes(serialization.Encoding.PEM).decode(),
        public_key=leaf_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    db_session.add(certificate)

    responder_name = "Crypto Test Responder"
    responder, responder_key, responder_cert = _build_ocsp_responder(
        name=responder_name,
        ca_cert=ca_cert,
        ca_key=ca_key,
    )
    db_session.add(responder)
    db_session.commit()

    db_session.refresh(certificate)
    db_session.refresh(responder)

    builder = ocsp.OCSPRequestBuilder()
    builder = builder.add_certificate(leaf_cert, ca_cert, hashes.SHA1())
    ocsp_request = builder.build()
    request_der = ocsp_request.public_bytes(serialization.Encoding.DER)

    responder_key_hash_bytes = (
        ocsp.OCSPResponseBuilder()
        .add_response(
            cert=leaf_cert,
            issuer=ca_cert,
            algorithm=hashes.SHA1(),
            cert_status=x509.ocsp.OCSPCertStatus.GOOD,
            this_update=now_utc,
            next_update=now_utc + datetime.timedelta(hours=24),
            revocation_time=None,
            revocation_reason=None,
        )
        .responder_id(x509.ocsp.OCSPResponderEncoding.HASH, responder_cert)
        .certificates([responder_cert])
        .sign(private_key=responder_key, algorithm=hashes.SHA256())
        .responder_key_hash
    )
    assert responder_key_hash_bytes is not None

    setup = OCSPCryptoSetup(
        request_der=request_der,
        serial_number=str(leaf_cert.serial_number),
        issuer_key_hash=ocsp_request.issuer_key_hash.hex(),
        issuer_name_hash=ocsp_request.issuer_name_hash.hex(),
        hash_algorithm=ocsp_request.hash_algorithm.name,
        ca_cert=ca_cert,
        responder_cert=responder_cert,
        responder_name=responder_name,
        responder_key_hash_hex=responder_key_hash_bytes.hex(),
        responder_key_hash_bytes=responder_key_hash_bytes,
        certificate_id=certificate.id,
        responder_id=responder.id,
    )

    yield setup

    responses = db_session.exec(
        select(OCSPResponse).where(OCSPResponse.certificate_serial == setup.serial_number)
    ).all()
    for response in responses:
        db_session.delete(response)

    requests = db_session.exec(select(OCSPRequest).where(OCSPRequest.certificate_serial == setup.serial_number)).all()
    for request in requests:
        db_session.delete(request)

    certificate_obj = db_session.get(Certificate, setup.certificate_id)
    if certificate_obj:
        db_session.delete(certificate_obj)

    responder_obj = db_session.get(OCSPResponder, setup.responder_id)
    if responder_obj:
        db_session.delete(responder_obj)

    db_session.commit()


@pytest.fixture
def ocsp_sha256_crypto_setup(db_session):
    """生成使用 SHA-256 CertID 的 OCSP 请求和响应环境。"""

    ca_key, ca_cert = _build_test_ca("SHA256 Test CA")
    ca_subject = ca_cert.subject
    now_utc = datetime.datetime.now(datetime.UTC)

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SHA256 Leaf Cert")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc - datetime.timedelta(days=1))
        .not_valid_after(now_utc + datetime.timedelta(days=365))
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    certificate = Certificate(
        certificate_type=CertificateType.USER,
        serial_number=str(leaf_cert.serial_number),
        subject=leaf_cert.subject.rfc4514_string(),
        issuer=leaf_cert.issuer.rfc4514_string(),
        status=CertificateStatus.VALID,
        issued_at=beijing_now(),
        expires_at=beijing_now() + datetime.timedelta(days=365),
        certificate_pem=leaf_cert.public_bytes(serialization.Encoding.PEM).decode(),
        public_key=leaf_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    db_session.add(certificate)

    responder_name = "SHA256 Crypto Test Responder"
    responder, responder_key, responder_cert = _build_ocsp_responder(
        name=responder_name,
        ca_cert=ca_cert,
        ca_key=ca_key,
    )
    db_session.add(responder)
    db_session.commit()
    db_session.refresh(certificate)
    db_session.refresh(responder)

    builder = ocsp.OCSPRequestBuilder()
    builder = builder.add_certificate(leaf_cert, ca_cert, hashes.SHA256())
    ocsp_request = builder.build()
    request_der = ocsp_request.public_bytes(serialization.Encoding.DER)

    responder_key_hash_bytes = (
        ocsp.OCSPResponseBuilder()
        .add_response(
            cert=leaf_cert,
            issuer=ca_cert,
            algorithm=hashes.SHA256(),
            cert_status=x509.ocsp.OCSPCertStatus.GOOD,
            this_update=now_utc,
            next_update=now_utc + datetime.timedelta(hours=24),
            revocation_time=None,
            revocation_reason=None,
        )
        .responder_id(x509.ocsp.OCSPResponderEncoding.HASH, responder_cert)
        .certificates([responder_cert])
        .sign(private_key=responder_key, algorithm=hashes.SHA256())
        .responder_key_hash
    )
    assert responder_key_hash_bytes is not None

    setup = OCSPCryptoSetup(
        request_der=request_der,
        serial_number=str(leaf_cert.serial_number),
        issuer_key_hash=ocsp_request.issuer_key_hash.hex(),
        issuer_name_hash=ocsp_request.issuer_name_hash.hex(),
        hash_algorithm=ocsp_request.hash_algorithm.name,
        ca_cert=ca_cert,
        responder_cert=responder_cert,
        responder_name=responder_name,
        responder_key_hash_hex=responder_key_hash_bytes.hex(),
        responder_key_hash_bytes=responder_key_hash_bytes,
        certificate_id=certificate.id,
        responder_id=responder.id,
    )

    yield setup

    responses = db_session.exec(
        select(OCSPResponse).where(OCSPResponse.certificate_serial == setup.serial_number)
    ).all()
    for response in responses:
        db_session.delete(response)

    requests = db_session.exec(select(OCSPRequest).where(OCSPRequest.certificate_serial == setup.serial_number)).all()
    for request in requests:
        db_session.delete(request)

    certificate_obj = db_session.get(Certificate, setup.certificate_id)
    if certificate_obj:
        db_session.delete(certificate_obj)

    responder_obj = db_session.get(OCSPResponder, setup.responder_id)
    if responder_obj:
        db_session.delete(responder_obj)

    db_session.commit()


@pytest.fixture
def ocsp_expired_crypto_setup(db_session):
    """生成已过期证书的 OCSP 请求和响应环境"""

    ca_key, ca_cert = _build_test_ca("Test CA")
    ca_subject = ca_cert.subject
    now_utc = datetime.datetime.now(datetime.UTC)

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Expired Leaf Cert")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc - datetime.timedelta(days=60))
        .not_valid_after(now_utc - datetime.timedelta(days=1))
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    certificate = Certificate(
        certificate_type=CertificateType.USER,
        serial_number=str(leaf_cert.serial_number),
        subject=leaf_cert.subject.rfc4514_string(),
        issuer=leaf_cert.issuer.rfc4514_string(),
        status=CertificateStatus.EXPIRED,
        issued_at=beijing_now() - datetime.timedelta(days=30),
        expires_at=beijing_now() - datetime.timedelta(days=1),
        certificate_pem=leaf_cert.public_bytes(serialization.Encoding.PEM).decode(),
        public_key=leaf_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    db_session.add(certificate)

    responder_name = "Expired Crypto Test Responder"
    responder, responder_key, responder_cert = _build_ocsp_responder(
        name=responder_name,
        ca_cert=ca_cert,
        ca_key=ca_key,
    )
    db_session.add(responder)
    db_session.commit()
    db_session.refresh(certificate)
    db_session.refresh(responder)

    builder = ocsp.OCSPRequestBuilder()
    builder = builder.add_certificate(leaf_cert, ca_cert, hashes.SHA1())
    ocsp_request = builder.build()
    request_der = ocsp_request.public_bytes(serialization.Encoding.DER)

    responder_key_hash_bytes = (
        ocsp.OCSPResponseBuilder()
        .add_response(
            cert=leaf_cert,
            issuer=ca_cert,
            algorithm=hashes.SHA1(),
            cert_status=x509.ocsp.OCSPCertStatus.UNKNOWN,
            this_update=now_utc,
            next_update=now_utc + datetime.timedelta(hours=24),
            revocation_time=None,
            revocation_reason=None,
        )
        .responder_id(x509.ocsp.OCSPResponderEncoding.HASH, responder_cert)
        .certificates([responder_cert])
        .sign(private_key=responder_key, algorithm=hashes.SHA256())
        .responder_key_hash
    )
    assert responder_key_hash_bytes is not None

    setup = OCSPCryptoSetup(
        request_der=request_der,
        serial_number=str(leaf_cert.serial_number),
        issuer_key_hash=ocsp_request.issuer_key_hash.hex(),
        issuer_name_hash=ocsp_request.issuer_name_hash.hex(),
        hash_algorithm=ocsp_request.hash_algorithm.name,
        ca_cert=ca_cert,
        responder_cert=responder_cert,
        responder_name=responder_name,
        responder_key_hash_hex=responder_key_hash_bytes.hex(),
        responder_key_hash_bytes=responder_key_hash_bytes,
        certificate_id=certificate.id,
        responder_id=responder.id,
    )

    yield setup

    responses = db_session.exec(
        select(OCSPResponse).where(OCSPResponse.certificate_serial == setup.serial_number)
    ).all()
    for response in responses:
        db_session.delete(response)

    requests = db_session.exec(select(OCSPRequest).where(OCSPRequest.certificate_serial == setup.serial_number)).all()
    for request in requests:
        db_session.delete(request)

    certificate_obj = db_session.get(Certificate, setup.certificate_id)
    if certificate_obj:
        db_session.delete(certificate_obj)

    responder_obj = db_session.get(OCSPResponder, setup.responder_id)
    if responder_obj:
        db_session.delete(responder_obj)

    db_session.commit()


class TestOCSPCertificateStatusAPI:
    """测试OCSP证书状态查询API"""

    def test_get_valid_certificate_status(self, client: TestClient, valid_certificate) -> None:
        """测试查询有效证书状态"""
        response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
        assert response.status_code == 200

        data = response.json()
        assert data["serialNumber"] == valid_certificate.serial_number
        assert data["certificateStatus"] == "good"
        assert "thisUpdate" in data
        assert "nextUpdate" in data

    def test_get_revoked_certificate_status(self, client: TestClient, revoked_certificate) -> None:
        """测试查询已吊销证书状态"""
        response = client.get(f"/acps-atr-v2/ocsp/certificate/{revoked_certificate.serial_number}")
        assert response.status_code == 200

        data = response.json()
        assert data["serialNumber"] == revoked_certificate.serial_number
        assert data["certificateStatus"] == "revoked"
        assert "revocationTime" in data
        assert "revocationReason" in data
        assert data["revocationReason"] == "keyCompromise"

    def test_get_expired_certificate_status(self, client: TestClient, expired_certificate) -> None:
        """测试查询已过期证书状态"""
        response = client.get(f"/acps-atr-v2/ocsp/certificate/{expired_certificate.serial_number}")
        assert response.status_code == 200

        data = response.json()
        assert data["serialNumber"] == expired_certificate.serial_number
        assert data["certificateStatus"] == "expired"

    def test_get_unknown_certificate_status(self, client: TestClient) -> None:
        """测试查询不存在证书状态"""
        response = client.get("/acps-atr-v2/ocsp/certificate/NONEXISTENT123456")
        assert response.status_code == 200

        data = response.json()
        assert data["serialNumber"] == "NONEXISTENT123456"
        assert data["certificateStatus"] == "unknown"

    def test_certificate_status_consistency(self, client: TestClient, valid_certificate) -> None:
        """测试证书状态的一致性"""
        # 多次查询同一证书，状态应该保持一致
        responses = []
        for _ in range(3):
            response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
            assert response.status_code == 200
            responses.append(response.json())

        # 所有响应的状态应该相同
        statuses = [r["certificateStatus"] for r in responses]
        assert len(set(statuses)) == 1


class TestOCSPBatchAPI:
    """测试OCSP批量查询API"""

    def test_batch_certificate_status(self, client: TestClient, valid_certificate, revoked_certificate) -> None:
        """测试批量查询证书状态"""
        request_data = {
            "certificates": [
                {
                    "serial_number": valid_certificate.serial_number,
                    "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
                },
                {
                    "serial_number": revoked_certificate.serial_number,
                    "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
                },
                {
                    "serial_number": "NONEXISTENT123",
                    "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
                },
            ]
        }

        response = client.post("/acps-atr-v2/ocsp/batch", json=request_data)
        assert response.status_code == 200

        data = response.json()
        assert "responses" in data
        assert len(data["responses"]) == 3

        # 验证响应内容
        responses_by_serial = {r["serial_number"]: r for r in data["responses"]}

        # 有效证书
        valid_resp = responses_by_serial[valid_certificate.serial_number]
        assert valid_resp["status"] == "good"

        # 吊销证书
        revoked_resp = responses_by_serial[revoked_certificate.serial_number]
        assert revoked_resp["status"] == "revoked"

        # 不存在证书
        unknown_resp = responses_by_serial["NONEXISTENT123"]
        assert unknown_resp["status"] == "unknown"

    def test_empty_batch_request(self, client: TestClient) -> None:
        """测试空的批量请求"""
        request_data: dict[str, list[dict[str, str]]] = {"certificates": []}

        response = client.post("/acps-atr-v2/ocsp/batch", json=request_data)
        assert response.status_code == 200

        data = response.json()
        assert data["responses"] == []

    def test_large_batch_request(self, client: TestClient, valid_certificate) -> None:
        """测试大批量请求"""
        # 创建100个查询请求
        certificates = [
            {
                "serial_number": f"TEST{i:04d}",
                "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
            }
            for i in range(100)
        ]
        certificates.append(
            {
                "serial_number": valid_certificate.serial_number,
                "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
            }
        )

        request_data = {"certificates": certificates}

        response = client.post("/acps-atr-v2/ocsp/batch", json=request_data)
        assert response.status_code == 200

        data = response.json()
        assert len(data["responses"]) == 101


class TestOCSPResponderAPI:
    """测试OCSP响应器API"""

    def test_get_responder_info(self, client: TestClient, ocsp_responder) -> None:
        """测试获取OCSP响应器信息"""
        response = client.get("/acps-atr-v2/ocsp/responder/info")
        assert response.status_code == 200

        data = response.json()
        assert "endpoints" in data
        assert "responder" in data
        assert "service_info" in data
        assert data["responder"]["name"] == ocsp_responder.name
        assert "key_hash" in data["responder"]

    def test_get_responder_info_bootstraps_default_responder(self, client: TestClient, db_session) -> None:
        """测试没有响应器时会自动 bootstrap 默认 responder。"""
        # 删除所有响应器
        for responder in db_session.exec(select(OCSPResponder)).all():
            db_session.delete(responder)
        db_session.commit()

        response = client.get("/acps-atr-v2/ocsp/responder/info")
        assert response.status_code == 200
        data = response.json()
        assert data["responder"]["name"] == "Agent CA OCSP Responder"
        assert "key_hash" in data["responder"]


class TestOCSPStatsAPI:
    """测试OCSP统计API"""

    def test_get_ocsp_statistics(self, client: TestClient) -> None:
        """测试获取OCSP统计信息"""
        response = client.get("/acps-atr-v2/ocsp/stats")
        assert response.status_code == 200

        data = response.json()
        assert "total_requests" in data
        assert "good_responses" in data
        assert "revoked_responses" in data
        assert "unknown_responses" in data
        assert "average_response_time_ms" in data
        assert "last_24h_requests" in data

        # 统计数据应该是非负数
        for key in [
            "total_requests",
            "good_responses",
            "revoked_responses",
            "unknown_responses",
        ]:
            assert data[key] >= 0

        assert data["average_response_time_ms"] >= 0.0

    def test_stats_update_after_requests(self, client: TestClient, valid_certificate) -> None:
        """测试请求后统计数据更新"""
        # 获取初始统计
        initial_response = client.get("/acps-atr-v2/ocsp/stats")
        initial_data = initial_response.json()
        initial_total = initial_data["total_requests"]

        # 执行一些OCSP查询
        for _ in range(3):
            client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")

        # 获取更新后的统计
        updated_response = client.get("/acps-atr-v2/ocsp/stats")
        updated_data = updated_response.json()

        assert updated_data["total_requests"] >= initial_total


class TestOCSPService:
    """测试OCSP服务层功能"""

    async def test_get_certificate_status_valid(self, ocsp_service, valid_certificate) -> None:
        """测试获取有效证书状态"""
        status = await ocsp_service.get_certificate_status(valid_certificate.serial_number)

        assert status is not None
        assert status["serialNumber"] == valid_certificate.serial_number
        assert status["certificateStatus"] == "good"

    async def test_get_certificate_status_revoked(self, ocsp_service, revoked_certificate) -> None:
        """测试获取已吊销证书状态"""
        status = await ocsp_service.get_certificate_status(revoked_certificate.serial_number)

        assert status is not None
        assert status["serialNumber"] == revoked_certificate.serial_number
        assert status["certificateStatus"] == "revoked"
        assert status["revocationReason"] == "keyCompromise"

    async def test_get_certificate_status_unknown(self, ocsp_service) -> None:
        """测试获取未知证书状态"""
        status = await ocsp_service.get_certificate_status("UNKNOWN_SERIAL")

        assert status is not None
        assert status["serialNumber"] == "UNKNOWN_SERIAL"
        assert status["certificateStatus"] == "unknown"

    async def test_process_ocsp_request_records_modern_fields(
        self, ocsp_service, ocsp_crypto_setup, monkeypatch
    ) -> None:
        """验证 cryptography 新版属性与响应者信息记录逻辑"""

        mock_ca_manager = MagicMock()
        mock_ca_manager.ca_cert = ocsp_crypto_setup.ca_cert
        monkeypatch.setattr("app.common.ocsp_service.get_ca_manager", lambda: mock_ca_manager)

        response_der, processing_time = await ocsp_service.process_ocsp_request(ocsp_crypto_setup.request_der)

        assert processing_time >= 0

        ocsp_response = x509.ocsp.load_der_ocsp_response(response_der)
        assert ocsp_response.response_status == x509.ocsp.OCSPResponseStatus.SUCCESSFUL
        assert ocsp_response.certificate_status == x509.ocsp.OCSPCertStatus.GOOD
        assert ocsp_response.responder_key_hash == ocsp_crypto_setup.responder_key_hash_bytes
        assert ocsp_response.certificates
        assert ocsp_response.certificates[0].subject == ocsp_crypto_setup.responder_cert.subject

        stored_response_result = await ocsp_service.session.execute(
            select(OCSPResponse)
            .where(OCSPResponse.certificate_serial == ocsp_crypto_setup.serial_number)
            .order_by(desc(literal_column("created_at")))
        )
        stored_response = stored_response_result.scalars().first()
        assert stored_response is not None
        assert stored_response.cert_status == OCSPResponseStatus.GOOD
        assert stored_response.responder_id == ocsp_crypto_setup.responder_name
        assert stored_response.responder_key_hash == ocsp_crypto_setup.responder_key_hash_hex

        stored_request_result = await ocsp_service.session.execute(
            select(OCSPRequest).where(OCSPRequest.id == stored_response.request_id)
        )
        stored_request = stored_request_result.scalars().first()
        assert stored_request is not None
        assert stored_request.certificate_serial == ocsp_crypto_setup.serial_number
        assert stored_request.issuer_key_hash == ocsp_crypto_setup.issuer_key_hash
        assert stored_request.issuer_name_hash == ocsp_crypto_setup.issuer_name_hash
        assert stored_request.hash_algorithm == ocsp_crypto_setup.hash_algorithm

    async def test_process_ocsp_request_preserves_sha256_certid_algorithm(
        self,
        ocsp_service,
        ocsp_sha256_crypto_setup,
        monkeypatch,
    ) -> None:
        """验证 OCSP 响应中的 CertID 哈希算法与 SHA-256 请求保持一致。"""

        mock_ca_manager = MagicMock()
        mock_ca_manager.ca_cert = ocsp_sha256_crypto_setup.ca_cert
        monkeypatch.setattr("app.common.ocsp_service.get_ca_manager", lambda: mock_ca_manager)

        response_der, processing_time = await ocsp_service.process_ocsp_request(ocsp_sha256_crypto_setup.request_der)

        assert processing_time >= 0

        ocsp_response = x509.ocsp.load_der_ocsp_response(response_der)
        assert ocsp_response.response_status == x509.ocsp.OCSPResponseStatus.SUCCESSFUL
        assert ocsp_response.certificate_status == x509.ocsp.OCSPCertStatus.GOOD
        assert ocsp_response.hash_algorithm.name == ocsp_sha256_crypto_setup.hash_algorithm

        stored_request_result = await ocsp_service.session.execute(
            select(OCSPRequest).where(OCSPRequest.certificate_serial == ocsp_sha256_crypto_setup.serial_number)
        )
        stored_request = stored_request_result.scalars().first()
        assert stored_request is not None
        assert stored_request.hash_algorithm == "sha256"

    async def test_process_ocsp_request_maps_expired_to_unknown(
        self,
        ocsp_service,
        ocsp_expired_crypto_setup,
        monkeypatch,
    ) -> None:
        """验证过期证书在 DER 响应中按 RFC 6960 回落为 UNKNOWN。"""

        mock_ca_manager = MagicMock()
        mock_ca_manager.ca_cert = ocsp_expired_crypto_setup.ca_cert
        monkeypatch.setattr("app.common.ocsp_service.get_ca_manager", lambda: mock_ca_manager)

        response_der, processing_time = await ocsp_service.process_ocsp_request(ocsp_expired_crypto_setup.request_der)

        assert processing_time >= 0

        ocsp_response = x509.ocsp.load_der_ocsp_response(response_der)
        assert ocsp_response.response_status == x509.ocsp.OCSPResponseStatus.SUCCESSFUL
        assert ocsp_response.certificate_status == x509.ocsp.OCSPCertStatus.UNKNOWN

        stored_response_result = await ocsp_service.session.execute(
            select(OCSPResponse)
            .where(OCSPResponse.certificate_serial == ocsp_expired_crypto_setup.serial_number)
            .order_by(desc(literal_column("created_at")))
        )
        stored_response = stored_response_result.scalars().first()
        assert stored_response is not None
        assert stored_response.cert_status == OCSPResponseStatus.UNKNOWN

    async def test_get_responder_info(self, ocsp_service, ocsp_responder) -> None:
        """测试获取响应器信息"""
        info = await ocsp_service.get_responder_info()

        assert info is not None
        assert "endpoints" in info
        assert "responder" in info
        assert info["responder"]["name"] == ocsp_responder.name
        assert "key_hash" in info["responder"]

    async def test_get_ocsp_statistics(self, ocsp_service) -> None:
        """测试获取OCSP统计"""
        stats = await ocsp_service.get_ocsp_statistics()

        assert stats is not None
        assert "total_requests" in stats
        assert "good_responses" in stats
        assert "revoked_responses" in stats
        assert "unknown_responses" in stats

    async def test_batch_certificate_status(self, ocsp_service, valid_certificate, revoked_certificate) -> None:
        """测试批量证书状态查询"""
        certificates = [
            {"serial_number": valid_certificate.serial_number},
            {"serial_number": revoked_certificate.serial_number},
            {"serial_number": "NONEXISTENT"},
        ]

        responses = await ocsp_service.batch_certificate_status(certificates)

        assert len(responses) == 3

        # 验证响应
        serials = [r["serial_number"] for r in responses]
        assert valid_certificate.serial_number in serials
        assert revoked_certificate.serial_number in serials
        assert "NONEXISTENT" in serials


class TestOCSPIntegration:
    """测试OCSP集成功能"""

    def test_certificate_revocation_updates_ocsp(self, client: TestClient, valid_certificate) -> None:
        """测试证书吊销后OCSP状态更新"""
        # 初始状态应该是good
        initial_response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
        assert initial_response.status_code == 200
        assert initial_response.json()["certificateStatus"] == "good"

        # 吊销证书
        revoke_response = client.post(
            f"/admin/certificates/{valid_certificate.id}/revoke",
            params={"reason": "keyCompromise"},
        )
        assert revoke_response.status_code == 200

        # 再次查询OCSP状态
        updated_response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
        assert updated_response.status_code == 200

        updated_data = updated_response.json()
        assert updated_data["certificateStatus"] == "revoked"
        assert updated_data["revocationReason"] == "keyCompromise"

    def test_ocsp_response_format(self, client: TestClient, valid_certificate) -> None:
        """测试OCSP响应格式的正确性"""
        response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
        assert response.status_code == 200

        data = response.json()

        # 验证必需字段
        required_fields = ["serialNumber", "certificateStatus", "thisUpdate"]
        for field in required_fields:
            assert field in data

        # 验证状态值
        assert data["certificateStatus"] in ["good", "revoked", "expired", "unknown"]

        # 验证时间格式
        assert isinstance(data["thisUpdate"], str)
        if "nextUpdate" in data:
            assert isinstance(data["nextUpdate"], str)

    def test_ocsp_error_handling(self, client: TestClient) -> None:
        """测试OCSP错误处理"""
        # 测试无效的序列号格式
        response = client.get("/acps-atr-v2/ocsp/certificate/")
        assert response.status_code == 400  # FastAPI validation error for missing path parameter

        # 测试特殊字符
        response = client.get("/acps-atr-v2/ocsp/certificate/INVALID!@#$%")
        assert response.status_code == 200
        assert response.json()["certificateStatus"] == "unknown"

    def test_ocsp_performance(self, client: TestClient, valid_certificate) -> None:
        """测试OCSP性能"""
        import time

        # 测试多个并发请求的响应时间
        start_time = time.time()
        for _ in range(10):
            response = client.get(f"/acps-atr-v2/ocsp/certificate/{valid_certificate.serial_number}")
            assert response.status_code == 200
        end_time = time.time()

        # 平均每个请求应该在合理时间内完成（这里设置为1秒）
        average_time = (end_time - start_time) / 10
        assert average_time < 1.0, f"Average response time {average_time}s is too slow"


class TestOCSPTimezoneHandling:
    """测试OCSP时区处理"""

    async def test_timezone_aware_comparison(self, ocsp_service, db_session) -> None:
        """测试时区感知的时间比较"""
        # 创建一个即将过期的证书
        soon_expired_cert = Certificate(
            certificate_type="user",
            serial_number=f"SOONEXP{uuid4().hex[:12].upper()}",
            subject="CN=soonexpired.example.com,O=Test Org,C=CN",
            issuer="CN=Test CA,O=Test CA,C=CN",
            status=CertificateStatus.VALID,
            issued_at=beijing_now() - timedelta(days=30),
            expires_at=beijing_now() + timedelta(hours=1),  # 1小时后过期
            certificate_pem="-----BEGIN CERTIFICATE-----\nSOON_EXPIRED_CERT\n-----END CERTIFICATE-----",
            public_key="SOON_EXPIRED_PUBLIC_KEY",
        )
        db_session.add(soon_expired_cert)
        db_session.commit()

        # 查询状态，应该还是good（未过期）
        status = await ocsp_service.get_certificate_status(soon_expired_cert.serial_number)
        assert status["certificateStatus"] == "good"

    async def test_expired_certificate_detection(self, ocsp_service, db_session) -> None:
        """测试过期证书检测"""
        # 创建已过期的证书（设置为VALID状态但过期时间已过）
        past_expired_cert = Certificate(
            certificate_type="user",
            serial_number=f"PASTEXP{uuid4().hex[:12].upper()}",
            subject="CN=pastexpired.example.com,O=Test Org,C=CN",
            issuer="CN=Test CA,O=Test CA,C=CN",
            status=CertificateStatus.VALID,  # 数据库中状态为VALID
            issued_at=beijing_now() - timedelta(days=400),
            expires_at=beijing_now() - timedelta(days=1),  # 昨天过期
            certificate_pem="-----BEGIN CERTIFICATE-----\nPAST_EXPIRED_CERT\n-----END CERTIFICATE-----",
            public_key="PAST_EXPIRED_PUBLIC_KEY",
        )
        db_session.add(past_expired_cert)
        db_session.commit()

        # OCSP应该检测到过期并返回expired状态
        status = await ocsp_service.get_certificate_status(past_expired_cert.serial_number)
        assert status["certificateStatus"] == "expired"


class TestOCSPStandardAPI:
    """测试标准OCSP API接口 (RFC 6960)"""

    def test_ocsp_post_request(self, anonymous_client: TestClient) -> None:
        """测试POST方法查询OCSP"""
        # 构造一个伪造的OCSP请求 (DER编码)
        # 这里只是为了测试API能否接收请求，不需要真实的OCSP请求结构
        # 因为我们的Mock服务或者实际服务会解析它
        # 注意：实际服务会尝试解析ASN.1，所以我们需要一个最小合法的ASN.1结构或者Mock掉解析过程
        # 这里我们发送一个简单的字节序列，预期会返回400或者解析错误，
        # 但如果我们要测试成功路径，我们需要构造合法的OCSPRequest
        # 鉴于构造复杂的ASN.1比较麻烦，我们这里主要测试端点存在性和Content-Type检查

        # 1. 测试错误的Content-Type
        response = anonymous_client.post(
            "/acps-atr-v2/ocsp",
            content=b"dummy_request",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 415

        # 2. 测试正确的Content-Type (但请求体无效)
        response = anonymous_client.post(
            "/acps-atr-v2/ocsp",
            content=b"dummy_request",
            headers={"Content-Type": "application/ocsp-request"},
        )
        # 因为请求体不是有效的ASN.1，预期返回400
        assert response.status_code == 400

    def test_ocsp_get_request(self, anonymous_client: TestClient) -> None:
        """测试GET方法查询OCSP"""
        dummy_req = base64.urlsafe_b64encode(b"dummy_request").decode()

        response = anonymous_client.get(f"/acps-atr-v2/ocsp/{dummy_req}")
        # 因为请求体不是有效的ASN.1，预期返回400
        assert response.status_code == 400
