"""公钥查询 API：供验签方根据证书序列号或 AIC 获取公钥。

端点均为公开只读，与 trust-bundle、OCSP 采用相同的限流策略。
公钥（PublicKey）属于公开密钥材料，本身无需保密。

验签流程：
  Agent 在签名 AMP 审计日志时，将自身 X.509 证书的序列号写入
  LogRecord.integrity.kid 字段；验签方用该 kid 调用 GET /keys/{serial_number}
  取回公钥，完成签名校验（RFC 5280 §4.1.2.2）。
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import CertificateStatus, format_datetime
from app.core.db_session import get_async_session
from app.core.public_access import limit_public_read_access

from .exception import CertificateNotFoundError, InvalidAICFormatError
from .service import CertificateManagementService

logger = structlog.get_logger(__name__)

router = APIRouter()


# ── 响应模型 ──────────────────────────────────────────────────────────────────


class PublicKeyResponse(BaseModel):
    """公钥查询响应（序列号查询与 AIC 查询共用）。"""

    public_key: str = Field(..., serialization_alias="publicKey", description="SPKI PEM 格式公钥")
    aic: str | None = Field(None, description="Agent Identity Code（非 Agent 证书时为 null）")
    serial_number: str = Field(..., serialization_alias="serialNumber", description="证书序列号（十六进制大写）")
    status: CertificateStatus = Field(..., description="证书状态：valid / revoked / expired")
    expires_at: str = Field(..., serialization_alias="expiresAt", description="证书过期时间（ISO 8601）")

    model_config = ConfigDict(populate_by_name=True)


# ── 依赖注入 ──────────────────────────────────────────────────────────────────


def _get_certificate_service(
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> CertificateManagementService:
    return CertificateManagementService(session)


_ServiceDep = Annotated[CertificateManagementService, Depends(_get_certificate_service)]
_SerialPath = Annotated[
    str,
    Path(
        description="证书序列号，即 AMP LogRecord.integrity.kid（十六进制，大小写不敏感）",
        min_length=1,
    ),
]
_AICQuery = Annotated[
    str,
    Query(description="Agent Identity Code", min_length=1),
]


# ── 端点 ──────────────────────────────────────────────────────────────────────


@router.get(
    "/keys/{serial_number}",
    summary="按证书序列号获取公钥",
    dependencies=[Depends(limit_public_read_access)],
    responses={
        200: {"description": "返回公钥及证书元数据"},
        404: {"description": "证书序列号不存在"},
        429: {"description": "请求过于频繁"},
    },
)
async def get_public_key_by_serial(
    serial_number: _SerialPath,
    service: _ServiceDep,
) -> PublicKeyResponse:
    """根据证书序列号返回公钥，主要用于 AMP 审计日志验签。

    serial_number 即 AMP LogRecord.integrity.kid 的值（X.509 serialNumber 字段，
    十六进制字符串）。对已吊销或已过期证书同样返回结果，调用方须检查 status 字段，
    对非 valid 状态的签名应拒绝信任。
    """
    normalized = serial_number.strip().upper()
    certificate = await service.get_certificate_by_serial(normalized)
    if certificate is None:
        raise CertificateNotFoundError(f"Certificate with serial {serial_number!r} not found.")

    logger.info("公钥查询（序列号）", serial_number=normalized, aic=certificate.aic, status=certificate.status)
    return PublicKeyResponse(
        public_key=certificate.public_key,
        aic=certificate.aic,
        serial_number=certificate.serial_number,
        status=certificate.status,
        expires_at=format_datetime(certificate.expires_at),
    )


@router.get(
    "/keys",
    summary="按 AIC 获取当前有效公钥",
    dependencies=[Depends(limit_public_read_access)],
    responses={
        200: {"description": "返回该 AIC 版本号最高的有效证书公钥"},
        400: {"description": "AIC 参数缺失或格式无效"},
        404: {"description": "该 AIC 无有效证书"},
        429: {"description": "请求过于频繁"},
    },
)
async def get_current_public_key_by_aic(
    aic: _AICQuery,
    service: _ServiceDep,
) -> PublicKeyResponse:
    """根据 AIC 返回当前最新有效证书的公钥，用于验签前预热缓存或发现当前签名密钥。

    返回指定 AIC 中版本号（version）最高的 valid 状态证书的公钥。
    响应中的 serialNumber 可直接用作后续 GET /keys/{serial_number} 的查询参数。

    若需验证历史日志（已轮转的旧密钥），应使用 GET /keys/{serial_number} 精确查询。
    """
    normalized_aic = (aic or "").strip()
    if not normalized_aic:
        raise InvalidAICFormatError()

    certificate = await service.get_current_valid_certificate_by_aic(normalized_aic)

    logger.info(
        "公钥查询（AIC）",
        aic=normalized_aic,
        serial_number=certificate.serial_number,
        version=certificate.version,
    )
    return PublicKeyResponse(
        public_key=certificate.public_key,
        aic=certificate.aic,
        serial_number=certificate.serial_number,
        status=certificate.status,
        expires_at=format_datetime(certificate.expires_at),
    )
