"""
CRL API路由

实现CRL (Certificate Revocation List) 相关的API端点。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import (
    CRLDetailResponse,
    CRLDistributionPointsResponse,
    CRLInfoResponse,
    CRLListResponse,
    CRLResponse,
    CRLService,
    CRLStatus,
    RevokedCertificateInfo,
)
from app.core.auth import require_admin_auth
from app.core.base_exception import AppError
from app.core.config import get_settings
from app.core.db_session import get_async_session
from app.core.public_access import limit_public_read_access

from .exception import (
    CRLDetailRetrievalFailedError,
    CRLGenerationFailedError,
    CRLNotFoundError,
    CRLRefreshFailedError,
)

router = APIRouter()

RFC1123_TIME_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"
CRL_DOWNLOAD_FILENAME = 'attachment; filename="agent-ca.crl"'
CRL_CACHE_CONTROL_SHORT = "max-age=3600"
CRL_NOT_FOUND_DETAIL = "No current CRL available."


def get_crl_service(session: Annotated[AsyncSession, Depends(get_async_session)]) -> CRLService:
    """获取CRL服务实例"""
    return CRLService(session)


CRLServiceDep = Annotated[CRLService, Depends(get_crl_service)]
CRLFormatQuery = Annotated[str, Query(alias="format", pattern="^(pem|der)$", description="CRL格式 (pem 或 der)")]
CRLStatusQuery = Annotated[CRLStatus | None, Query(description="CRL状态过滤")]
PageQuery = Annotated[int, Query(ge=1, description="页码")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页数量")]


@router.get(
    "",
    summary="下载CRL",
    description="下载证书吊销列表",
    response_class=Response,
    dependencies=[Depends(limit_public_read_access)],
)
async def download_crl(
    service: CRLServiceDep,
    cert_format: CRLFormatQuery = "der",
) -> Response:
    """
    下载证书吊销列表

    权限级别: public
    """
    current_crl = await service.get_current_crl()
    if not current_crl:
        # 如果没有当前CRL，尝试生成一个新的
        try:
            current_crl = await service.generate_new_crl(issuer="Agent CA")
        except (RuntimeError, ValueError, OSError) as e:
            raise CRLGenerationFailedError() from e

    if cert_format == "pem":
        return Response(
            content=current_crl.crl_pem,
            media_type="application/x-pem-file",
            headers={
                "Content-Disposition": CRL_DOWNLOAD_FILENAME,
                "Cache-Control": CRL_CACHE_CONTROL_SHORT,
                "Expires": current_crl.next_update.strftime(RFC1123_TIME_FORMAT),
                "Last-Modified": current_crl.this_update.strftime(RFC1123_TIME_FORMAT),
                "ETag": f'"{current_crl.version}"',
            },
        )
    return Response(
        content=current_crl.crl_der,
        media_type="application/pkix-crl",
        headers={
            "Content-Disposition": CRL_DOWNLOAD_FILENAME,
            "Cache-Control": CRL_CACHE_CONTROL_SHORT,
            "Expires": current_crl.next_update.strftime(RFC1123_TIME_FORMAT),
            "Last-Modified": current_crl.this_update.strftime(RFC1123_TIME_FORMAT),
            "ETag": f'"{current_crl.version}"',
        },
    )


@router.get(
    "/current",
    summary="获取当前CRL",
    description="获取最新的证书撤销列表",
    response_class=Response,
    dependencies=[Depends(limit_public_read_access)],
)
async def get_current_crl(service: CRLServiceDep) -> Response:
    """
    获取当前有效的CRL

    权限级别: public - CRL是公开信息，任何客户端都可以下载来验证证书状态
    """
    current_crl = await service.get_current_crl()
    if not current_crl:
        raise CRLNotFoundError(CRL_NOT_FOUND_DETAIL)

    # 返回CRL的DER格式内容
    return Response(
        content=current_crl.crl_der,
        media_type="application/pkix-crl",
        headers={
            "Content-Disposition": CRL_DOWNLOAD_FILENAME,
            "Cache-Control": CRL_CACHE_CONTROL_SHORT,
            "Expires": current_crl.next_update.strftime(RFC1123_TIME_FORMAT),
            "Last-Modified": current_crl.this_update.strftime(RFC1123_TIME_FORMAT),
            "ETag": f'"{current_crl.version}"',
        },
    )


@router.get(
    "/current/pem",
    summary="获取当前CRL (PEM格式)",
    description="获取最新的证书撤销列表 (PEM格式)",
    response_class=Response,
    dependencies=[Depends(limit_public_read_access)],
)
async def get_current_crl_pem(service: CRLServiceDep) -> Response:
    """
    获取当前有效的CRL (PEM格式)

    权限级别: public - CRL是公开信息，任何客户端都可以下载来验证证书状态
    """
    current_crl = await service.get_current_crl()
    if not current_crl:
        raise CRLNotFoundError(CRL_NOT_FOUND_DETAIL)

    # 返回CRL的PEM格式内容
    return Response(
        content=current_crl.crl_pem,
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": CRL_DOWNLOAD_FILENAME,
            "Cache-Control": CRL_CACHE_CONTROL_SHORT,
            "Expires": current_crl.next_update.strftime(RFC1123_TIME_FORMAT),
            "Last-Modified": current_crl.this_update.strftime(RFC1123_TIME_FORMAT),
            "ETag": f'"{current_crl.version}"',
        },
    )


@router.get(
    "/info",
    summary="获取CRL信息",
    description="获取CRL的元数据信息",
    dependencies=[Depends(limit_public_read_access)],
)
async def get_crl_info(service: CRLServiceDep) -> CRLInfoResponse:
    """
    获取CRL元数据信息

    权限级别: public - CRL元数据信息公开可访问
    """
    current_crl = await service.get_current_crl()
    if not current_crl:
        raise CRLNotFoundError(CRL_NOT_FOUND_DETAIL)

    distribution_point = (
        current_crl.distribution_points[0]
        if current_crl.distribution_points
        else get_settings().crl_distribution_point_url
    )

    return CRLInfoResponse(
        version=current_crl.version,
        issuer=current_crl.issuer,
        this_update=current_crl.this_update,
        next_update=current_crl.next_update,
        revoked_certificates_count=current_crl.revoked_certificates_count,
        crl_size=current_crl.crl_size,
        distribution_point=distribution_point,
        signature={
            "algorithm": current_crl.signature_algorithm,
            "key_id": current_crl.signature_key_id,
        },
    )


@router.get(
    "/version/{version}",
    summary="获取历史CRL",
    description="获取指定版本的历史CRL",
    response_class=Response,
    dependencies=[Depends(limit_public_read_access)],
)
async def get_crl_by_version(version: str, service: CRLServiceDep) -> Response:
    """
    获取指定版本的历史CRL

    权限级别: public - 历史CRL信息公开可访问

    Args:
        version: CRL版本号，格式为YYYYMMDDHH
    """
    crl = await service.get_crl_by_version(version)
    if not crl:
        raise CRLNotFoundError(f"CRL version {version} not found.")

    # 返回CRL的DER格式内容
    return Response(
        content=crl.crl_der,
        media_type="application/pkcs7-crl",
        headers={
            "Content-Disposition": f'attachment; filename="agent-ca-{version}.crl"',
            "Cache-Control": "max-age=86400",
            "Expires": crl.next_update.strftime(RFC1123_TIME_FORMAT),
            "Last-Modified": crl.this_update.strftime(RFC1123_TIME_FORMAT),
            "ETag": f'"{crl.version}"',
        },
    )


@router.get(
    "/distribution-points",
    summary="获取CRL分发点配置",
    description="获取CRL分发点的配置信息",
    dependencies=[Depends(limit_public_read_access)],
)
async def get_crl_distribution_points(service: CRLServiceDep) -> CRLDistributionPointsResponse:
    """
    获取CRL分发点配置

    权限级别: public - 分发点配置信息公开可访问
    """
    distribution_points = await service.get_crl_distribution_points()
    return CRLDistributionPointsResponse(**distribution_points)


@router.get(
    "/list",
    summary="获取CRL列表",
    description="获取CRL的历史列表（需要管理员权限）",
    dependencies=[Depends(require_admin_auth)],
)
async def get_crl_list(
    service: CRLServiceDep,
    status: CRLStatusQuery = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
) -> CRLListResponse:
    """
    获取CRL列表

    权限级别: admin - CRL管理信息需要管理员权限

    Note: 这里暂时开放为public，实际部署时应该加上认证
    """
    crls, total = await service.get_crl_list(status=status, page=page, page_size=page_size)

    total_pages = (total + page_size - 1) // page_size

    return CRLListResponse(
        items=[CRLResponse.model_validate(crl) for crl in crls],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post(
    "/refresh",
    summary="刷新CRL",
    description="重新生成当前CRL以包含最新的吊销信息",
    dependencies=[Depends(require_admin_auth)],
)
async def refresh_crl(service: CRLServiceDep) -> CRLInfoResponse:
    """
    刷新CRL

    权限级别: admin - CRL重新生成是管理操作
    """
    try:
        # 重新生成CRL（这个方法内部会处理旧CRL的状态）
        new_crl = await service.generate_new_crl(issuer="CN=CA,O=Example,C=CN", next_update_hours=24)

        # 返回新CRL信息
        distribution_point = (
            new_crl.distribution_points[0] if new_crl.distribution_points else get_settings().crl_distribution_point_url
        )

        return CRLInfoResponse(
            version=new_crl.version,
            issuer=new_crl.issuer,
            this_update=new_crl.this_update,
            next_update=new_crl.next_update,
            revoked_certificates_count=new_crl.revoked_certificates_count,
            crl_size=new_crl.crl_size,
            distribution_point=distribution_point,
            signature={
                "algorithm": new_crl.signature_algorithm,
                "key_id": new_crl.signature_key_id,
            },
        )

    except AppError:
        raise
    except (RuntimeError, ValueError, OSError) as e:
        raise CRLRefreshFailedError() from e


@router.get(
    "/detail",
    summary="获取CRL详细信息",
    description="获取当前CRL的详细信息，包括所有吊销证书",
    dependencies=[Depends(limit_public_read_access)],
)
async def get_crl_detail(service: CRLServiceDep) -> CRLDetailResponse:
    """
    获取CRL详细信息

    权限级别: public - CRL详细信息公开可访问
    """
    current_crl = await service.get_current_crl()
    if not current_crl:
        raise CRLNotFoundError(CRL_NOT_FOUND_DETAIL)

    # 通过 service 层获取吊销证书条目
    try:
        revoked_entries = await service.get_revoked_entries_for_crl(str(current_crl.id))

        revoked_certificates: list[RevokedCertificateInfo] = [
            RevokedCertificateInfo(
                serial_number=entry.serial_number,
                revocation_date=entry.revocation_date,
                revocation_reason=entry.revocation_reason,
            )
            for entry in revoked_entries
        ]

        return CRLDetailResponse(
            version=current_crl.version,
            issuer=current_crl.issuer,
            this_update=current_crl.this_update,
            next_update=current_crl.next_update,
            revoked_certificates=revoked_certificates,
            revoked_certificates_count=len(revoked_certificates),
        )

    except AppError:
        raise
    except (RuntimeError, ValueError, OSError) as e:
        raise CRLDetailRetrievalFailedError() from e
