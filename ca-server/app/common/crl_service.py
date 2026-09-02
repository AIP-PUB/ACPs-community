"""CRL（证书吊销列表）业务服务：生成、更新、分发 CRL 的核心逻辑"""

import hashlib
from collections.abc import Sequence
from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    Encoding,
)
from cryptography.x509 import ReasonFlags
from sqlalchemy import literal_column
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import and_, desc, func, select

from ..core.ca_manager import get_ca_manager
from ..core.config import get_settings
from ..core.crypto_utils import signature_algorithm_name, x509_signature_algorithm_for
from .certificate_model import Certificate, CertificateStatus
from .crl_model import CRL, CRLStatus, RevokedCertificateEntry
from .time_utils import beijing_now


def get_default_crl_distribution_point() -> str:
    """获取默认 CRL 分发点 URL"""
    return get_settings().crl_distribution_point_url


class CRLService:
    """CRL管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    @property
    def db(self) -> AsyncSession:
        """兼容旧调用方对 db 属性的访问"""
        return self.session

    async def expire_old_crls_except(self, exclude_id: str) -> None:
        """将旧的CRL标记为已过期（除了指定ID的CRL）"""
        # 将所有当前状态的CRL标记为已过期，除了新创建的CRL
        statement = select(CRL).where(and_(CRL.status == CRLStatus.CURRENT, CRL.id != exclude_id))
        result = await self.session.execute(statement)
        old_crls = result.scalars().all()

        for crl in old_crls:
            crl.status = CRLStatus.EXPIRED
            self.session.add(crl)

        await self.session.flush()

    async def expire_old_crls(self) -> None:
        """将旧的CRL标记为已过期"""
        # 将所有当前状态的CRL标记为已过期
        statement = select(CRL).where(CRL.status == CRLStatus.CURRENT)
        result = await self.session.execute(statement)
        old_crls = result.scalars().all()

        for crl in old_crls:
            crl.status = CRLStatus.EXPIRED
            self.session.add(crl)

        await self.session.flush()

    async def get_current_crl(self) -> CRL | None:
        """获取当前有效的CRL"""
        statement = select(CRL).where(CRL.status == CRLStatus.CURRENT).order_by(desc(CRL.this_update)).limit(1)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_crl_by_version(self, version: str) -> CRL | None:
        """根据版本号获取CRL"""
        statement = select(CRL).where(CRL.version == version)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_crl_by_number(self, crl_number: int) -> CRL | None:
        """根据CRL编号获取CRL"""
        statement = select(CRL).where(CRL.crl_number == crl_number)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_crl_list(
        self,
        status: CRLStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[CRL], int]:
        """获取CRL列表"""
        statement = select(CRL)

        if status:
            statement = statement.where(CRL.status == status)

        # 获取总数
        total_statement = select(func.count()).select_from(CRL)
        if status:
            total_statement = total_statement.where(CRL.status == status)
        total_result = await self.session.execute(total_statement)
        total = int(total_result.scalar_one())

        # 分页查询
        statement = statement.order_by(desc(CRL.this_update))
        statement = statement.offset((page - 1) * page_size).limit(page_size)

        result = await self.session.execute(statement)
        crls = list(result.scalars().all())
        return crls, total

    async def generate_new_crl(
        self,
        issuer: str,
        next_update_hours: int = 24,
        distribution_points: list[str] | None = None,
    ) -> CRL:
        """生成新的CRL"""
        # 获取CA管理器
        ca_manager = get_ca_manager()
        ca_cert = ca_manager.ca_cert
        ca_private_key = ca_manager.ca_private_key
        if ca_cert is None or ca_private_key is None:
            raise RuntimeError("CA certificate or private key is not initialized")

        # 生成版本号（YYYYMMDDHHMMSS + 毫秒格式以确保唯一性）
        now = beijing_now()
        version = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"

        # 获取下一个CRL编号
        result = await self.session.execute(select(CRL).order_by(desc(CRL.crl_number)).limit(1))
        last_crl = result.scalar_one_or_none()
        crl_number = (last_crl.crl_number + 1) if last_crl else 1

        # 获取所有吊销的证书
        stmt_revoked = select(Certificate).where(Certificate.status == CertificateStatus.REVOKED)
        revoked_result = await self.session.execute(stmt_revoked)
        revoked_certs = list(revoked_result.scalars().all())

        # 构建吊销证书列表
        revoked_cert_list = self._build_revoked_cert_list(revoked_certs)

        # 生成CRL
        next_update = now + timedelta(hours=next_update_hours)
        crl_builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(ca_cert.subject)
            .last_update(now)
            .next_update(next_update)
        )

        # 添加吊销证书
        for revoked_cert in revoked_cert_list:
            crl_builder = crl_builder.add_revoked_certificate(revoked_cert)

        subject_key_identifier = ca_cert.extensions.get_extension_for_oid(
            x509.oid.ExtensionOID.SUBJECT_KEY_IDENTIFIER
        ).value
        if not isinstance(subject_key_identifier, x509.SubjectKeyIdentifier):
            raise RuntimeError("CA certificate is missing SubjectKeyIdentifier extension")

        # 添加CRL编号扩展
        crl_builder = crl_builder.add_extension(x509.CRLNumber(crl_number), critical=False)
        crl_builder = crl_builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(subject_key_identifier),
            critical=False,
        )

        # 签名CRL
        crl = crl_builder.sign(
            private_key=ca_private_key,
            algorithm=x509_signature_algorithm_for(ca_private_key),
        )

        # 转换为不同格式
        crl_der = crl.public_bytes(Encoding.DER)
        crl_pem = crl.public_bytes(Encoding.PEM).decode("utf-8")

        # 默认分发点
        if not distribution_points:
            distribution_points = [get_default_crl_distribution_point()]

        # 获取签名密钥ID
        signature_key_id = subject_key_identifier.key_identifier.hex()

        # 创建CRL记录
        crl_record = CRL(
            version=version,
            crl_number=crl_number,
            issuer=issuer,
            this_update=now,
            next_update=next_update,
            status=CRLStatus.CURRENT,
            revoked_certificates_count=len(revoked_certs),
            crl_der=crl_der,
            crl_pem=crl_pem,
            crl_size=len(crl_der),
            distribution_points=distribution_points,
            signature_algorithm=signature_algorithm_name(ca_private_key),
            signature_key_id=signature_key_id,
        )

        # 将之前的CRL标记为已取代
        current_result = await self.session.execute(select(CRL).where(CRL.status == CRLStatus.CURRENT))
        for old_crl in current_result.scalars().all():
            old_crl.status = CRLStatus.SUPERSEDED
            self.session.add(old_crl)

        # 保存新CRL
        self.session.add(crl_record)

        # 创建吊销证书条目
        for cert in revoked_certs:
            if cert.revoked_at and cert.revocation_reason:
                entry = RevokedCertificateEntry(
                    crl_id=crl_record.id,
                    serial_number=cert.serial_number,
                    revocation_date=cert.revoked_at,
                    revocation_reason=cert.revocation_reason,
                )
                self.session.add(entry)

        await self.session.flush()
        await self.session.refresh(crl_record)

        return crl_record

    async def get_crl_distribution_points(self) -> dict[str, str | list[str]]:
        """获取CRL分发点配置"""
        current_crl = await self.get_current_crl()
        if not current_crl:
            return {
                "primary": get_default_crl_distribution_point(),
                "mirrors": [],
                "update_interval": "PT24H",
                "max_age": "PT48H",
            }

        return {
            "primary": (
                current_crl.distribution_points[0]
                if current_crl.distribution_points
                else get_default_crl_distribution_point()
            ),
            "mirrors": (current_crl.distribution_points[1:] if len(current_crl.distribution_points) > 1 else []),
            "update_interval": "PT24H",
            "max_age": "PT48H",
        }

    async def get_revoked_entries_for_crl(self, crl_id: str) -> list[RevokedCertificateEntry]:
        """获取指定CRL的所有吊销证书条目，按吊销时间升序排列"""
        statement = (
            select(RevokedCertificateEntry)
            .where(RevokedCertificateEntry.crl_id == crl_id)
            .order_by(literal_column("revocation_date"))
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    def is_crl_expired(self, crl: CRL) -> bool:
        """检查CRL是否过期"""
        return beijing_now() > crl.next_update

    async def mark_expired_crls(self) -> int:
        """标记过期的CRL"""
        now = beijing_now()
        result = await self.session.execute(
            select(CRL).where(and_(CRL.status == CRLStatus.CURRENT, CRL.next_update < now))
        )
        expired_crls = result.scalars().all()

        count = 0
        for crl in expired_crls:
            crl.status = CRLStatus.EXPIRED
            self.session.add(crl)
            count += 1

        if count > 0:
            await self.session.flush()

        return count

    def _build_revoked_cert_list(self, revoked_certs: Sequence[Certificate]) -> list[x509.RevokedCertificate]:
        """将已吊销证书转换为 CRL 条目列表"""
        revoked_cert_list: list[x509.RevokedCertificate] = []
        for cert in revoked_certs:
            if cert.revoked_at and cert.revocation_reason:
                serial_int = self._serialize_cert_serial_number(cert.serial_number)
                reason_flag = self._map_reason_flag(cert.revocation_reason.to_acme_code())
                revoked_cert_list.append(
                    x509.RevokedCertificateBuilder()
                    .serial_number(serial_int)
                    .revocation_date(cert.revoked_at)
                    .add_extension(
                        x509.CRLReason(reason_flag),
                        critical=False,
                    )
                    .build()
                )
        return revoked_cert_list

    def _serialize_cert_serial_number(self, serial_number: str) -> int:
        """将证书序列号转换为 CRL 需要的整数格式"""
        try:
            return int(serial_number, 16)
        except ValueError:
            return int(hashlib.sha256(serial_number.encode()).hexdigest()[:16], 16)

    def _map_reason_flag(self, reason_code: int) -> ReasonFlags:
        """将 ACME 吊销原因码映射为 cryptography ReasonFlags"""
        reason_flag_mapping = {
            0: ReasonFlags.unspecified,
            1: ReasonFlags.key_compromise,
            2: ReasonFlags.ca_compromise,
            3: ReasonFlags.affiliation_changed,
            4: ReasonFlags.superseded,
            5: ReasonFlags.cessation_of_operation,
        }
        return reason_flag_mapping.get(reason_code, ReasonFlags.unspecified)
