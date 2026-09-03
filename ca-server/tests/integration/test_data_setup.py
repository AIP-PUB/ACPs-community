"""
测试数据设置工具

为CRL和OCSP测试创建标准的测试数据
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Session

from app.common import (
    Certificate,
    CertificateStatus,
    CertificateType,
    CRLService,
    OCSPResponder,
    RevocationReason,
    beijing_now,
)


def create_test_certificates(db_session: Session, count: int = 5):
    """创建测试证书"""
    certificates = []

    for i in range(count):
        # 生成符合标准的16进制序列号
        serial_hex = f"{i + 1:04X}{uuid4().hex[:12].upper()}"

        cert = Certificate(
            certificate_type=CertificateType.USER,
            serial_number=serial_hex,
            subject=f"CN=test{i}.example.com,O=Test Org,C=CN",
            issuer="CN=Test CA,O=Test CA,C=CN",
            status=CertificateStatus.VALID,
            issued_at=beijing_now(),
            expires_at=beijing_now() + timedelta(days=365),
            certificate_pem=f"-----BEGIN CERTIFICATE-----\nTEST_CERT_{i}\n-----END CERTIFICATE-----",
            public_key=f"TEST_PUBLIC_KEY_{i}",
        )
        db_session.add(cert)
        certificates.append(cert)

    db_session.commit()
    for cert in certificates:
        db_session.refresh(cert)

    return certificates


def create_revoked_certificate(db_session: Session, reason: RevocationReason = RevocationReason.KEY_COMPROMISE):
    """创建已吊销证书"""
    # 生成符合标准的16进制序列号
    serial_hex = f"{9999:04X}{uuid4().hex[:12].upper()}"

    cert = Certificate(
        certificate_type=CertificateType.USER,
        serial_number=serial_hex,
        subject="CN=revoked.example.com,O=Test Org,C=CN",
        issuer="CN=Test CA,O=Test CA,C=CN",
        status=CertificateStatus.REVOKED,
        issued_at=beijing_now() - timedelta(days=30),
        expires_at=beijing_now() + timedelta(days=335),
        revoked_at=beijing_now() - timedelta(days=1),
        revocation_reason=reason,
        certificate_pem="-----BEGIN CERTIFICATE-----\nREVOKED_CERT\n-----END CERTIFICATE-----",
        public_key="REVOKED_PUBLIC_KEY",
    )
    db_session.add(cert)
    db_session.commit()
    db_session.refresh(cert)
    return cert


def create_expired_certificate(db_session: Session):
    """创建已过期证书"""
    # 生成符合标准的16进制序列号
    serial_hex = f"{8888:04X}{uuid4().hex[:12].upper()}"

    cert = Certificate(
        certificate_type=CertificateType.USER,
        serial_number=serial_hex,
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


def create_ocsp_responder(db_session: Session):
    """创建OCSP响应器"""
    responder = OCSPResponder(
        responder_url="http://ocsp.test.com",
        responder_cert_pem="-----BEGIN CERTIFICATE-----\nOCSP_RESPONDER_CERT\n-----END CERTIFICATE-----",
        responder_key_hash="d042ee4e30dcd77e3a2f8eb3f5d8fe8673567864",
        is_active=True,
    )
    db_session.add(responder)
    db_session.commit()
    db_session.refresh(responder)
    return responder


async def setup_crl_test_environment(db_session: Session, async_db_session: AsyncSession):
    """设置CRL测试环境"""
    # 创建测试证书
    valid_certs = create_test_certificates(db_session, count=3)
    revoked_cert = create_revoked_certificate(db_session)
    expired_cert = create_expired_certificate(db_session)

    # 生成初始CRL
    crl_service = CRLService(async_db_session)
    initial_crl = await crl_service.generate_new_crl(issuer="CN=Test CA,O=Test CA,C=CN", next_update_hours=24)

    return {
        "valid_certificates": valid_certs,
        "revoked_certificate": revoked_cert,
        "expired_certificate": expired_cert,
        "initial_crl": initial_crl,
    }


def setup_ocsp_test_environment(db_session: Session):
    """设置OCSP测试环境"""
    # 创建OCSP响应器
    responder = create_ocsp_responder(db_session)

    # 创建测试证书
    valid_certs = create_test_certificates(db_session, count=2)
    revoked_cert = create_revoked_certificate(db_session)
    expired_cert = create_expired_certificate(db_session)

    return {
        "responder": responder,
        "valid_certificates": valid_certs,
        "revoked_certificate": revoked_cert,
        "expired_certificate": expired_cert,
    }


def cleanup_test_data(db_session: Session):
    """清理测试数据"""
    from sqlmodel import select, text

    from app.common import (
        CRL,
        OCSPRequest,
        OCSPResponder,
        OCSPResponse,
        RevokedCertificateEntry,
    )

    try:
        # 1. 先删除关联的CRL条目
        revoked_entries = db_session.exec(select(RevokedCertificateEntry)).all()
        for entry in revoked_entries:
            db_session.delete(entry)

        # 2. 删除所有CRL记录（为了确保测试隔离）
        crls = db_session.exec(select(CRL)).all()
        for crl in crls:
            db_session.delete(crl)

        # 3. 删除测试证书 - 使用原始SQL查询避免枚举值问题
        # 直接使用SQL删除所有测试相关的证书，避免枚举映射问题
        result = db_session.connection().execute(
            text(
                """
            DELETE FROM certificates
            WHERE
                -- 检查序列号模式：以0001-9999开头的16进制数字
                (LENGTH(serial_number) >= 4 AND
                 UPPER(SUBSTR(serial_number, 1, 4)) ~ '^[0-9A-F]{4}$')
                OR
                -- 检查序列号前缀：TEST, REVOKED, EXPIRED
                (serial_number LIKE 'TEST%' OR
                 serial_number LIKE 'REVOKED%' OR
                 serial_number LIKE 'EXPIRED%')
                OR
                -- 检查Subject字段包含测试关键词
                (subject LIKE '%Test%' OR
                 subject LIKE '%test%' OR
                 subject LIKE '%example.com%')
        """
            )
        )

        print(f"删除了 {result.rowcount} 个测试证书")

        # 4. 删除所有OCSP响应记录（先删响应再删请求，遵守外键约束）
        ocsp_responses = db_session.exec(select(OCSPResponse)).all()
        for resp in ocsp_responses:
            db_session.delete(resp)

        # 5. 删除所有OCSP请求记录
        ocsp_requests = db_session.exec(select(OCSPRequest)).all()
        for req in ocsp_requests:
            db_session.delete(req)

        # 6. 删除所有 OCSP 响应器。
        # 默认 responder 现在由应用启动和运行时幂等 bootstrap，因此测试环境需要在每个
        # 用例前后把该表清空，避免 module-scope TestClient 与函数级 fixture 叠加出多个 active responder。
        responders = db_session.exec(select(OCSPResponder)).all()
        for responder in responders:
            db_session.delete(responder)

        # 提交所有更改
        db_session.commit()

    except Exception as e:
        # 如果出错，回滚事务
        db_session.rollback()
        print(f"清理测试数据时出错: {e}")
        raise


def revoke_certificate_for_testing(
    db_session: Session,
    certificate: Certificate,
    reason: RevocationReason = RevocationReason.KEY_COMPROMISE,
):
    """为测试目的吊销证书"""
    certificate.status = CertificateStatus.REVOKED
    certificate.revoked_at = beijing_now()
    certificate.revocation_reason = reason

    db_session.add(certificate)
    db_session.commit()
    db_session.refresh(certificate)

    return certificate
