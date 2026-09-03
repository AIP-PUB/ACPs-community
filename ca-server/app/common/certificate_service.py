"""证书公共服务层：证书签发、查询、吊销的底层实现，供上层模块复用"""

import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import and_, func, select

from ..core.crypto_utils import PrivateKeyTypes, x509_signature_algorithm_for
from .certificate_model import Certificate, CertificateStatus, CertificateType, RevocationReason
from .certificate_version import get_next_certificate_version
from .time_utils import beijing_now


class CertificateService:
    """证书管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_certificate(self, certificate_data: dict[str, Any]) -> Certificate:
        """
        创建新证书

        Args:
            certificate_data: 证书数据字典

        Returns:
            Certificate: 创建的证书对象
        """
        # 若是带 AIC 的证书且未显式指定 version，则按 AIC 自动递增版本号
        aic = certificate_data.get("aic")
        if aic and certificate_data.get("version") is None:
            certificate_data["version"] = await get_next_certificate_version(self.session, aic)

        # 生成序列号
        serial_number = self._generate_serial_number()

        certificate = Certificate(serial_number=serial_number, **certificate_data)

        self.session.add(certificate)
        await self.session.flush()
        await self.session.refresh(certificate)

        return certificate

    async def get_certificate_by_id(self, certificate_id: UUID) -> Certificate | None:
        """
        根据ID获取证书

        Args:
            certificate_id: 证书ID

        Returns:
            Optional[Certificate]: 证书对象或None
        """
        return await self.session.get(Certificate, certificate_id)

    async def get_certificate_by_serial(self, serial_number: str) -> Certificate | None:
        """
        根据序列号获取证书

        Args:
            serial_number: 证书序列号

        Returns:
            Optional[Certificate]: 证书对象或None
        """
        statement = select(Certificate).where(Certificate.serial_number == serial_number)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_certificates(
        self,
        page: int = 1,
        page_size: int = 20,
        certificate_type: CertificateType | None = None,
        status: CertificateStatus | None = None,
        aic: str | None = None,
    ) -> tuple[list[Certificate], int]:
        """
        获取证书列表

        Args:
            page: 页码
            page_size: 每页数量
            certificate_type: 证书类型过滤
            status: 状态过滤
            aic: AIC过滤

        Returns:
            Tuple[List[Certificate], int]: 证书列表和总数
        """
        statement = select(Certificate)

        # 添加过滤条件
        conditions = []
        if certificate_type:
            conditions.append(Certificate.certificate_type == certificate_type)
        if status:
            conditions.append(Certificate.status == status)
        if aic:
            conditions.append(Certificate.aic == aic)

        if conditions:
            statement = statement.where(and_(*conditions))

        # 计算总数
        count_statement = select(func.count()).select_from(Certificate)
        if conditions:
            count_statement = count_statement.where(and_(*conditions))
        count_result = await self.session.execute(count_statement)
        total = int(count_result.scalar_one())

        # 分页查询
        statement = statement.offset((page - 1) * page_size).limit(page_size)
        result = await self.session.execute(statement)
        certificates = list(result.scalars().all())

        return certificates, total

    async def update_certificate_status(
        self,
        certificate_id: UUID,
        status: CertificateStatus,
        revocation_reason: str | RevocationReason | None = None,
    ) -> Certificate | None:
        """
        更新证书状态

        Args:
            certificate_id: 证书ID
            status: 新状态
            revocation_reason: 吊销原因（如果是吊销操作）

        Returns:
            Optional[Certificate]: 更新后的证书对象或None
        """
        certificate = await self.get_certificate_by_id(certificate_id)
        if not certificate:
            return None

        certificate.status = status
        certificate.updated_at = beijing_now()

        if status == CertificateStatus.REVOKED:
            certificate.revoked_at = beijing_now()
            certificate.revocation_reason = self._normalize_revocation_reason(revocation_reason)

        await self.session.flush()
        await self.session.refresh(certificate)

        return certificate

    async def revoke_certificate(self, certificate_id: UUID, reason: str) -> Certificate | None:
        """
        吊销证书

        Args:
            certificate_id: 证书ID
            reason: 吊销原因

        Returns:
            Optional[Certificate]: 吊销后的证书对象或None
        """
        return await self.update_certificate_status(certificate_id, CertificateStatus.REVOKED, reason)

    def _normalize_revocation_reason(self, reason: str | RevocationReason | None) -> RevocationReason | None:
        """将传入的吊销原因归一化为模型枚举"""
        if reason is None:
            return None
        if isinstance(reason, RevocationReason):
            return reason
        if reason == "续期替换":
            return RevocationReason.SUPERSEDED
        try:
            return RevocationReason(reason)
        except ValueError:
            return RevocationReason.UNSPECIFIED

    async def get_expiring_certificates(self, days_ahead: int = 30) -> list[Certificate]:
        """
        获取即将过期的证书

        Args:
            days_ahead: 提前多少天

        Returns:
            List[Certificate]: 即将过期的证书列表
        """
        expiry_threshold = beijing_now() + timedelta(days=days_ahead)
        statement = select(Certificate).where(
            and_(
                Certificate.status == CertificateStatus.VALID,
                Certificate.expires_at <= expiry_threshold,
            )
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def get_root_certificates(self) -> list[Certificate]:
        """
        获取所有根证书

        Returns:
            List[Certificate]: 根证书列表
        """
        statement = select(Certificate).where(Certificate.certificate_type == CertificateType.ROOT)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def get_intermediate_certificates(self, parent_id: UUID | None = None) -> list[Certificate]:
        """
        获取中间证书

        Args:
            parent_id: 父证书ID，如果指定则只返回该父证书的子证书

        Returns:
            List[Certificate]: 中间证书列表
        """
        statement = select(Certificate).where(Certificate.certificate_type == CertificateType.INTERMEDIATE)

        if parent_id:
            statement = statement.where(Certificate.parent_certificate_id == parent_id)

        result = await self.session.execute(statement)
        return list(result.scalars().all())

    def _generate_serial_number(self) -> str:
        """
        生成证书序列号

        Returns:
            str: 16位十六进制序列号
        """
        return secrets.token_hex(8).upper()

    def generate_certificate_pair(
        self,
        subject_name: str,
        certificate_type: CertificateType,
        validity_days: int = 365,
        parent_certificate: Certificate | None = None,
        key_type: str = "ed25519",
    ) -> tuple[str, str]:
        """
        生成证书和私钥对

        Args:
            subject_name: 证书主体名称
            certificate_type: 证书类型
            validity_days: 有效期天数
            parent_certificate: 父证书（用于签名）

        Returns:
            Tuple[str, str]: (证书PEM, 私钥PEM)
        """
        # 生成私钥
        private_key: PrivateKeyTypes
        if key_type == "rsa":
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
        elif key_type == "ec":
            private_key = ec.generate_private_key(ec.SECP256R1())
        else:
            private_key = ed25519.Ed25519PrivateKey.generate()

        # 创建证书主体
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, subject_name),
            ]
        )

        # 如果有父证书，使用父证书作为签发者，否则自签名
        if parent_certificate:
            issuer_name = parent_certificate.subject
            # 这里应该加载父证书的私钥进行签名，暂时使用自签名
            signing_key = private_key
        else:
            issuer_name = subject_name
            signing_key = private_key

        issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, issuer_name),
            ]
        )

        # 创建证书
        cert_builder = x509.CertificateBuilder()
        cert_builder = cert_builder.subject_name(subject)
        cert_builder = cert_builder.issuer_name(issuer)
        cert_builder = cert_builder.public_key(private_key.public_key())
        cert_builder = cert_builder.serial_number(int(self._generate_serial_number(), 16))
        cert_builder = cert_builder.not_valid_before(beijing_now())
        cert_builder = cert_builder.not_valid_after(beijing_now() + timedelta(days=validity_days))

        # 添加扩展
        if certificate_type == CertificateType.ROOT:
            cert_builder = cert_builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
        elif certificate_type == CertificateType.INTERMEDIATE:
            cert_builder = cert_builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=0),
                critical=True,
            )

        # 签名证书
        certificate = cert_builder.sign(signing_key, x509_signature_algorithm_for(signing_key))

        # 转换为PEM格式
        cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        return cert_pem, private_key_pem
