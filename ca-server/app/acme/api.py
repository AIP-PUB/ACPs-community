"""ACME API 路由：实现 ACME 协议端点，处理证书申请、验证、签发等请求"""

import json
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import BEIJING_TZ, beijing_now, format_datetime
from app.core.ca_manager import get_ca_manager
from app.core.config import Settings, get_settings
from app.core.db_session import get_async_session

from .eab_verifier import verify_eab_binding
from .exception import AcmeError, AcmeException
from .model import OrderStatus
from .registry_client import get_registry_client
from .schema import (
    AccountCreate,
    ACMEDirectory,
    JWSRequest,
    OrderCreate,
)
from .service import (
    JWKService,
    build_expected_acme_request_url,
    create_acme_response,
    ensure_post_as_get_uses_empty_payload,
    get_account_by_jwk,
    get_account_from_request,
    get_account_service,
    get_authorization_service,
    get_certificate_service,
    get_configured_acme_base_url,
    get_nonce_service,
    get_order_service,
    parse_jws_request,
    verify_jws_signature,
    verify_jws_signature_with_jwk,
)

router = APIRouter()

# scope="function"：在响应发出前结束依赖并 commit。
# 默认 request scope 会在响应发送后才 commit；局域网客户端可能在
# Replay-Nonce 已返回、nonce 行尚未可见时立刻 POST → BAD_NONCE。
SessionDep = Annotated[AsyncSession, Depends(get_async_session, scope="function")]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ================== API 端点 ==================


@router.get(
    "/directory",
    response_model=ACMEDirectory,
    summary="获取 ACME 服务目录",
    tags=["ACME"],
    responses={200: {"description": "ACME 服务目录"}},
)
async def get_directory(request: Request, settings: SettingsDep) -> ACMEDirectory:
    """获取 ACME 服务目录

    返回服务支持的所有端点 URL 和配置信息。

    Returns:
        ACMEDirectory: 目录信息，包含所有服务端点 URL
    """
    base_url = settings.acme_directory_url

    return ACMEDirectory(
        newNonce=f"{base_url}/new-nonce",
        newAccount=f"{base_url}/new-account",
        newOrder=f"{base_url}/new-order",
        newAuthz=None,
        revokeCert=f"{base_url}/revoke-cert",
        keyChange=f"{base_url}/key-change",
        meta={
            "externalAccountRequired": True,
        },
    )


@router.get(
    "/ca-cert",
    summary="获取 CA 证书",
    tags=["ACME"],
    responses={200: {"description": "CA 证书 PEM"}},
)
async def get_ca_certificate() -> Response:
    """获取 CA 根证书"""
    try:
        ca_manager = get_ca_manager()
        ca_cert_pem = ca_manager.get_ca_certificate_pem()

        return Response(
            content=ca_cert_pem,
            media_type="application/x-pem-file",
            headers={
                "Content-Disposition": "attachment; filename=ca.crt",
                "Cache-Control": "public, max-age=86400",  # 缓存1天
            },
        )
    except (RuntimeError, ValueError, OSError) as e:
        raise AcmeException(
            status_code=500,
            error_name=AcmeError.SERVER_INTERNAL,
            error_msg=f"Failed to retrieve CA certificate: {e!s}",
        ) from e


@router.head(
    "/new-nonce",
    summary="获取新 Nonce（HEAD）",
    tags=["ACME"],
    responses={200: {"description": "成功返回 nonce"}},
)
@router.get("/new-nonce", summary="获取新 Nonce", tags=["ACME"], responses={200: {"description": "成功返回 nonce"}})
async def get_new_nonce(session: SessionDep) -> Response:
    """获取新的 nonce

    用于防止重放攻击。客户端必须为每个请求获取新的 nonce。

    Returns:
        Response: 包含 Replay-Nonce 头的响应
    """
    nonce_service = get_nonce_service(session)
    new_nonce = await nonce_service.generate_nonce()

    return Response(
        status_code=200,
        headers={"Replay-Nonce": new_nonce, "Cache-Control": "no-store"},
    )


@router.post(
    "/new-account",
    summary="创建新账户",
    tags=["ACME"],
    responses={
        201: {"description": "账户已创建"},
        400: {"description": "请求错误"},
        409: {"description": "账户已存在"},
    },
    status_code=201,
)
async def create_account(
    request_data: JWSRequest,
    _request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """创建新账户"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    registry_client = get_registry_client()

    try:
        expected_url = build_expected_acme_request_url(settings, "/new-account")
        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url=expected_url)

        # 验证必须包含 jwk
        if "jwk" not in protected:
            raise AcmeException(
                status_code=400,
                error_name="MALFORMED_REQUEST",
                error_msg="New account request must include jwk",
            )

        jwk = JWKService.project_public_jwk(protected["jwk"])
        key_id = JWKService.compute_jwk_thumbprint(jwk)

        # 检查是否是查询已存在账户
        if payload.get("onlyReturnExisting", False):
            existing_account = await account_service.get_account_by_key_id(key_id)
            if not existing_account:
                raise AcmeException(
                    status_code=404,
                    error_name="ACCOUNT_NOT_FOUND",
                    error_msg="Account does not exist",
                )

            account = existing_account

            # 对于已存在的账户，验证JWS签名
            verify_jws_signature(request_data, protected, account)
        else:
            external_account_binding = payload.get("externalAccountBinding")
            if not isinstance(external_account_binding, dict):
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.EXTERNAL_ACCOUNT_REQUIRED,
                    error_msg="externalAccountBinding is required for new account",
                )

            # 对于新账户，直接使用请求中的 JWK 进行签名校验
            JWKService.verify_new_account_signature(request_data, jwk)

            expected_eab_url = protected.get("url") or f"{get_configured_acme_base_url(settings)}/new-account"
            # 兼容层：只读，不再增强（Phase 4.3）。Agent CA 以 EAB 作为主体认证入口：这里把账户公钥绑定到已确认的 AIC，
            # 后续不再需要通过独立 ACME challenge 证明域名控制权。
            bound_aic = await verify_eab_binding(
                external_account_binding,
                jwk,
                expected_eab_url,
                registry_client,
            )

            # 创建新账户
            account_data = AccountCreate(
                key_id=key_id,
                public_key=json.dumps(jwk),
                contact=payload.get("contact"),
                terms_of_service_agreed=payload.get("termsOfServiceAgreed", False),
                external_account_binding=external_account_binding,
                aic=bound_aic,
            )

            account = await account_service.create_account(account_data)

        base_url = get_configured_acme_base_url(settings)
        account_url = f"{base_url}/acct/{account.id}"

        response_data = {
            "status": account.status,
            "contact": account.contact,
            "termsOfServiceAgreed": account.terms_of_service_agreed,
            "orders": f"{account_url}/orders",
        }

        # 检查账户是否是新创建的
        # 如果账户的created_at时间很近（比如5秒内），则认为是新创建的
        account_created_at = account.created_at
        if account_created_at.tzinfo is None:
            account_created_at = account_created_at.replace(tzinfo=BEIJING_TZ)

        is_new_account = (beijing_now() - account_created_at) < timedelta(seconds=5)
        status_code = 201 if is_new_account else 200

        response = await create_acme_response(response_data, nonce_service, status_code)
        response.headers["Location"] = account_url

        return response

    except AcmeException:
        raise


@router.post(
    "/acct/{account_id}",
    summary="更新或查询账户",
    tags=["ACME"],
    responses={200: {"description": "账户信息"}, 400: {"description": "请求错误"}, 404: {"description": "账户不存在"}},
)
async def update_account(
    account_id: int,
    request_data: JWSRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """更新账户信息"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, f"/acct/{account_id}")
        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)
        if not payload:
            ensure_post_as_get_uses_empty_payload(request_data)

        account = await get_account_from_request(protected, account_service)
        verify_jws_signature(request_data, protected, account)

        if account.id != account_id:
            raise AcmeException(
                status_code=403,
                error_name="UNAUTHORIZED",
                error_msg="Account ID mismatch",
            )

        # 更新账户信息
        update_data = {}
        if "contact" in payload:
            update_data["contact"] = payload["contact"]
        if "status" in payload:
            update_data["status"] = payload["status"]

        if update_data:
            account = await account_service.update_account(account, **update_data)

        base_url = get_configured_acme_base_url(settings)
        response_data = {
            "status": account.status,
            "contact": account.contact,
            "termsOfServiceAgreed": account.terms_of_service_agreed,
            "orders": f"{base_url}/acct/{account.id}/orders",
        }

        return await create_acme_response(response_data, nonce_service)

    except AcmeException:
        raise


@router.post(
    "/new-order",
    summary="创建新订单",
    tags=["ACME"],
    responses={
        201: {"description": "订单已创建"},
        400: {"description": "请求错误"},
        403: {"description": "账户已停用"},
    },
    status_code=201,
)
async def create_order(
    request_data: JWSRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """创建新订单"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    order_service = get_order_service(session)
    authorization_service = get_authorization_service(session)
    registry_client = get_registry_client()

    try:
        expected_url = build_expected_acme_request_url(settings, "/new-order")

        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)

        account = await get_account_from_request(protected, account_service)

        # 验证JWS签名
        verify_jws_signature(request_data, protected, account)

        if not account.aic:
            raise AcmeException(
                status_code=403,
                error_name=AcmeError.UNAUTHORIZED,
                error_msg="Account is not bound to an AIC via external account binding",
            )

        identifiers_payload = payload.get("identifiers", [])
        identifiers, validated_agents = await order_service.normalize_and_validate_agent_identifiers(
            identifiers=identifiers_payload,
            account_aic=account.aic,
            registry_client=registry_client,
        )

        # 创建订单
        order_data = OrderCreate(
            account_id=account.id,
            identifiers=identifiers,
            not_before=payload.get("notBefore"),
            not_after=payload.get("notAfter"),
        )

        order = await order_service.create_order(order_data)

        base_url = get_configured_acme_base_url(settings)
        # 兼容层：只读，不再增强（Phase 4.3）。Agent CA 认证的对象是 AIC 而不是 DNS 名称。account 在 new-account/EAB
        # 阶段已经绑定到 AIC，这里只需确认 identifiers 仍属于该 AIC，即可把授权
        # 直接推进到可签发状态，并保留 challenge 字段形状用于 ACME 兼容输出。
        authorizations = await authorization_service.create_valid_authorization_urls(order, identifiers, base_url)
        await order_service.notify_certificate_requests(validated_agents, order.order_id, registry_client)
        order = await order_service.mark_ready_with_authorizations(order, authorizations)

        order_url = f"{base_url}/order/{order.order_id}"

        response_data = {
            "status": order.status,
            "expires": format_datetime(order.expires),
            "identifiers": order.identifiers,
            "authorizations": authorizations,
            "finalize": f"{order_url}/finalize",
        }

        if order.not_before:
            response_data["notBefore"] = format_datetime(order.not_before)
        if order.not_after:
            response_data["notAfter"] = format_datetime(order.not_after)

        response = await create_acme_response(response_data, nonce_service, 201)
        response.headers["Location"] = order_url

        return response

    except AcmeException:
        raise


@router.post(
    "/order/{order_id}",
    summary="查询或修改订单",
    tags=["ACME"],
    responses={200: {"description": "订单信息"}, 400: {"description": "请求错误"}, 404: {"description": "订单不存在"}},
)
async def get_order(
    order_id: str,
    request_data: JWSRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """查询订单状态"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    order_service = get_order_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, f"/order/{order_id}")
        protected, _, _ = await parse_jws_request(request_data, nonce_service, expected_url)
        ensure_post_as_get_uses_empty_payload(request_data)

        account = await get_account_from_request(protected, account_service)
        verify_jws_signature(request_data, protected, account)
        order = await order_service.get_order_by_id(order_id)

        if not order or order.account_id != account.id:
            raise AcmeException(
                status_code=404,
                error_name="ORDER_NOT_FOUND",
                error_msg="Order not found",
            )

        base_url = get_configured_acme_base_url(settings)
        response_data = {
            "status": order.status,
            "expires": format_datetime(order.expires),
            "identifiers": order.identifiers,
            "authorizations": order.authorizations or [],
            "finalize": f"{base_url}/order/{order.order_id}/finalize",
        }

        if order.certificate:
            response_data["certificate"] = order.certificate

        return await create_acme_response(response_data, nonce_service)

    except AcmeException:
        raise


@router.post(
    "/authz/{authz_id}",
    summary="查询或修改授权（兼容层）",
    tags=["ACME"],
    responses={200: {"description": "授权信息"}, 400: {"description": "请求错误"}, 404: {"description": "授权不存在"}},
)
async def get_authorization(
    authz_id: str,
    request_data: JWSRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """获取授权信息或按 RFC 8555 停用 authorization。

    兼容层：只读，不再增强（Phase 4.3）。
    该 endpoint 仅保留 ACME authorization/challenges 的响应形状与 deactivation 语义；
    当前 ATR 主链路发证不再依赖独立 challenge 交互。
    """
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    authorization_service = get_authorization_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, f"/authz/{authz_id}")
        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)

        account = await get_account_from_request(protected, account_service)
        verify_jws_signature(request_data, protected, account)
        authorization = await authorization_service.get_authorization_by_id(authz_id)

        if not authorization:
            raise AcmeException(
                status_code=404,
                error_name="AUTHORIZATION_NOT_FOUND",
                error_msg="Authorization not found",
            )

        # 验证账户权限
        if authorization.order.account_id != account.id:
            raise AcmeException(
                status_code=403,
                error_name="UNAUTHORIZED",
                error_msg="Unauthorized access to authorization",
            )

        if payload:
            if payload != {"status": "deactivated"}:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED_REQUEST,
                    error_msg="Authorization update payload must be exactly {'status': 'deactivated'}",
                )

            authorization = await authorization_service.deactivate_authorization(authorization)
        else:
            ensure_post_as_get_uses_empty_payload(request_data)

        base_url = get_configured_acme_base_url(settings)
        response_data = authorization_service.build_authorization_response(authorization, base_url)

        return await create_acme_response(response_data, nonce_service)

    except AcmeException:
        raise


@router.post(
    "/order/{order_id}/finalize",
    summary="完成订单并颁发证书",
    tags=["ACME"],
    responses={
        200: {"description": "订单状态"},
        400: {"description": "CSR 错误"},
        403: {"description": "授权未完成"},
        404: {"description": "订单不存在"},
    },
)
async def finalize_order(
    order_id: str,
    request_data: JWSRequest,
    _request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """完成订单，签发证书"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    order_service = get_order_service(session)
    certificate_service = get_certificate_service(session)
    registry_client = get_registry_client()

    try:
        expected_url = build_expected_acme_request_url(settings, f"/order/{order_id}/finalize")

        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)

        account = await get_account_from_request(protected, account_service)

        # 验证JWS签名
        verify_jws_signature(request_data, protected, account)

        order = await order_service.get_order_by_id(order_id)

        if not order or order.account_id != account.id:
            raise AcmeException(
                status_code=404,
                error_name="ORDER_NOT_FOUND",
                error_msg="Order not found",
            )

        if order.status != OrderStatus.READY:
            raise AcmeException(
                status_code=400,
                error_name="ORDER_NOT_READY",
                error_msg="Order is not ready for finalization",
            )

        csr_der = certificate_service.decode_csr_payload(payload.get("csr"))
        agent_infos = await certificate_service.validate_order_agents(order.identifiers, registry_client)

        # 更新订单状态为处理中
        order = await order_service.update_order_status(order, OrderStatus.PROCESSING)

        # 签发证书 - 传递Agent信息用于构造证书DN
        # 从订单标识符中提取 usage（v2.1.0: 支持单一 EKU 由客户端指定）
        # 现在返回证书列表（每个Agent一张证书）
        usage = "clientAuth"
        if order.identifiers:
            usage = order.identifiers[0].get("usage") or "clientAuth"
        certificates = await certificate_service.issue_certificate(order, csr_der, agent_infos, usage=usage)

        base_url = get_configured_acme_base_url(settings)

        # 更新订单状态为有效
        # 为多证书订单，提供第一张证书的URL作为主要证书URL
        if certificates:
            primary_cert_url = f"{base_url}/cert/{certificates[0].cert_id}"
            order.certificate = primary_cert_url

        order = await order_service.update_order_status(order, OrderStatus.VALID)

        await certificate_service.notify_issued_certificates(
            agent_infos,
            certificates,
            order.order_id,
            registry_client,
        )

        # 构造响应数据，包含多证书信息
        response_data: dict[str, Any] = {
            "status": order.status,
            "expires": format_datetime(order.expires),
            "identifiers": order.identifiers,
            "authorizations": order.authorizations or [],
            "finalize": f"{base_url}/order/{order.order_id}/finalize",
            "certificate": order.certificate,
        }

        # 如果有多张证书，添加扩展信息
        if len(certificates) > 1:
            cert_urls = [f"{base_url}/cert/{cert.cert_id}" for cert in certificates]
            response_data["certificates"] = cert_urls
            response_data["certificate_count"] = len(certificates)

        response = await create_acme_response(response_data, nonce_service)
        response.headers["Location"] = f"{base_url}/order/{order.order_id}"
        return response

    except AcmeException:
        raise


@router.post(
    "/cert/{cert_id}",
    summary="获取已颁发证书",
    tags=["ACME"],
    responses={200: {"description": "证书 PEM"}, 404: {"description": "证书不存在"}},
)
async def get_certificate(
    cert_id: str,
    request_data: JWSRequest,
    _request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """获取已颁发的证书"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    certificate_service = get_certificate_service(session)
    order_service = get_order_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, f"/cert/{cert_id}")
        protected, _, _ = await parse_jws_request(request_data, nonce_service, expected_url)
        ensure_post_as_get_uses_empty_payload(request_data)

        account = await get_account_from_request(protected, account_service)
        verify_jws_signature(request_data, protected, account)
        certificate = await certificate_service.get_certificate_by_id(cert_id)

        if not certificate:
            raise AcmeException(
                status_code=404,
                error_name=AcmeError.CERTIFICATE_NOT_FOUND,
                error_msg="Certificate not found",
            )

        # 获取关联的订单来验证访问权限
        order = await order_service.get_order_by_pk(certificate.order_id)
        if not order or order.account_id != account.id:
            raise AcmeException(
                status_code=403,
                error_name="UNAUTHORIZED",
                error_msg="Unauthorized access to certificate",
            )

        new_nonce = await nonce_service.generate_nonce()

        # 拼接叶子证书 + Intermediate CA PEM，符合 RFC 8555 §7.4.2 证书链格式
        ca_manager = get_ca_manager()
        chain_content = certificate.certificate_pem + ca_manager.get_issuer_chain_pem()

        return Response(
            content=chain_content,
            media_type="application/pem-certificate-chain",
            headers={"Replay-Nonce": new_nonce, "Cache-Control": "no-store"},
        )

    except AcmeException:
        raise


@router.post(
    "/revoke-cert",
    summary="撤销证书",
    tags=["ACME"],
    responses={200: {"description": "撤销成功"}, 400: {"description": "请求错误"}, 403: {"description": "无权撤销"}},
)
async def revoke_certificate(request_data: JWSRequest, session: SessionDep, settings: SettingsDep) -> Response:
    """吊销证书。

    对应 RFC 8555 revoke-cert 语义：既接受账户密钥，也接受证书私钥。
    """
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)
    certificate_service = get_certificate_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, "/revoke-cert")
        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)

        cert_der = certificate_service.decode_revoke_certificate_payload(payload.get("certificate"))
        reason_code = certificate_service.validate_revocation_reason(payload.get("reason"))

        account = None
        account_id = None
        if "kid" in protected:
            account = await get_account_from_request(protected, account_service)
        elif "jwk" in protected:
            account = await get_account_by_jwk(protected, account_service)
        else:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Missing kid or jwk in protected header",
            )

        if account is not None:
            verify_jws_signature(request_data, protected, account)

            account_id = account.id
            if account_id is None:
                raise AcmeException(
                    status_code=500,
                    error_name=AcmeError.SERVER_INTERNAL,
                    error_msg="Account ID is missing",
                )

        acme_cert, parsed_certificate = await certificate_service.get_revocable_certificate(
            cert_der=cert_der,
            account_id=account_id,
        )

        if account is None:
            certificate_jwk = certificate_service.public_key_to_jwk(parsed_certificate.public_key())

            # RFC 8555 允许使用证书私钥发起 revoke-cert。
            # 若证书私钥已经泄露，攻击者本就能够继续冒充该证书主体；允许其吊销同一证书
            # 不会带来更严重的新增后果，因此这里遵循标准接受该路径。
            verify_jws_signature_with_jwk(request_data, certificate_jwk)

        # 执行证书吊销
        await certificate_service.revoke_certificate(acme_cert, reason_code)

        # 返回成功响应（空内容）
        new_nonce = await nonce_service.generate_nonce()
        return Response(
            status_code=200,
            headers={"Replay-Nonce": new_nonce, "Cache-Control": "no-store"},
        )

    except AcmeException:
        raise


@router.post(
    "/key-change",
    summary="更换账户密钥",
    tags=["ACME"],
    responses={
        200: {"description": "密钥更换成功"},
        400: {"description": "请求错误"},
        403: {"description": "新密钥验证失败"},
    },
)
async def change_key(
    request_data: JWSRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """更换账户密钥"""
    nonce_service = get_nonce_service(session)
    account_service = get_account_service(session)

    try:
        expected_url = build_expected_acme_request_url(settings, "/key-change")
        protected, payload, _ = await parse_jws_request(request_data, nonce_service, expected_url)

        account = await get_account_from_request(protected, account_service)

        # 验证JWS签名
        verify_jws_signature(request_data, protected, account)

        # 解析内层JWS（新密钥签名的请求）
        if not payload:
            raise AcmeException(
                status_code=400,
                error_name="MALFORMED_REQUEST",
                error_msg="Missing payload in key change request",
            )

        base_url = get_configured_acme_base_url(settings)
        if not isinstance(payload, dict):
            raise AcmeException(
                status_code=400,
                error_name="MALFORMED_REQUEST",
                error_msg="Payload must be a JSON object",
            )
        account = await account_service.apply_key_change(account=account, payload=payload, base_url=base_url)

        # 构建响应
        response_data = {
            "status": account.status,
            "contact": account.contact,
            "termsOfServiceAgreed": account.terms_of_service_agreed,
            "orders": f"{base_url}/acct/{account.id}/orders",
        }

        return await create_acme_response(response_data, nonce_service)

    except AcmeException:
        raise
