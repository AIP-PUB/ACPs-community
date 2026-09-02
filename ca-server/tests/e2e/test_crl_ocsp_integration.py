"""CRL 和 OCSP 黑盒端到端测试。"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlmodel import Session, select

from app.common import Certificate, CertificateStatus, OCSPResponder, RevocationReason
from app.core.db_session import engine

pytestmark = [pytest.mark.crl_ocsp, pytest.mark.e2e]


@pytest.fixture
def db_session() -> Generator[Session]:
    """提供用于准备和校验数据的数据库会话。"""

    with Session(engine) as session:
        yield session


@pytest.fixture
def test_certificates(db_session: Session) -> list[Certificate]:
    """创建测试用证书集合。"""

    certificates: list[Certificate] = []

    for i in range(3):
        cert = Certificate(
            certificate_type="user",
            serial_number=f"VALID{i:03d}{uuid4().hex[:8].upper()}",
            subject=f"CN=test{i}.example.com,O=Test Org,C=CN",
            issuer="CN=Test CA,O=Test CA,C=CN",
            status=CertificateStatus.VALID,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=365),
            certificate_pem=f"-----BEGIN CERTIFICATE-----\nTEST_CERT_{i}\n-----END CERTIFICATE-----",
            public_key=f"TEST_PUBLIC_KEY_{i}",
        )
        db_session.add(cert)
        certificates.append(cert)

    revoked_cert = Certificate(
        certificate_type="user",
        serial_number=f"REVOKED{uuid4().hex[:8].upper()}",
        subject="CN=revoked.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.REVOKED,
        issued_at=datetime.now(UTC) - timedelta(days=30),
        expires_at=datetime.now(UTC) + timedelta(days=335),
        revoked_at=datetime.now(UTC) - timedelta(days=1),
        revocation_reason=RevocationReason.KEY_COMPROMISE,
        certificate_pem="-----BEGIN CERTIFICATE-----\nREVOKED_CERT\n-----END CERTIFICATE-----",
        public_key="REVOKED_PUBLIC_KEY",
    )
    db_session.add(revoked_cert)
    certificates.append(revoked_cert)

    expired_cert = Certificate(
        certificate_type="user",
        serial_number=f"EXPIRED{uuid4().hex[:8].upper()}",
        subject="CN=expired.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.EXPIRED,
        issued_at=datetime.now(UTC) - timedelta(days=400),
        expires_at=datetime.now(UTC) - timedelta(days=35),
        certificate_pem="-----BEGIN CERTIFICATE-----\nEXPIRED_CERT\n-----END CERTIFICATE-----",
        public_key="EXPIRED_PUBLIC_KEY",
    )
    db_session.add(expired_cert)
    certificates.append(expired_cert)

    db_session.commit()
    for cert in certificates:
        db_session.refresh(cert)

    return certificates


@pytest.fixture
def ocsp_responder(db_session: Session) -> OCSPResponder:
    """创建测试用 OCSP 响应器。"""

    responder = OCSPResponder(
        name="Test OCSP Responder",
        certificate_pem="-----BEGIN CERTIFICATE-----\nOCSP_RESPONDER_CERT\n-----END CERTIFICATE-----",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nOCSP_RESPONDER_KEY\n-----END PRIVATE KEY-----",
        certificate_serial="OCSP123456789",
        endpoints=["http://ocsp.test.com"],
        supported_extensions=["basic", "nonce"],
        is_active=True,
    )
    db_session.add(responder)
    db_session.commit()
    db_session.refresh(responder)
    return responder


def _get_valid_certificate(certificates: list[Certificate]) -> Certificate:
    """返回第一个有效证书。"""

    for cert in certificates:
        if cert.status == CertificateStatus.VALID:
            return cert
    raise AssertionError("需要至少一个有效证书进行测试")


class TestCRLOCSPBasicIntegration:
    """测试 CRL 和 OCSP 基本集成功能。"""

    async def test_initial_state_consistency(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
        ocsp_responder: OCSPResponder,
    ) -> None:
        """测试初始状态下 CRL 和 OCSP 的一致性。"""

        _ = ocsp_responder

        crl_response = await client.post("/acps-atr-v2/crl/refresh")
        assert crl_response.status_code == 200

        crl_detail_response = await client.get("/acps-atr-v2/crl/detail")
        assert crl_detail_response.status_code == 200
        crl_data = crl_detail_response.json()
        crl_revoked_serials = {cert["serialNumber"] for cert in crl_data["revokedCertificates"]}

        for cert in test_certificates:
            ocsp_response = await client.get(f"/acps-atr-v2/ocsp/certificate/{cert.serial_number}")
            assert ocsp_response.status_code == 200
            ocsp_data = ocsp_response.json()

            if cert.status == CertificateStatus.REVOKED:
                assert cert.serial_number in crl_revoked_serials
                assert ocsp_data["certificateStatus"] == "revoked"
            elif cert.status == CertificateStatus.VALID:
                assert cert.serial_number not in crl_revoked_serials
                assert ocsp_data["certificateStatus"] == "good"
            elif cert.status == CertificateStatus.EXPIRED:
                assert ocsp_data["certificateStatus"] == "expired"

    async def test_certificate_lifecycle_integration(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试证书生命周期的完整集成。"""

        valid_cert = _get_valid_certificate(test_certificates)

        ocsp_response = await client.get(f"/acps-atr-v2/ocsp/certificate/{valid_cert.serial_number}")
        assert ocsp_response.status_code == 200
        assert ocsp_response.json()["certificateStatus"] == "good"

        revoke_response = await client.post(
            f"/admin/certificates/{valid_cert.id}/revoke",
            params={"reason": "keyCompromise"},
        )
        assert revoke_response.status_code == 200

        ocsp_response = await client.get(f"/acps-atr-v2/ocsp/certificate/{valid_cert.serial_number}")
        assert ocsp_response.status_code == 200
        ocsp_data = ocsp_response.json()
        assert ocsp_data["certificateStatus"] == "revoked"
        assert ocsp_data["revocationReason"] == "keyCompromise"

        crl_refresh_response = await client.post("/acps-atr-v2/crl/refresh")
        assert crl_refresh_response.status_code == 200

        crl_detail_response = await client.get("/acps-atr-v2/crl/detail")
        assert crl_detail_response.status_code == 200
        crl_data = crl_detail_response.json()

        revoked_serials = [cert["serialNumber"] for cert in crl_data["revokedCertificates"]]
        assert valid_cert.serial_number in revoked_serials

        for revoked_cert in crl_data["revokedCertificates"]:
            if revoked_cert["serialNumber"] == valid_cert.serial_number:
                assert revoked_cert["reason"] == "keyCompromise"
                break
        else:
            pytest.fail(f"Certificate {valid_cert.serial_number} not found in CRL")

    async def test_batch_ocsp_consistency(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试批量 OCSP 查询的一致性。"""

        certificate_requests = [
            {
                "serial_number": cert.serial_number,
                "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
            }
            for cert in test_certificates
        ]
        certificate_requests.append(
            {
                "serial_number": "NONEXISTENT123456",
                "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
            }
        )

        batch_response = await client.post("/acps-atr-v2/ocsp/batch", json={"certificates": certificate_requests})
        assert batch_response.status_code == 200

        batch_data = batch_response.json()
        assert "responses" in batch_data
        assert len(batch_data["responses"]) == len(certificate_requests)

        for cert in test_certificates:
            single_response = await client.get(f"/acps-atr-v2/ocsp/certificate/{cert.serial_number}")
            single_data = single_response.json()

            batch_cert_response = next(
                (resp for resp in batch_data["responses"] if resp["serial_number"] == cert.serial_number),
                None,
            )

            assert batch_cert_response is not None
            assert batch_cert_response["status"] == single_data["certificateStatus"]


class TestCRLOCSPErrorHandling:
    """测试 CRL 和 OCSP 错误处理。"""

    async def test_crl_generation_without_ca(self, client: AsyncClient) -> None:
        """测试在没有 CA 证书时的 CRL 生成。"""

        response = await client.post("/acps-atr-v2/crl/refresh")
        assert response.status_code in [200, 500]

    async def test_ocsp_response_without_responder(self, client: AsyncClient, db_session: Session) -> None:
        """测试在没有 OCSP 响应器时会自动 bootstrap 默认 responder。"""

        for responder in db_session.exec(select(OCSPResponder)).all():
            db_session.delete(responder)
        db_session.commit()

        response = await client.get("/acps-atr-v2/ocsp/responder/info")
        assert response.status_code == 200
        data = response.json()
        assert data["responder"]["name"] == "Agent CA OCSP Responder"
        assert "key_hash" in data["responder"]

        response = await client.get("/acps-atr-v2/ocsp/certificate/TEST123")
        assert response.status_code == 200

    async def test_invalid_certificate_id_revocation(self, client: AsyncClient) -> None:
        """测试使用无效证书 ID 进行吊销。"""

        invalid_id = "00000000-0000-0000-0000-000000000000"
        response = await client.post(
            f"/admin/certificates/{invalid_id}/revoke",
            params={"reason": "keyCompromise"},
        )
        assert response.status_code == 404

    async def test_malformed_ocsp_requests(self, client: AsyncClient) -> None:
        """测试格式错误的 OCSP 请求。"""

        malformed_requests = [
            {},
            {"certificates": "not_a_list"},
            {"certificates": [{"wrong_field": "value"}]},
        ]

        for request_data in malformed_requests:
            response = await client.post("/acps-atr-v2/ocsp/batch", json=request_data)
            assert response.status_code in [400, 422]


class TestCRLOCSPPerformance:
    """测试 CRL 和 OCSP 性能。"""

    async def test_crl_generation_performance(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试 CRL 生成性能。"""

        _ = test_certificates

        start_time = perf_counter()
        response = await client.post("/acps-atr-v2/crl/refresh")
        generation_time = perf_counter() - start_time

        assert response.status_code == 200
        assert generation_time < 5.0, f"CRL generation took {generation_time}s, which is too slow"

    async def test_ocsp_response_performance(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试 OCSP 响应性能。"""

        cert = test_certificates[0]
        start_time = perf_counter()
        response = await client.get(f"/acps-atr-v2/ocsp/certificate/{cert.serial_number}")
        response_time = perf_counter() - start_time

        assert response.status_code == 200
        assert response_time < 1.0, f"OCSP response took {response_time}s, which is too slow"

    async def test_batch_ocsp_performance(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试批量 OCSP 查询性能。"""

        certificate_requests = [
            {
                "serial_number": cert.serial_number,
                "issuer_key_hash": "d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
            }
            for cert in test_certificates
        ]

        start_time = perf_counter()
        response = await client.post("/acps-atr-v2/ocsp/batch", json={"certificates": certificate_requests})
        batch_time = perf_counter() - start_time

        assert response.status_code == 200
        assert batch_time < 2.0, f"Batch OCSP took {batch_time}s, which is too slow"


class TestCRLOCSPDataConsistency:
    """测试 CRL 和 OCSP 数据一致性。"""

    async def test_multiple_revocations_consistency(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
    ) -> None:
        """测试多次吊销操作的数据一致性。"""

        valid_certs = [cert for cert in test_certificates if cert.status == CertificateStatus.VALID]
        if len(valid_certs) < 2:
            pytest.skip("需要至少2个有效证书进行此测试")

        refresh_response = await client.post("/acps-atr-v2/crl/refresh")
        assert refresh_response.status_code == 200

        initial_crl_response = await client.get("/acps-atr-v2/crl/detail")
        assert initial_crl_response.status_code == 200
        initial_crl_data = initial_crl_response.json()
        initial_revoked_count = initial_crl_data["revokedCertificatesCount"]

        revoked_serials: list[str] = []
        for cert in valid_certs[:2]:
            revoke_response = await client.post(
                f"/admin/certificates/{cert.id}/revoke",
                params={"reason": "keyCompromise"},
            )
            assert revoke_response.status_code == 200
            revoked_serials.append(cert.serial_number)

        crl_refresh_response = await client.post("/acps-atr-v2/crl/refresh")
        assert crl_refresh_response.status_code == 200

        final_crl_response = await client.get("/acps-atr-v2/crl/detail")
        final_crl_data = final_crl_response.json()
        assert final_crl_data["revokedCertificatesCount"] == initial_revoked_count + 2

        final_revoked_serials = [cert["serialNumber"] for cert in final_crl_data["revokedCertificates"]]
        for serial in revoked_serials:
            assert serial in final_revoked_serials

        for serial in revoked_serials:
            ocsp_response = await client.get(f"/acps-atr-v2/ocsp/certificate/{serial}")
            assert ocsp_response.status_code == 200
            assert ocsp_response.json()["certificateStatus"] == "revoked"

    async def test_crl_version_progression(self, client: AsyncClient) -> None:
        """测试 CRL 版本的正确递进。"""

        crl_info_response = await client.get("/acps-atr-v2/crl/info")
        initial_version = crl_info_response.json()["version"] if crl_info_response.status_code == 200 else None

        refresh_response = await client.post("/acps-atr-v2/crl/refresh")
        assert refresh_response.status_code == 200
        new_version = refresh_response.json()["version"]

        if initial_version:
            assert new_version > initial_version

        refresh_response2 = await client.post("/acps-atr-v2/crl/refresh")
        assert refresh_response2.status_code == 200
        newer_version = refresh_response2.json()["version"]
        assert newer_version > new_version

    async def test_database_transaction_consistency(
        self,
        client: AsyncClient,
        test_certificates: list[Certificate],
        db_session: Session,
    ) -> None:
        """测试数据库事务的一致性。"""

        valid_cert = _get_valid_certificate(test_certificates)

        initial_revoked_count = db_session.exec(
            select(Certificate).where(Certificate.status == CertificateStatus.REVOKED)
        ).all()
        initial_count = len(initial_revoked_count)

        revoke_response = await client.post(
            f"/admin/certificates/{valid_cert.id}/revoke",
            params={"reason": "keyCompromise"},
        )
        assert revoke_response.status_code == 200

        db_session.refresh(valid_cert)
        assert valid_cert.status == CertificateStatus.REVOKED
        assert valid_cert.revocation_reason == RevocationReason.KEY_COMPROMISE
        assert valid_cert.revoked_at is not None

        final_revoked_count = db_session.exec(
            select(Certificate).where(Certificate.status == CertificateStatus.REVOKED)
        ).all()
        assert len(final_revoked_count) == initial_count + 1
