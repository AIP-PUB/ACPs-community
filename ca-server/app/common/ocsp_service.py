"""OCSP（在线证书状态协议）业务服务：生成、缓存、响应 OCSP 查询的核心逻辑"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.ocsp import OCSPResponseBuilder
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from ..core.ca_manager import get_ca_manager
from ..core.config import get_settings
from ..core.crypto_utils import (
    PrivateKeyTypes,
    PublicKeyTypes,
    assert_private_key_matches_certificate,
    ocsp_responder_key_hash,
    require_supported_public_key,
    signature_algorithm_name,
    x509_signature_algorithm_for,
)
from ..ocsp.exception import OCSPProcessingFailedError, OCSPResponderNotFoundError
from .certificate_model import Certificate, CertificateStatus, RevocationReason
from .ocsp_model import (
    OCSPRequest,
    OCSPResponder,
    OCSPResponse,
    OCSPResponseStatus,
)
from .time_utils import beijing_now, format_datetime

logger = structlog.get_logger(__name__)


def build_ocsp_responder_certificate(
    public_key: PublicKeyTypes,
    ca_cert: x509.Certificate,
    ca_private_key: PrivateKeyTypes,
) -> x509.Certificate:
    """使用当前 CA 签发 OCSP responder 证书。"""
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Agent CA OCSP Responder")]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(require_supported_public_key(ca_cert.public_key())),
            critical=False,
        )
    )
    return builder.sign(
        private_key=ca_private_key,
        algorithm=x509_signature_algorithm_for(ca_private_key),
    )


class OCSPService:
    """OCSP服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_responder(self) -> OCSPResponder | None:
        """获取活跃的OCSP响应器"""
        statement = select(OCSPResponder).where(OCSPResponder.is_active == True)  # noqa: E712
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def process_ocsp_request(self, request_der: bytes, client_ip: str | None = None) -> tuple[bytes, int]:
        """处理OCSP请求"""
        start_time = beijing_now()

        try:
            ocsp_request = x509.ocsp.load_der_ocsp_request(request_der)
            request_id = hashlib.sha256(request_der).hexdigest()
            (
                serial_number_int,
                serial_number,
                issuer_key_hash_bytes,
                issuer_name_hash_bytes,
                issuer_key_hash,
                issuer_name_hash,
                request_hash_algorithm,
                hash_algorithm,
            ) = self._extract_ocsp_request_fields(ocsp_request)

            request_record = await self._get_or_create_request_record(
                request_id=request_id,
                serial_number=serial_number,
                issuer_key_hash=issuer_key_hash,
                issuer_name_hash=issuer_name_hash,
                hash_algorithm=hash_algorithm,
                client_ip=client_ip,
                request_der=request_der,
            )

            stmt = select(Certificate).where(Certificate.serial_number == serial_number)
            result = await self.session.execute(stmt)
            certificate = result.scalar_one_or_none()
            cert_status, revocation_time, revocation_reason = self._resolve_certificate_status(certificate)

            ca_cert = self._get_ca_certificate_or_raise()
            responder = await self.bootstrap_default_responder()
            if not responder:
                raise OCSPResponderNotFoundError()

            response_builder, now, next_update = self._build_response_builder(
                certificate=certificate,
                cert_status=cert_status,
                revocation_time=revocation_time,
                revocation_reason=revocation_reason,
                ca_cert=ca_cert,
                issuer_name_hash_bytes=issuer_name_hash_bytes,
                issuer_key_hash_bytes=issuer_key_hash_bytes,
                serial_number_int=serial_number_int,
                request_hash_algorithm=request_hash_algorithm,
            )
            _ocsp_response, response_der, response_signature_algorithm = self._sign_ocsp_response(
                response_builder,
                responder,
            )
            responder_key_hash_hex = self._responder_public_key_hash(
                x509.load_pem_x509_certificate(responder.certificate_pem.encode())
            )
            processing_time = int((beijing_now() - start_time).total_seconds() * 1000)
            self._persist_ocsp_response(
                request_record=request_record,
                serial_number=serial_number,
                cert_status=cert_status,
                now=now,
                next_update=next_update,
                revocation_time=revocation_time,
                revocation_reason=revocation_reason,
                responder_name=responder.name,
                responder_key_hash=responder_key_hash_hex,
                response_der=response_der,
                signature_algorithm=response_signature_algorithm,
                processing_time=processing_time,
            )
            await self.session.flush()
            return response_der, processing_time

        except OCSPResponderNotFoundError:
            raise
        except (ValueError, TypeError, RuntimeError, SQLAlchemyError) as e:
            logger.error("OCSP请求处理失败", error_type=type(e).__name__)
            raise OCSPProcessingFailedError(f"OCSP processing failed: {e!s}") from e

    async def _get_or_create_request_record(
        self,
        request_id: str,
        serial_number: str,
        issuer_key_hash: str,
        issuer_name_hash: str,
        hash_algorithm: str,
        client_ip: str | None,
        request_der: bytes,
    ) -> OCSPRequest:
        """按 request_id 复用或创建 OCSP 请求记录"""
        result = await self.session.execute(select(OCSPRequest).where(OCSPRequest.request_id == request_id))
        existing_request = result.scalar_one_or_none()
        if existing_request:
            if client_ip and existing_request.client_ip != client_ip:
                existing_request.client_ip = client_ip
            return existing_request

        request_record = OCSPRequest(
            request_id=request_id,
            certificate_serial=serial_number,
            issuer_key_hash=issuer_key_hash,
            issuer_name_hash=issuer_name_hash,
            hash_algorithm=hash_algorithm,
            client_ip=client_ip,
            request_der=request_der,
        )
        self.session.add(request_record)
        return request_record

    def _get_ca_certificate_or_raise(self) -> x509.Certificate:
        """获取已初始化的 CA 证书"""
        ca_cert = get_ca_manager().ca_cert
        if ca_cert is None:
            raise RuntimeError("CA certificate is not initialized")
        return ca_cert

    def _build_response_builder(
        self,
        certificate: Certificate | None,
        cert_status: OCSPResponseStatus,
        revocation_time: datetime | None,
        revocation_reason: RevocationReason | None,
        ca_cert: x509.Certificate,
        issuer_name_hash_bytes: bytes,
        issuer_key_hash_bytes: bytes,
        serial_number_int: int,
        request_hash_algorithm: hashes.HashAlgorithm,
    ) -> tuple[OCSPResponseBuilder, datetime, datetime]:
        """根据证书状态构建 OCSPResponseBuilder"""
        now = beijing_now()
        next_update = now + timedelta(hours=24)
        response_builder = OCSPResponseBuilder()

        if certificate is None:
            response_builder = response_builder.add_response_by_hash(
                issuer_name_hash=issuer_name_hash_bytes,
                issuer_key_hash=issuer_key_hash_bytes,
                serial_number=serial_number_int,
                algorithm=request_hash_algorithm,
                cert_status=x509.ocsp.OCSPCertStatus.UNKNOWN,
                this_update=now,
                next_update=next_update,
                revocation_time=None,
                revocation_reason=None,
            )
            return response_builder, now, next_update

        cert_obj = x509.load_pem_x509_certificate(certificate.certificate_pem.encode())
        cert_status_value = x509.ocsp.OCSPCertStatus.GOOD
        if cert_status == OCSPResponseStatus.REVOKED:
            cert_status_value = x509.ocsp.OCSPCertStatus.REVOKED
        elif cert_status in {OCSPResponseStatus.UNKNOWN, OCSPResponseStatus.EXPIRED}:
            # RFC 6960 只有 good / revoked / unknown 三种 ASN.1 状态，项目扩展的 expired
            # 在 DER 响应里必须回落为 unknown，避免把过期证书错误编码为 good。
            cert_status_value = x509.ocsp.OCSPCertStatus.UNKNOWN

        revocation_time_value = None
        revocation_reason_value: x509.ReasonFlags | None = None
        if cert_status == OCSPResponseStatus.REVOKED:
            revocation_time_value = revocation_time or now
            revocation_reason_value = (
                x509.ReasonFlags.key_compromise
                if revocation_reason == RevocationReason.KEY_COMPROMISE
                else x509.ReasonFlags.unspecified
            )

        response_builder = response_builder.add_response(
            cert=cert_obj,
            issuer=ca_cert,
            algorithm=request_hash_algorithm,
            cert_status=cert_status_value,
            this_update=now,
            next_update=next_update,
            revocation_time=revocation_time_value,
            revocation_reason=revocation_reason_value,
        )
        return response_builder, now, next_update

    def _sign_ocsp_response(
        self,
        response_builder: OCSPResponseBuilder,
        responder: OCSPResponder,
    ) -> tuple[x509.ocsp.OCSPResponse, bytes, str]:
        """使用 responder 证书与私钥签发 OCSP 响应"""
        responder_private_key = serialization.load_pem_private_key(responder.private_key_pem.encode(), password=None)
        responder_cert = x509.load_pem_x509_certificate(responder.certificate_pem.encode())
        if not isinstance(
            responder_private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)
        ):
            raise ValueError("Unsupported responder private key type")
        self._validate_responder_profile(responder_cert, responder_private_key)

        response_builder = response_builder.responder_id(x509.ocsp.OCSPResponderEncoding.HASH, responder_cert)
        response_builder = response_builder.certificates([responder_cert])
        algorithm_name = signature_algorithm_name(responder_private_key)
        ocsp_response = response_builder.sign(
            private_key=responder_private_key,
            algorithm=x509_signature_algorithm_for(responder_private_key),
        )
        return ocsp_response, ocsp_response.public_bytes(Encoding.DER), algorithm_name

    def _persist_ocsp_response(
        self,
        request_record: OCSPRequest,
        serial_number: str,
        cert_status: OCSPResponseStatus,
        now: datetime,
        next_update: datetime,
        revocation_time: datetime | None,
        revocation_reason: RevocationReason | None,
        responder_name: str,
        responder_key_hash: str,
        response_der: bytes,
        signature_algorithm: str,
        processing_time: int,
    ) -> None:
        """持久化 OCSP 响应记录"""
        response_record = OCSPResponse(
            request_id=request_record.id,
            certificate_serial=serial_number,
            cert_status=cert_status,
            this_update=now,
            next_update=next_update,
            revocation_time=revocation_time,
            revocation_reason=revocation_reason,
            responder_id=responder_name,
            responder_key_hash=responder_key_hash,
            response_der=response_der,
            response_size=len(response_der),
            signature_algorithm=signature_algorithm,
            processing_time_ms=processing_time,
        )
        self.session.add(response_record)

    async def batch_check_certificates(self, certificates: list[dict[str, str]]) -> list[dict[str, Any]]:
        """批量检查证书状态"""
        responses = []

        for cert_info in certificates:
            serial_number = cert_info.get("serial_number")
            if not serial_number:
                continue

            stmt = select(Certificate).where(Certificate.serial_number == serial_number)
            result = await self.session.execute(stmt)
            certificate = result.scalar_one_or_none()
            status, this_update, next_update, revocation_time, revocation_reason = self._build_batch_status(certificate)

            response: dict[str, Any] = {
                "serial_number": serial_number,
                "status": status if isinstance(status, str) else status.value,
                "this_update": this_update,
                "next_update": next_update,
            }

            if revocation_time:
                response["revocation_time"] = revocation_time

            if revocation_reason:
                response["revocation_reason"] = revocation_reason

            responses.append(response)

        return responses

    async def get_responder_info(self) -> dict[str, Any]:
        """获取OCSP响应器信息"""
        responder = await self.bootstrap_default_responder()
        if not responder:
            raise OCSPResponderNotFoundError()

        responder_cert = x509.load_pem_x509_certificate(responder.certificate_pem.encode())
        responder_private_key = serialization.load_pem_private_key(responder.private_key_pem.encode(), password=None)
        if not isinstance(
            responder_private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)
        ):
            raise RuntimeError("Responder private key type is not supported")
        self._validate_responder_profile(responder_cert, responder_private_key)

        return {
            "responder": {
                "name": responder.name,
                "key_hash": self._responder_public_key_hash(responder_cert),
                "key_fingerprint_sha256": responder_cert.fingerprint(hashes.SHA256()).hex(),
                "certificate": responder.certificate_pem,
            },
            "service_info": {
                "version": "1.0",
                "supported_extensions": responder.supported_extensions,
                "max_request_size": responder.max_request_size,
                "response_timeout": f"PT{responder.response_timeout_seconds}S",
            },
            "endpoints": responder.endpoints,
        }

    async def create_responder(
        self,
        name: str,
        certificate_pem: str,
        private_key_pem: str,
        endpoints: dict[str, Any],
        max_request_size: int = 1048576,
        response_timeout_seconds: int = 30,
        supported_extensions: list[str] | None = None,
    ) -> OCSPResponder:
        """创建OCSP响应器"""
        if supported_extensions is None:
            supported_extensions = ["nonce"]

        # 验证证书和私钥
        try:
            cert = x509.load_pem_x509_certificate(certificate_pem.encode())
            private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
            if not isinstance(private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)):
                raise ValueError("Unsupported responder private key type")
            self._validate_responder_profile(cert, private_key)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid certificate or private key: {e!s}") from e

        # 停用现有的响应器
        result = await self.session.execute(select(OCSPResponder).where(OCSPResponder.is_active == True))  # noqa: E712
        existing_responders = result.scalars().all()
        for responder in existing_responders:
            responder.is_active = False
            self.session.add(responder)

        # 创建新响应器
        responder = OCSPResponder(
            name=name,
            certificate_pem=certificate_pem,
            private_key_pem=private_key_pem,
            certificate_serial=format(cert.serial_number, "x"),
            is_active=True,
            endpoints=endpoints,
            max_request_size=max_request_size,
            response_timeout_seconds=response_timeout_seconds,
            supported_extensions=supported_extensions,
        )

        self.session.add(responder)
        await self.session.flush()
        await self.session.refresh(responder)

        return responder

    async def bootstrap_default_responder(self) -> OCSPResponder:
        """确保存在一个默认的活跃 OCSP responder。"""
        existing = await self.get_active_responder()
        if existing:
            cert = x509.load_pem_x509_certificate(existing.certificate_pem.encode())
            private_key = serialization.load_pem_private_key(existing.private_key_pem.encode(), password=None)
            if not isinstance(private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)):
                raise RuntimeError("Unsupported responder private key type")
            self._validate_responder_profile(cert, private_key)
            return existing

        ca_manager = get_ca_manager()
        if ca_manager.ca_cert is None or ca_manager.ca_private_key is None:
            raise RuntimeError("CA certificate or private key is not initialized")

        private_key = ed25519.Ed25519PrivateKey.generate()
        certificate = build_ocsp_responder_certificate(
            private_key.public_key(),
            ca_manager.ca_cert,
            ca_manager.ca_private_key,
        )
        return await self.create_responder(
            name="Agent CA OCSP Responder",
            certificate_pem=certificate.public_bytes(Encoding.PEM).decode("utf-8"),
            private_key_pem=private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8"),
            endpoints={"primary": get_settings().ocsp_responder_url},
        )

    async def get_ocsp_statistics(self) -> dict[str, Any]:
        """获取OCSP统计信息"""
        # 总请求数
        total_requests_result = await self.session.execute(select(func.count()).select_from(OCSPRequest))
        total_requests = int(total_requests_result.scalar_one())

        # 各种状态的响应数
        valid_responses_result = await self.session.execute(
            select(func.count()).select_from(OCSPResponse).where(OCSPResponse.cert_status == OCSPResponseStatus.GOOD)
        )
        valid_responses = int(valid_responses_result.scalar_one())

        revoked_responses_result = await self.session.execute(
            select(func.count()).select_from(OCSPResponse).where(OCSPResponse.cert_status == OCSPResponseStatus.REVOKED)
        )
        revoked_responses = int(revoked_responses_result.scalar_one())

        unknown_responses_result = await self.session.execute(
            select(func.count()).select_from(OCSPResponse).where(OCSPResponse.cert_status == OCSPResponseStatus.UNKNOWN)
        )
        unknown_responses = int(unknown_responses_result.scalar_one())

        # 平均响应时间
        avg_response_time_result = await self.session.execute(select(func.avg(OCSPResponse.processing_time_ms)))
        avg_response_time = avg_response_time_result.scalar_one() or 0

        # 最近24小时的请求数
        since_24h = beijing_now() - timedelta(hours=24)
        last_24h_requests_result = await self.session.execute(
            select(func.count()).select_from(OCSPRequest).where(OCSPRequest.created_at >= since_24h)
        )
        last_24h_requests = int(last_24h_requests_result.scalar_one())

        return {
            "total_requests": total_requests,
            "good_responses": valid_responses,  # Add alias for backward compatibility
            "valid_responses": valid_responses,
            "revoked_responses": revoked_responses,
            "unknown_responses": unknown_responses,
            "average_response_time_ms": float(avg_response_time),
            "last_24h_requests": last_24h_requests,
        }

    async def get_certificate_status(self, serial_number: str) -> dict[str, Any]:
        """获取证书状态（简化接口）"""
        try:
            # 查询证书
            statement = select(Certificate).where(Certificate.serial_number == serial_number)
            result = await self.session.execute(statement)
            certificate = result.scalar_one_or_none()

            current_time = beijing_now()

            # 构建响应
            response_data = self._build_status_response_base(serial_number, current_time)

            if not certificate:
                # 证书不存在，返回UNKNOWN状态
                response_data["certificateStatus"] = "unknown"
                return response_data

            # 根据证书状态设置响应
            if certificate.status == CertificateStatus.REVOKED:
                response_data.update(
                    {
                        "certificateStatus": "revoked",
                        "revocationTime": (format_datetime(certificate.revoked_at) if certificate.revoked_at else None),
                        "revocationReason": (
                            certificate.revocation_reason.value if certificate.revocation_reason else None
                        ),
                    }
                )
            elif certificate.status == CertificateStatus.VALID:
                response_data["certificateStatus"] = self._resolve_valid_certificate_status(certificate, current_time)
            elif certificate.status == CertificateStatus.EXPIRED:
                response_data["certificateStatus"] = "expired"
            else:
                response_data["certificateStatus"] = "unknown"

            return response_data

        except (ValueError, TypeError, RuntimeError, SQLAlchemyError) as e:
            # 保持向后兼容：异常时返回 unknown 状态而不是抛错
            logger.error(
                "获取证书状态时发生异常",
                serial_number=serial_number,
                error_type=type(e).__name__,
            )
            current_time = beijing_now()
            return {
                "serial_number": serial_number,
                "serialNumber": serial_number,
                "certificateStatus": "unknown",
                "thisUpdate": format_datetime(current_time),
                "nextUpdate": format_datetime(current_time + timedelta(hours=24)),
            }

    def _extract_ocsp_request_fields(
        self,
        ocsp_request: x509.ocsp.OCSPRequest,
    ) -> tuple[int, str, bytes, bytes, str, str, hashes.HashAlgorithm, str]:
        """提取 OCSP 请求核心字段，兼容新旧 cryptography API"""
        if hasattr(ocsp_request, "serial_number"):
            serial_number_int = ocsp_request.serial_number
            issuer_key_hash_bytes = ocsp_request.issuer_key_hash
            issuer_name_hash_bytes = ocsp_request.issuer_name_hash
            request_hash_algorithm = ocsp_request.hash_algorithm
            hash_algorithm = ocsp_request.hash_algorithm.name
        else:
            legacy_request = getattr(ocsp_request, "tbs_request", None)
            if legacy_request is None:
                raise ValueError("OCSP request does not expose legacy request data")
            cert_id = legacy_request.request_list[0].req_cert
            serial_number_int = cert_id.serial_number
            issuer_key_hash_bytes = cert_id.issuer_key_hash
            issuer_name_hash_bytes = cert_id.issuer_name_hash
            request_hash_algorithm = cert_id.hash_algorithm
            hash_algorithm = cert_id.hash_algorithm.name

        serial_number = str(serial_number_int)
        issuer_key_hash = issuer_key_hash_bytes.hex()
        issuer_name_hash = issuer_name_hash_bytes.hex()
        return (
            serial_number_int,
            serial_number,
            issuer_key_hash_bytes,
            issuer_name_hash_bytes,
            issuer_key_hash,
            issuer_name_hash,
            request_hash_algorithm,
            hash_algorithm,
        )

    def _resolve_certificate_status(
        self,
        certificate: Certificate | None,
    ) -> tuple[OCSPResponseStatus, datetime | None, RevocationReason | None]:
        """根据证书记录归一化为 OCSP 状态三元组"""
        if certificate is None:
            return OCSPResponseStatus.UNKNOWN, None, None
        if certificate.status == CertificateStatus.REVOKED:
            return OCSPResponseStatus.REVOKED, certificate.revoked_at, certificate.revocation_reason
        if certificate.status == CertificateStatus.VALID:
            return OCSPResponseStatus.GOOD, None, None
        if certificate.status == CertificateStatus.EXPIRED:
            # RFC 6960 的协议状态只有 good / revoked / unknown。
            # 过期属于应用层扩展语义，在 DER 响应和响应记录里统一回落为 unknown。
            return OCSPResponseStatus.UNKNOWN, None, None
        return OCSPResponseStatus.UNKNOWN, None, None

    def _build_batch_status(
        self,
        certificate: Certificate | None,
    ) -> tuple[OCSPResponseStatus, datetime, datetime, datetime | None, str | None]:
        """构建批量查询场景下单证书状态结果"""
        this_update = beijing_now()
        next_update = this_update + timedelta(hours=24)

        if certificate is None:
            return OCSPResponseStatus.UNKNOWN, this_update, next_update, None, None
        if certificate.status == CertificateStatus.REVOKED:
            return (
                OCSPResponseStatus.REVOKED,
                this_update,
                next_update,
                certificate.revoked_at,
                certificate.revocation_reason.value if certificate.revocation_reason else None,
            )
        if certificate.status == CertificateStatus.VALID:
            return OCSPResponseStatus.GOOD, this_update, next_update, None, None
        if certificate.status == CertificateStatus.EXPIRED:
            return OCSPResponseStatus.EXPIRED, this_update, next_update, certificate.expires_at, None
        return OCSPResponseStatus.UNKNOWN, this_update, next_update, None, None

    def _build_status_response_base(self, serial_number: str, current_time: datetime) -> dict[str, Any]:
        """构建证书状态查询基础响应字段"""
        return {
            "serial_number": serial_number,  # Use snake_case for consistency
            "serialNumber": serial_number,  # Keep camelCase for backward compatibility
            "thisUpdate": format_datetime(current_time),
            "nextUpdate": format_datetime(current_time + timedelta(hours=24)),
        }

    def _resolve_valid_certificate_status(self, certificate: Certificate, current_time: datetime) -> str:
        """判定 valid 证书在当前时间点的对外状态"""
        if not certificate.expires_at:
            return "good"

        expires_at = certificate.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        current_time_aware = current_time if current_time.tzinfo is not None else current_time.replace(tzinfo=UTC)
        return "expired" if expires_at < current_time_aware else "good"

    async def batch_certificate_status(self, certificates: list[dict[str, str]]) -> list[dict[str, Any]]:
        """批量获取证书状态"""
        results = []
        for cert_req in certificates:
            serial_number = cert_req.get("serial_number", "")
            if not serial_number:
                continue

            status = await self.get_certificate_status(serial_number)
            results.append(status)

        return results

    def _responder_public_key_hash(self, certificate: x509.Certificate) -> str:
        """返回 responder 证书公钥对应的 byKey hash。"""
        return ocsp_responder_key_hash(require_supported_public_key(certificate.public_key()))

    def _validate_responder_profile(self, cert: x509.Certificate, private_key: PrivateKeyTypes) -> None:
        """校验 responder 证书画像与私钥匹配。"""
        assert_private_key_matches_certificate(private_key, cert)

        try:
            basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound as exc:
            raise ValueError("OCSP responder certificate must include BasicConstraints") from exc
        if basic_constraints.ca:
            raise ValueError("OCSP responder certificate must not be a CA")

        try:
            key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound as exc:
            raise ValueError("OCSP responder certificate must include KeyUsage") from exc
        if not key_usage.digital_signature:
            raise ValueError("OCSP responder certificate must allow digitalSignature")

        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound as exc:
            raise ValueError("OCSP responder certificate must include id-kp-OCSPSigning") from exc
        if ExtendedKeyUsageOID.OCSP_SIGNING not in eku:
            raise ValueError("OCSP responder certificate must include id-kp-OCSPSigning")
