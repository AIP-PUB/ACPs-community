"""证书管理服务层：封装管理员证书 CRUD，依赖 CertificateService 基类"""

from datetime import datetime, timedelta
from urllib.parse import unquote
from uuid import UUID

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import desc, func, literal_column
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.common import (
    Certificate,
    CertificateService,
    CertificateStatus,
    CertificateType,
    beijing_now,
)
from app.core.base_exception import AppError

from .exception import (
    CertificateNotFoundError,
    CertificateRetrievalFailedError,
    InvalidParentCertificateError,
)

logger = structlog.get_logger(__name__)


class CertificateManagementService(CertificateService):
    """证书管理扩展服务"""

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create_root_certificate(self, subject_name: str, validity_days: int = 3650) -> Certificate:
        """
        创建根证书

        Args:
            subject_name: 证书主体名称
            validity_days: 有效期天数，默认10年

        Returns:
            Certificate: 创建的根证书
        """
        cert_pem, _ = self.generate_certificate_pair(
            subject_name=subject_name,
            certificate_type=CertificateType.ROOT,
            validity_days=validity_days,
        )

        certificate_data = {
            "certificate_type": CertificateType.ROOT,
            "subject": subject_name,
            "issuer": subject_name,  # 根证书是自签名的
            "status": CertificateStatus.VALID,
            "certificate_pem": cert_pem,
            "public_key": self._extract_public_key_from_cert(cert_pem),
            "expires_at": self._calculate_expiry_date(validity_days),
        }

        return await self.create_certificate(certificate_data)

    async def create_intermediate_certificate(
        self, subject_name: str, parent_certificate_id: UUID, validity_days: int = 1825
    ) -> Certificate:
        """
        创建中间证书

        Args:
            subject_name: 证书主体名称
            parent_certificate_id: 父证书ID
            validity_days: 有效期天数，默认5年

        Returns:
            Certificate: 创建的中间证书
        """
        parent_certificate = await self.get_certificate_by_id(parent_certificate_id)
        if not parent_certificate or parent_certificate.status != CertificateStatus.VALID:
            raise InvalidParentCertificateError()

        cert_pem, _ = self.generate_certificate_pair(
            subject_name=subject_name,
            certificate_type=CertificateType.INTERMEDIATE,
            validity_days=validity_days,
            parent_certificate=parent_certificate,
        )

        certificate_data = {
            "certificate_type": CertificateType.INTERMEDIATE,
            "subject": subject_name,
            "issuer": parent_certificate.subject,
            "status": CertificateStatus.VALID,
            "certificate_pem": cert_pem,
            "public_key": self._extract_public_key_from_cert(cert_pem),
            "parent_certificate_id": parent_certificate_id,
            "expires_at": self._calculate_expiry_date(validity_days),
        }

        return await self.create_certificate(certificate_data)

    async def renew_certificate(self, certificate_id: UUID, validity_days: int | None = None) -> Certificate:
        """
        续期证书

        Args:
            certificate_id: 证书ID
            validity_days: 新的有效期天数，如果不指定则使用默认值

        Returns:
            Certificate: 新的证书
        """
        old_certificate = await self.get_certificate_by_id(certificate_id)
        if not old_certificate:
            raise CertificateNotFoundError()

        # 确定续期天数
        if validity_days is None:
            validity_days = self._get_default_validity_days(old_certificate.certificate_type)

        parent_certificate = await self._resolve_renewal_parent_certificate(old_certificate)

        # 生成新证书
        cert_pem, _ = self.generate_certificate_pair(
            subject_name=old_certificate.subject,
            certificate_type=old_certificate.certificate_type,
            validity_days=validity_days,
            parent_certificate=parent_certificate,
        )

        # 创建新证书
        certificate_data = self._build_renewed_certificate_data(old_certificate, cert_pem, validity_days)

        new_certificate = await self.create_certificate(certificate_data)

        # 吊销旧证书
        await self.revoke_certificate(certificate_id, "续期替换")

        return new_certificate

    async def _resolve_renewal_parent_certificate(self, old_certificate: Certificate) -> Certificate | None:
        """根据旧证书解析续期时需要的父证书"""
        if old_certificate.certificate_type == CertificateType.INTERMEDIATE:
            if old_certificate.parent_certificate_id is None:
                raise InvalidParentCertificateError("Intermediate certificate missing parent certificate.")

            parent_certificate = await self.get_certificate_by_id(old_certificate.parent_certificate_id)
            if parent_certificate is None or parent_certificate.status != CertificateStatus.VALID:
                raise InvalidParentCertificateError()
            return parent_certificate

        if old_certificate.parent_certificate_id is not None:
            parent_certificate = await self.get_certificate_by_id(old_certificate.parent_certificate_id)
            if parent_certificate is None or parent_certificate.status != CertificateStatus.VALID:
                raise InvalidParentCertificateError("Parent certificate is not valid for renewal.")
            return parent_certificate

        return None

    def _build_renewed_certificate_data(
        self,
        old_certificate: Certificate,
        cert_pem: str,
        validity_days: int,
    ) -> dict[str, CertificateType | CertificateStatus | str | UUID | None | datetime]:
        """构建续期证书写入数据"""
        return {
            "certificate_type": old_certificate.certificate_type,
            "subject": old_certificate.subject,
            "issuer": old_certificate.issuer,
            "status": CertificateStatus.VALID,
            "certificate_pem": cert_pem,
            "public_key": self._extract_public_key_from_cert(cert_pem),
            "parent_certificate_id": old_certificate.parent_certificate_id,
            "aic": old_certificate.aic,
            "expires_at": self._calculate_expiry_date(validity_days),
        }

    def _get_default_validity_days(self, certificate_type: CertificateType) -> int:
        """根据证书类型返回默认续期天数"""
        if certificate_type == CertificateType.ROOT:
            return 3650  # 10年
        if certificate_type == CertificateType.INTERMEDIATE:
            return 1825  # 5年
        return 365  # 1年

    async def get_certificate_chain(self, certificate_id: UUID) -> list[Certificate]:
        """
        获取证书链

        Args:
            certificate_id: 证书ID

        Returns:
            List[Certificate]: 证书链，从用户证书到根证书
        """
        chain: list[Certificate] = []
        visited_ids: set[UUID] = set()
        current_cert = await self.get_certificate_by_id(certificate_id)

        while current_cert:
            if current_cert.id in visited_ids:
                logger.warning("检测到证书链循环引用，提前终止遍历", certificate_id=str(current_cert.id))
                break

            visited_ids.add(current_cert.id)
            chain.append(current_cert)
            if current_cert.parent_certificate_id:
                current_cert = await self.get_certificate_by_id(current_cert.parent_certificate_id)
            else:
                break

        return chain

    async def get_certificate_or_error(self, certificate_id: UUID) -> Certificate:
        """获取证书，不存在时抛出模块异常"""
        certificate = await self.get_certificate_by_id(certificate_id)
        if certificate is None:
            raise CertificateNotFoundError()
        return certificate

    async def revoke_certificate(self, certificate_id: UUID, reason: str) -> Certificate:
        """吊销证书，不存在时抛出模块异常"""
        certificate = await super().revoke_certificate(certificate_id, reason)
        if certificate is None:
            raise CertificateNotFoundError()
        return certificate

    def _extract_public_key_from_cert(self, cert_pem: str) -> str:
        """
        从证书PEM中提取公钥

        Args:
            cert_pem: 证书PEM格式字符串

        Returns:
            str: 公钥PEM格式字符串
        """
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        public_key = cert.public_key()

        return public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    def _calculate_expiry_date(self, validity_days: int) -> datetime:
        """
        计算过期日期

        Args:
            validity_days: 有效期天数

        Returns:
            datetime: 过期日期
        """
        return beijing_now() + timedelta(days=validity_days)

    async def revoke_certificates_by_aic(self, aic: str, reason: str) -> int:
        """
        根据 AIC 批量吊销证书

        查找指定 AIC 的所有已签发有效证书并吊销。

        Args:
            aic: Agent Identity Code
            reason: 吊销原因

        Returns:
            int: 成功吊销的证书数量
        """
        # 只吊销已经签发且当前仍然有效的证书。
        # 旧环境中的 certificates.status 枚举不一定包含 PENDING，
        # 这里避免把未签发状态带入查询导致 revoke-notify 整体失败。
        statement = select(Certificate).where(
            Certificate.aic == aic,
            Certificate.status == CertificateStatus.VALID,
        )

        result = await self.session.execute(statement)
        certificates = list(result.scalars().all())

        revoked_count = 0
        for cert in certificates:
            try:
                # 调用父类的吊销方法
                if await self.revoke_certificate(cert.id, reason):
                    revoked_count += 1
            except AppError as e:
                # 记录错误但继续处理其他证书
                logger.warning("证书吊销失败，跳过继续处理", cert_id=str(cert.id), error=str(e))
                continue

        return revoked_count

    async def retrieve_certificate_by_aic_and_version(self, aic: str, version: int | None) -> Certificate:
        """
        根据 AIC 和版本号检索证书

        Args:
            aic: Agent Identity Code
            version: 版本号，如果为 None 则检索最新有效证书

        Returns:
            Certificate: 证书对象

        Raises:
            CertificateNotFoundError: 如果未找到符合条件的证书
        """
        try:
            statement = select(Certificate).where(
                Certificate.aic == aic,
            )

            if version is not None:
                statement = statement.where(Certificate.version == version)
            else:
                statement = statement.where(Certificate.status == CertificateStatus.VALID)

            statement = statement.order_by(desc(literal_column("created_at")))

            result = await self.session.execute(statement)
            certificate = result.scalar_one_or_none()

            if not certificate:
                raise CertificateNotFoundError("Certificate not found for the given AIC and version.")

            return certificate
        except AppError:
            raise
        except SQLAlchemyError as e:
            logger.exception("按 AIC 和版本检索证书失败", aic=aic, version=version)
            raise CertificateRetrievalFailedError("Failed to retrieve certificate by AIC and version.") from e

    async def retrieve_certificate_by_cert(self, cert_pem: str) -> Certificate:
        """
        从证书PEM中提取AIC

        Args:
            cert_pem: 证书PEM格式字符串

        Returns:
            Certificate: 证书对象
        """
        try:
            # cert_pem 可能来自 URL query（出现 %0A、%20 等），先做一次 URL 解码。
            # 注意：不要用 unquote_plus，避免把 PEM/base64 里的 '+' 误当空格。
            cert_pem = unquote(cert_pem or "")

            # 1) 快路径：精确匹配（最快）
            statement = select(Certificate).where(Certificate.certificate_pem == cert_pem)
            result = await self.session.execute(statement)
            certificate = result.scalar_one_or_none()
            if certificate:
                return certificate

            # 2) 兼容：入参可能把换行替换成空格/混用 \r\n，忽略所有空白后再匹配。
            normalized_input = "".join(cert_pem.split())
            statement = select(Certificate).where(
                func.regexp_replace(
                    Certificate.certificate_pem,
                    r"\\s+",
                    "",
                    "g",
                )
                == normalized_input
            )
            result = await self.session.execute(statement)
            certificate = result.scalar_one_or_none()
            if certificate:
                return certificate

            # 3) 回退：在不支持 regexp_replace 的数据库上（例如 SQLite），用 Python 做归一化匹配。
            # 数据量很大时不建议使用该路径。
            result = await self.session.execute(select(Certificate))
            candidates = result.scalars().all()
            for item in candidates:
                if "".join((item.certificate_pem or "").split()) == normalized_input:
                    return item

            raise CertificateNotFoundError("Certificate not found for the given certificate content.")
        except AppError:
            raise
        except SQLAlchemyError as e:
            logger.exception("通过证书 PEM 检索证书失败")
            raise CertificateRetrievalFailedError() from e

    async def get_current_valid_certificate_by_aic(self, aic: str) -> Certificate:
        """按 AIC 获取版本号最高的当前有效证书，用于公钥发现。

        Args:
            aic: Agent Identity Code

        Returns:
            Certificate: 最新有效证书对象

        Raises:
            CertificateNotFoundError: 该 AIC 无有效证书
            CertificateRetrievalFailedError: 数据库查询异常
        """
        try:
            statement = (
                select(Certificate)
                .where(
                    Certificate.aic == aic,
                    Certificate.status == CertificateStatus.VALID,
                )
                .order_by(desc(literal_column("version")))
                .limit(1)
            )
            result = await self.session.execute(statement)
            certificate = result.scalar_one_or_none()
            if certificate is None:
                raise CertificateNotFoundError(f"No valid certificate found for AIC {aic!r}.")
            return certificate
        except AppError:
            raise
        except SQLAlchemyError as e:
            logger.exception("按 AIC 查询当前有效证书失败", aic=aic)
            raise CertificateRetrievalFailedError("Failed to retrieve current valid certificate by AIC.") from e
