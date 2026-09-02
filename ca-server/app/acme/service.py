"""ACME 业务逻辑服务：账户管理、订单处理、授权验证、证书签发等核心实现"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.types import CertificatePublicKeyTypes
from cryptography.x509.oid import NameOID
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.common import (
    Certificate,
    CertificateStatus,
    CertificateType,
    RevocationReason,
    beijing_now,
    format_datetime,
    get_next_certificate_version,
)
from app.core.ca_manager import get_ca_manager
from app.core.config import Settings, get_settings

from .exception import AcmeError, AcmeException
from .jwk import compute_jwk_thumbprint, public_jwk_projection, public_key_to_jwk
from .jws_verifier import get_jws_verifier
from .model import (
    AccountStatus,
    AcmeAccount,
    AcmeAuthorization,
    AcmeCertificate,
    AcmeChallenge,
    AcmeNonce,
    AcmeOrder,
    AuthorizationStatus,
    ChallengeStatus,
    OrderStatus,
)
from .schema import (
    AccountCreate,
    AuthorizationCreate,
    CertificateCreate,
    ChallengeCreate,
    JWSRequest,
    OrderCreate,
)
from .utils import ACMEResponse, parse_payload, parse_protected_header

if TYPE_CHECKING:
    from .registry_client import AgentInfo

logger = structlog.get_logger(__name__)

type CertificatePublicKey = CertificatePublicKeyTypes


class NonceService:
    """Nonce 管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def generate_nonce(self) -> str:
        """生成新的 nonce"""
        # 生成32字节的随机数据并转为 base64url
        random_bytes = secrets.token_bytes(32)
        nonce = base64.urlsafe_b64encode(random_bytes).decode("ascii").rstrip("=")

        # 保存到数据库
        nonce_obj = AcmeNonce(nonce=nonce)
        self.session.add(nonce_obj)
        await self.session.flush()

        return nonce

    async def validate_and_consume_nonce(self, nonce: str) -> bool:
        """验证并消费 nonce"""
        statement = select(AcmeNonce).where(
            AcmeNonce.nonce == nonce,
            AcmeNonce.used == False,  # noqa: E712 - SQLAlchemy 列比较需要显式 == False
            AcmeNonce.expires > beijing_now(),
        )
        result = await self.session.execute(statement)
        nonce_obj = result.scalar_one_or_none()

        if not nonce_obj:
            return False

        # 标记为已使用
        nonce_obj.used = True
        self.session.add(nonce_obj)
        await self.session.flush()

        return True

    async def cleanup_expired_nonces(self) -> None:
        """清理过期的 nonce"""
        statement = select(AcmeNonce).where(AcmeNonce.expires <= beijing_now())
        result = await self.session.execute(statement)
        expired_nonces = result.scalars().all()

        for nonce in expired_nonces:
            await self.session.delete(nonce)

        await self.session.flush()


class JWKService:
    """JSON Web Key 处理服务"""

    @staticmethod
    def compute_jwk_thumbprint(jwk: dict[str, Any]) -> str:
        """计算 JWK 指纹"""
        return compute_jwk_thumbprint(jwk)

    @staticmethod
    def project_public_jwk(jwk: dict[str, Any]) -> dict[str, str]:
        """提取公开 JWK 投影。"""
        return public_jwk_projection(jwk)

    @staticmethod
    def create_key_authorization(token: str, jwk: dict[str, Any]) -> str:
        """创建密钥授权字符串"""
        thumbprint = JWKService.compute_jwk_thumbprint(jwk)
        return f"{token}.{thumbprint}"

    @staticmethod
    def verify_jws_request(
        jws_data: str,
        public_key_jwk: dict[str, Any],
        expected_nonce: str | None = None,
        expected_url: str | None = None,
    ) -> dict[str, Any]:
        """验证 JWS 请求"""
        jws_verifier = get_jws_verifier()
        return jws_verifier.verify_jws_signature(jws_data, public_key_jwk, expected_nonce, expected_url)

    @staticmethod
    def verify_new_account_signature(request_data: JWSRequest, jwk: dict[str, Any]) -> None:
        """校验 new-account 首次请求签名（账户尚未创建）"""
        jws_verifier = get_jws_verifier()
        jws_string = f"{request_data.protected}.{request_data.payload}.{request_data.signature}"
        try:
            jws_verifier.verify_jws_signature(jws_string, jwk, expected_nonce=None, expected_url=None)
        except Exception as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.BAD_SIGNATURE,
                error_msg=f"Invalid signature for new account: {e!s}",
            ) from e


async def verify_nonce(protected: dict[str, Any], nonce_service: NonceService) -> None:
    """验证 nonce"""
    nonce = protected.get("nonce")
    if not nonce:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.BAD_NONCE,
            error_msg="Missing nonce in protected header",
        )

    if not await nonce_service.validate_and_consume_nonce(nonce):
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.BAD_NONCE,
            error_msg="Invalid or expired nonce",
        )


async def parse_jws_request(
    request_data: JWSRequest,
    nonce_service: NonceService,
    expected_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """解析并验证 JWS 请求"""
    try:
        protected = parse_protected_header(request_data.protected)
        payload = parse_payload(request_data.payload)
        await verify_nonce(protected, nonce_service)

        if expected_url and protected.get("url") != expected_url:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg=f"URL mismatch: expected {expected_url}, got {protected.get('url')}",
            )

        return protected, payload, request_data.signature
    except AcmeException:
        raise
    except Exception as e:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED_REQUEST,
            error_msg=f"Invalid JWS format: {e!s}",
        ) from e


async def get_account_from_request(protected: dict[str, Any], account_service: AccountService) -> AcmeAccount:
    """从请求中解析账户"""
    if "kid" in protected:
        account_url = protected["kid"]
        account_id = account_url.split("/")[-1]
        account = await account_service.get_account_by_id(int(account_id))
    elif "jwk" in protected:
        jwk = JWKService.project_public_jwk(protected["jwk"])
        key_id = JWKService.compute_jwk_thumbprint(jwk)
        account = await account_service.get_account_by_key_id(key_id)
    else:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED_REQUEST,
            error_msg="Missing kid or jwk in protected header",
        )

    if not account:
        raise AcmeException(
            status_code=404,
            error_name=AcmeError.ACCOUNT_NOT_FOUND,
            error_msg="Account not found",
        )

    ensure_account_is_active(account)

    return account


async def get_account_by_jwk(protected: dict[str, Any], account_service: AccountService) -> AcmeAccount | None:
    """根据 protected header 中的 jwk 查询已有账户"""
    jwk = protected.get("jwk")
    if not isinstance(jwk, dict):
        return None

    key_id = JWKService.compute_jwk_thumbprint(jwk)
    account = await account_service.get_account_by_key_id(key_id)
    if account is not None:
        ensure_account_is_active(account)
    return account


def ensure_account_is_active(account: AcmeAccount) -> None:
    """确保 ACME 账户仍处于可用状态"""
    if account.status == AccountStatus.DEACTIVATED:
        raise AcmeException(
            status_code=403,
            error_name=AcmeError.UNAUTHORIZED,
            error_msg="Account is deactivated",
        )


def verify_jws_signature_with_jwk(request_data: JWSRequest, public_key_jwk: dict[str, Any]) -> bool:
    """使用给定 JWK 验证 JWS 签名"""
    try:
        jws_verifier = get_jws_verifier()
        jws_string = f"{request_data.protected}.{request_data.payload}.{request_data.signature}"
        jws_verifier.verify_jws_signature(
            jws_string,
            public_key_jwk,
            expected_nonce=None,
            expected_url=None,
        )
        return True
    except Exception as e:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.BAD_SIGNATURE,
            error_msg=f"Invalid signature: {e!s}",
        ) from e


def verify_jws_signature(request_data: JWSRequest, _protected: dict[str, Any], account: AcmeAccount) -> bool:
    """验证 JWS 签名"""
    account_jwk = json.loads(account.public_key)
    return verify_jws_signature_with_jwk(request_data, account_jwk)


class AccountService:
    """账户管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_account(self, account_data: AccountCreate) -> AcmeAccount:
        """创建新账户或返回现有账户"""
        # 检查账户是否已存在
        statement = select(AcmeAccount).where(AcmeAccount.key_id == account_data.key_id)
        result = await self.session.execute(statement)
        existing_account = result.scalar_one_or_none()

        if existing_account:
            # 根据ACME RFC，返回现有账户而不是错误
            return existing_account

        account = AcmeAccount(
            key_id=account_data.key_id,
            public_key=account_data.public_key,
            contact=account_data.contact,
            terms_of_service_agreed=account_data.terms_of_service_agreed,
            external_account_binding=account_data.external_account_binding,
            aic=account_data.aic,
        )

        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)

        return account

    async def get_account_by_key_id(self, key_id: str) -> AcmeAccount | None:
        """根据密钥ID获取账户"""
        statement = select(AcmeAccount).where(AcmeAccount.key_id == key_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_account_by_id(self, account_id: int) -> AcmeAccount | None:
        """根据ID获取账户"""
        statement = select(AcmeAccount).where(AcmeAccount.id == account_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def update_account(self, account: AcmeAccount, **kwargs: Any) -> AcmeAccount:
        """更新账户信息"""
        for key, value in kwargs.items():
            if hasattr(account, key):
                setattr(account, key, value)

        account.updated_at = beijing_now()
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)

        return account

    async def apply_key_change(self, account: AcmeAccount, payload: dict[str, Any], base_url: str) -> AcmeAccount:
        """处理 key-change 请求中的内层 JWS 验证与密钥替换"""
        if not isinstance(payload, dict):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Payload must be a JSON object",
            )

        inner_protected_b64 = payload.get("protected")
        inner_payload_b64 = payload.get("payload")
        inner_signature_b64 = payload.get("signature")

        if not isinstance(inner_protected_b64, str):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Inner protected header must be a base64 string",
            )
        if not isinstance(inner_payload_b64, str):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Inner payload must be a base64 string",
            )
        if not isinstance(inner_signature_b64, str):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Inner signature must be a base64 string",
            )

        if not all([inner_protected_b64, inner_payload_b64, inner_signature_b64]):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Invalid inner JWS format",
            )

        inner_protected = parse_protected_header(inner_protected_b64)
        if "jwk" not in inner_protected:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="New JWK must be provided in inner JWS",
            )

        new_jwk = inner_protected["jwk"]
        new_public_jwk = JWKService.project_public_jwk(new_jwk)

        jws_verifier = get_jws_verifier()
        inner_jws_string = f"{inner_protected_b64}.{inner_payload_b64}.{inner_signature_b64}"
        try:
            jws_verifier.verify_jws_signature(inner_jws_string, new_public_jwk, expected_nonce=None, expected_url=None)
        except Exception as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.BAD_SIGNATURE,
                error_msg=f"Invalid signature on inner JWS: {e!s}",
            ) from e

        inner_payload = parse_payload(inner_payload_b64)
        expected_account_url = f"{base_url}/acct/{account.id}"
        if inner_payload.get("account") != expected_account_url:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Account URL mismatch in inner payload",
            )

        current_jwk = json.loads(account.public_key)
        provided_old_key = inner_payload.get("oldKey")
        if not isinstance(provided_old_key, dict):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="oldKey does not match current account key",
            )
        if JWKService.project_public_jwk(provided_old_key) != current_jwk:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="oldKey does not match current account key",
            )

        new_key_id = JWKService.compute_jwk_thumbprint(new_public_jwk)
        existing_account = await self.get_account_by_key_id(new_key_id)
        if existing_account and existing_account.id != account.id:
            raise AcmeException(
                status_code=409,
                error_name="KEY_IN_USE",
                error_msg="New key is already associated with another account",
            )

        return await self.update_account(
            account,
            key_id=new_key_id,
            public_key=json.dumps(new_public_jwk),
        )


class OrderService:
    """订单管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_order(self, order_data: OrderCreate) -> AcmeOrder:
        """创建新订单"""
        order_id = self._generate_order_id()

        order = AcmeOrder(
            order_id=order_id,
            account_id=order_data.account_id,
            identifiers=order_data.identifiers,
            not_before=order_data.not_before,
            not_after=order_data.not_after,
            expires=beijing_now() + timedelta(days=1),  # 订单24小时过期
        )

        self.session.add(order)
        await self.session.flush()
        await self.session.refresh(order)

        return order

    async def get_order_by_id(self, order_id: str) -> AcmeOrder | None:
        """根据订单ID获取订单"""
        statement = select(AcmeOrder).where(AcmeOrder.order_id == order_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_order_by_pk(self, order_pk: int) -> AcmeOrder | None:
        """根据订单主键获取订单"""
        statement = select(AcmeOrder).where(AcmeOrder.id == order_pk)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def update_order_status(self, order: AcmeOrder, status: OrderStatus) -> AcmeOrder:
        """更新订单状态"""
        order.status = status
        order.updated_at = beijing_now()
        self.session.add(order)
        await self.session.flush()
        await self.session.refresh(order)

        return order

    def _generate_order_id(self) -> str:
        """生成订单ID"""
        return f"order_{secrets.token_urlsafe(16)}"

    async def normalize_and_validate_agent_identifiers(
        self,
        identifiers: list[dict[str, Any]],
        account_aic: str,
        registry_client: Any,
    ) -> tuple[list[dict[str, str]], list[AgentInfo]]:
        """规范化并校验订单中的 agent 标识符"""
        if not identifiers:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Missing identifiers",
            )

        normalized_identifiers: list[dict[str, str]] = []
        validated_agents: list[AgentInfo] = []

        for identifier in identifiers:
            if identifier.get("type") != "agent":
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.UNSUPPORTED_IDENTIFIER,
                    error_msg=f"Unsupported identifier type: {identifier.get('type')}",
                )

            raw_aic = identifier.get("value")
            if not raw_aic:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED_REQUEST,
                    error_msg="Missing identifier value",
                )

            normalized_aic = str(raw_aic).strip().upper()
            if normalized_aic != account_aic:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.INVALID_IDENTIFIER,
                    error_msg="Order identifier does not match account-bound AIC",
                )

            agent_info = await registry_client.validate_aic_and_get_info(normalized_aic)
            if not agent_info:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.INVALID_IDENTIFIER,
                    error_msg=f"Invalid or inactive agent: {normalized_aic}",
                )

            normalized_identifier: dict[str, str] = {"type": "agent", "value": normalized_aic}
            raw_usage = identifier.get("usage")
            if raw_usage is not None:
                normalized_usage = str(raw_usage).strip()
                if normalized_usage not in {"clientAuth", "serverAuth"}:
                    raise AcmeException(
                        status_code=400,
                        error_name=AcmeError.MALFORMED_REQUEST,
                        error_msg=f"Invalid identifier usage: {normalized_usage}",
                    )
                normalized_identifier["usage"] = normalized_usage

            normalized_identifiers.append(normalized_identifier)
            validated_agents.append(agent_info)

        return normalized_identifiers, validated_agents

    async def notify_certificate_requests(
        self,
        validated_agents: list[AgentInfo],
        order_id: str,
        registry_client: Any,
    ) -> None:
        """通知 Agent Registry 有新的证书请求"""
        for agent_info in validated_agents:
            await registry_client.register_certificate_request(agent_info.aic, order_id)

    async def mark_ready_with_authorizations(self, order: AcmeOrder, authorizations: list[str]) -> AcmeOrder:
        """设置订单授权并标记为 READY"""
        order.authorizations = authorizations
        return await self.update_order_status(order, OrderStatus.READY)


class AuthorizationService:
    """授权管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_authorization(self, auth_data: AuthorizationCreate) -> AcmeAuthorization:
        """创建授权"""
        authz_id = self._generate_authz_id()

        authorization = AcmeAuthorization(
            authz_id=authz_id,
            order_id=auth_data.order_id,
            identifier=auth_data.identifier,
            expires=auth_data.expires,
        )

        self.session.add(authorization)
        await self.session.flush()
        await self.session.refresh(authorization)

        return authorization

    async def get_authorization_by_id(self, authz_id: str) -> AcmeAuthorization | None:
        """根据授权ID获取授权"""
        statement = (
            select(AcmeAuthorization)
            .options(
                selectinload(cast("Any", AcmeAuthorization.order)),
                selectinload(cast("Any", AcmeAuthorization.challenges)),
            )
            .execution_options(populate_existing=True)
            .where(AcmeAuthorization.authz_id == authz_id)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_authorizations_by_order_id(self, order_id: int) -> list[AcmeAuthorization]:
        """根据订单ID获取所有授权"""
        statement = select(AcmeAuthorization).where(AcmeAuthorization.order_id == order_id)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def update_authorization_status(
        self, authorization: AcmeAuthorization, status: AuthorizationStatus
    ) -> AcmeAuthorization:
        """更新授权状态"""
        authorization.status = status
        authorization.updated_at = beijing_now()
        self.session.add(authorization)
        await self.session.flush()
        await self.session.refresh(authorization)

        return authorization

    async def deactivate_authorization(self, authorization: AcmeAuthorization) -> AcmeAuthorization:
        """停用 authorization，并在必要时阻止其继续作为签发依据"""
        if authorization.status == AuthorizationStatus.DEACTIVATED:
            return authorization

        if authorization.status not in {AuthorizationStatus.PENDING, AuthorizationStatus.VALID}:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Authorization cannot be deactivated from its current status",
            )

        authorization.status = AuthorizationStatus.DEACTIVATED
        authorization.updated_at = beijing_now()
        self.session.add(authorization)

        order = authorization.order
        if order.status in {OrderStatus.PENDING, OrderStatus.READY, OrderStatus.PROCESSING}:
            order.status = OrderStatus.INVALID
            order.updated_at = beijing_now()
            self.session.add(order)

        await self.session.flush()
        await self.session.refresh(authorization)

        return authorization

    def _generate_authz_id(self) -> str:
        """生成授权ID"""
        return f"authz_{secrets.token_urlsafe(16)}"

    async def create_valid_authorization_urls(
        self,
        order: AcmeOrder,
        identifiers: list[dict[str, str]],
        base_url: str,
    ) -> list[str]:
        """为订单创建授权并直接标记为 valid，返回授权 URL 列表。

        兼容层：只读，不再增强（Phase 4.3）。

        Agent CA 认证的对象是 AIC，而不是域名控制权。AIC 已在 new-account 的 EAB
        阶段与 account 绑定，因此 new-order 只需确认订单中的 agent identifier 仍
        归属于同一 AIC，无需再执行独立的 ACME challenge。
        """
        authorization_urls: list[str] = []

        for identifier in identifiers:
            auth_data = AuthorizationCreate(
                order_id=order.id,
                identifier=identifier,
                expires=order.expires,
            )
            authorization = await self.create_authorization(auth_data)
            await self.update_authorization_status(authorization, AuthorizationStatus.VALID)
            authorization_urls.append(f"{base_url}/authz/{authorization.authz_id}")

        return authorization_urls

    def build_authorization_response(self, authorization: AcmeAuthorization, base_url: str) -> dict[str, Any]:
        """构建授权查询响应体"""
        challenges: list[dict[str, Any]] = []

        for challenge in authorization.challenges:
            # 兼容层：只读，不再增强（Phase 4.3）。继续返回 challenges 数组与 URL 形状，
            # 但 Agent CA 已在 EAB 阶段完成 AIC 认证，客户端不需要再通过 challenge
            # URL 证明域名控制权；这里的 /challenge/{id} 仅是兼容输出投影，不代表
            # 当前服务端仍提供独立的 challenge 主链路或 HTTP-01 验证入口。
            challenge_data: dict[str, Any] = {
                "type": challenge.type,
                "url": f"{base_url}/challenge/{challenge.challenge_id}",
                "status": challenge.status,
                "token": challenge.token,
            }

            if challenge.validated:
                challenge_data["validated"] = format_datetime(challenge.validated)

            if challenge.error:
                challenge_data["error"] = challenge.error

            challenges.append(challenge_data)

        return {
            "identifier": authorization.identifier,
            "status": authorization.status,
            "expires": format_datetime(authorization.expires),
            "challenges": challenges,
        }


class ChallengeService:
    """挑战管理服务。

    兼容层：只读，不再增强（Phase 4.3）。
    """

    # ACME RFC 占位兼容：系统仍保留 challenge 记录与状态字段，
    # 但 Agent CA 在 new-account 的 EAB 阶段已经把 account 绑定到已确认的 AIC。
    # 后续订单流程认证的是“该 account 是否仍在为同一 AIC 申请证书”，不是域名控制权，
    # 因此 challenge 不驱动真实的 HTTP-01 校验，只用于维持 authorization/challenges
    # 的响应结构与状态可观测性。

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_challenge(self, challenge_data: ChallengeCreate) -> AcmeChallenge:
        """创建挑战"""
        challenge_id = self._generate_challenge_id()

        challenge = AcmeChallenge(
            challenge_id=challenge_id,
            authorization_id=challenge_data.authorization_id,
            type=challenge_data.type,
            token=challenge_data.token,
        )

        self.session.add(challenge)
        await self.session.flush()
        await self.session.refresh(challenge)

        return challenge

    async def get_challenge_by_id(self, challenge_id: str) -> AcmeChallenge | None:
        """根据挑战ID获取挑战"""
        statement = select(AcmeChallenge).where(AcmeChallenge.challenge_id == challenge_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def update_challenge_status(
        self,
        challenge: AcmeChallenge,
        status: ChallengeStatus,
        error: dict[str, Any] | None = None,
    ) -> AcmeChallenge:
        """更新挑战状态"""
        challenge.status = status
        challenge.updated_at = beijing_now()

        if status == ChallengeStatus.VALID:
            # 这里表示服务端基于 account<AIC 绑定关系直接确认 challenge 有效，
            # 并不会对外发起 HTTP-01 探测。
            challenge.validated = beijing_now()

        if error:
            challenge.error = error

        self.session.add(challenge)
        await self.session.flush()
        await self.session.refresh(challenge)

        return challenge

    def _generate_challenge_id(self) -> str:
        """生成挑战ID"""
        return f"challenge_{secrets.token_urlsafe(16)}"


class CertificateService:
    """证书管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def issue_certificate(
        self,
        order: AcmeOrder,
        csr_der: bytes,
        agent_infos: list[AgentInfo] | None = None,
        usage: str = "clientAuth",
    ) -> list[AcmeCertificate]:
        """签发证书 - 支持为每个Agent分别签发证书

        Args:
            order: ACME订单
            csr_der: DER格式的CSR
            agent_infos: Agent信息列表，用于构造证书DN
            usage: 证书用途，"clientAuth" 或 "serverAuth"（单一 EKU）

        Returns:
            List[AcmeCertificate]: 签发的证书列表（每个Agent一张证书）
        """
        # 解析 CSR
        csr = x509.load_der_x509_csr(csr_der)

        # 验证 CSR 中的主体名称与订单中的标识符匹配
        self._verify_csr_identifiers(csr, order.identifiers)

        # 提取 Agent ID 列表
        agent_ids = [identifier["value"] for identifier in order.identifiers if identifier["type"] == "agent"]

        if not agent_ids:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.INVALID_CERTIFICATE_FORMAT,
                error_msg="No valid agent identifiers found in order",
            )

        # 根据业务规则：每个Agent分别签发一张证书
        certificates = []
        for i, agent_id in enumerate(agent_ids):
            # 获取对应的Agent信息
            agent_info = None
            if agent_infos and i < len(agent_infos):
                # 查找匹配的Agent信息
                for info in agent_infos:
                    if hasattr(info, "agent_id") and info.agent_id == agent_id:
                        agent_info = info
                        break
                # 如果没找到匹配的，使用第一个作为默认
                if agent_info is None and agent_infos:
                    agent_info = agent_infos[0]

            # 为单个Agent生成证书
            cert_pem = self._generate_certificate_for_agent(csr, agent_id, agent_info, usage=usage)

            # 计算 not_after（与实际签发证书一致，动态有效期）
            cert_validity_days = 49
            if agent_info:
                cert_validity_days = agent_info.get_certificate_validity_days(
                    max_days=self.settings.max_certificate_validity_days
                )
            not_after = beijing_now() + timedelta(days=cert_validity_days)

            # 从生成的证书中提取序列号，确保数据库记录与实际证书一致
            serial_number = self._extract_serial_number_from_cert_pem(cert_pem)

            # 创建证书记录
            cert_data = CertificateCreate(
                order_id=order.id,
                serial_number=serial_number,
                certificate_pem=cert_pem,
                subject=self._extract_subject_from_cert_pem(cert_pem),
                not_before=beijing_now(),
                not_after=not_after,
                aic=agent_id,  # 设置AIC字段
            )

            certificate = await self._create_certificate(cert_data)
            certificates.append(certificate)

        return certificates

    async def get_certificate_by_id(self, cert_id: str) -> AcmeCertificate | None:
        """根据证书ID获取证书"""
        statement = select(AcmeCertificate).where(AcmeCertificate.cert_id == cert_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    def decode_csr_payload(self, csr_data: str | None) -> bytes:
        """解码 ACME finalize 请求中的 CSR 载荷"""
        if not csr_data:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Missing CSR",
            )

        try:
            return base64.urlsafe_b64decode(csr_data + "==")
        except Exception as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg=f"Invalid CSR encoding: {e!s}",
            ) from e

    def decode_revoke_certificate_payload(self, cert_data: str | None) -> bytes:
        """解码 ACME revoke-cert 请求中的 certificate 载荷"""
        if not cert_data:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Missing certificate",
            )

        try:
            return base64.urlsafe_b64decode(cert_data + "==")
        except Exception:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Invalid certificate encoding",
            ) from None

    def public_key_to_jwk(self, public_key: CertificatePublicKey) -> dict[str, str]:
        """将证书中的公钥转换为 JWK，供 ACME JWS 验签使用"""
        if not isinstance(public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey, ed25519.Ed25519PublicKey)):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported certificate key type: {type(public_key).__name__}",
            )
        return public_key_to_jwk(public_key)

    def validate_revocation_reason(self, reason_code: Any) -> int:
        """校验并返回吊销原因码"""
        if reason_code is None:
            return 0

        if not isinstance(reason_code, int) or reason_code < 0 or reason_code > 5:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Invalid revocation reason code",
            )

        return reason_code

    async def validate_order_agents(
        self,
        identifiers: list[dict[str, Any]],
        registry_client: Any,
    ) -> list[AgentInfo]:
        """校验订单标识符并返回可签发证书的 Agent 信息列表"""
        agent_infos: list[AgentInfo] = []

        for identifier in identifiers:
            aic = identifier.get("value")
            if not isinstance(aic, str) or not aic:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED_REQUEST,
                    error_msg="Order identifier is missing value",
                )

            agent_info = await registry_client.validate_aic_and_get_info(aic)
            if not agent_info:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.INVALID_IDENTIFIER,
                    error_msg=f"Agent {aic} is no longer valid",
                )
            agent_infos.append(agent_info)

        return agent_infos

    async def notify_issued_certificates(
        self,
        agent_infos: list[AgentInfo],
        certificates: list[AcmeCertificate],
        order_id: str,
        registry_client: Any,
    ) -> None:
        """通知 Agent Registry 证书签发完成"""
        for i, agent_info in enumerate(agent_infos):
            cert_id = certificates[i].cert_id if i < len(certificates) else certificates[0].cert_id
            await registry_client.notify_certificate_issued(agent_info.aic, order_id, cert_id)

    async def get_revocable_certificate(
        self,
        cert_der: bytes,
        account_id: int | None = None,
    ) -> tuple[AcmeCertificate, x509.Certificate]:
        """解析证书并返回可吊销的证书对象与解析后的 X.509 证书"""
        try:
            cert = x509.load_der_x509_certificate(cert_der, default_backend())
            serial_number = f"{cert.serial_number:X}"
        except Exception as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg=f"Invalid certificate format: {e!s}",
            ) from e

        statement = select(AcmeCertificate).where(AcmeCertificate.serial_number == serial_number)
        result = await self.session.execute(statement)
        acme_cert = result.scalar_one_or_none()

        if not acme_cert:
            raise AcmeException(
                status_code=404,
                error_name=AcmeError.CERTIFICATE_NOT_FOUND,
                error_msg="Certificate not found",
            )

        if account_id is not None:
            order = await self.session.get(AcmeOrder, acme_cert.order_id)
            if not order or order.account_id != account_id:
                raise AcmeException(
                    status_code=403,
                    error_name=AcmeError.UNAUTHORIZED,
                    error_msg="Certificate does not belong to this account",
                )

        if acme_cert.status == CertificateStatus.REVOKED:
            raise AcmeException(
                status_code=400,
                error_name="ALREADY_REVOKED",
                error_msg="Certificate is already revoked",
            )

        return acme_cert, cert

    async def revoke_certificate(self, certificate: AcmeCertificate, reason: int | None = None) -> None:
        """吊销证书"""
        revoked_at = beijing_now()
        revocation_reason = RevocationReason.from_acme_code(reason) if reason is not None else None

        certificate.status = CertificateStatus.REVOKED
        certificate.revoked_at = revoked_at
        certificate.revocation_reason = revocation_reason
        certificate.updated_at = revoked_at

        statement = select(Certificate).where(Certificate.serial_number == certificate.serial_number)
        result = await self.session.execute(statement)
        common_certificate = result.scalar_one_or_none()
        if common_certificate is not None and common_certificate.status != CertificateStatus.REVOKED:
            common_certificate.status = CertificateStatus.REVOKED
            common_certificate.revoked_at = revoked_at
            common_certificate.revocation_reason = revocation_reason
            common_certificate.updated_at = revoked_at
            self.session.add(common_certificate)

        self.session.add(certificate)
        await self.session.flush()

    def _verify_csr_identifiers(self, csr: x509.CertificateSigningRequest, identifiers: list[dict[str, str]]) -> None:
        """验证 CSR 中的标识符"""
        # 提取 CSR 的主体名称
        subject = csr.subject
        cn: str | None = None
        for attribute in subject:
            if attribute.oid == NameOID.COMMON_NAME:
                raw = attribute.value
                cn = raw.decode() if isinstance(raw, bytes) else raw
                break

        if not cn:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.INVALID_CERTIFICATE_FORMAT,
                error_msg="CSR must contain Common Name",
            )

        # 验证 CN 是否匹配订单中的标识符
        agent_ids = [identifier["value"] for identifier in identifiers if identifier["type"] == "agent"]

        valid_cns = set(agent_ids)

        if cn not in valid_cns:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.INVALID_CERTIFICATE_FORMAT,
                error_msg=f"CSR Common Name '{cn}' does not match any ordered identifiers",
            )

    def _generate_certificate_for_agent(
        self,
        csr: x509.CertificateSigningRequest,
        agent_id: str,
        agent_info: AgentInfo | None = None,
        usage: str = "clientAuth",
    ) -> str:
        """为单个Agent生成证书

        Args:
            csr: 证书签名请求
            agent_id: Agent ID
            agent_info: Agent信息，用于构造证书DN
            usage: 证书用途，"clientAuth" 或 "serverAuth"（单一 EKU）

        Returns:
            str: PEM格式的证书
        """
        # 构造证书DN信息
        cert_subject_components = {}
        dns_names: list[str] = []
        ip_addresses: list[str] = []
        validity_days = 49
        if agent_info:
            cert_subject_components = agent_info.get_certificate_subject_components()
            dns_names = agent_info.get_certificate_dns_names()
            ip_addresses = agent_info.get_certificate_ip_addresses()
            validity_days = agent_info.get_certificate_validity_days(
                max_days=self.settings.max_certificate_validity_days
            )

        # serverAuth 证书缺少 DNS/IP SAN 时发出警告（AIP-v2.1.0 §4.2.5）
        if usage == "serverAuth" and not dns_names and not ip_addresses:
            logger.warning(
                "serverAuth 证书缺少 DNS/IP SAN，TLS 主机名验证将失败",
                agent_id=agent_id,
            )

        # 使用 CA 管理器签发证书（单个Agent）
        ca_manager = get_ca_manager()
        try:
            return ca_manager.sign_certificate(
                csr,
                [agent_id],
                validity_days=validity_days,
                subject_components=cert_subject_components,
                dns_names=dns_names or None,
                ip_addresses=ip_addresses or None,
                usage=usage,
            )
        except Exception as e:
            raise AcmeException(
                status_code=500,
                error_name=AcmeError.SERVER_INTERNAL,
                error_msg=f"Certificate signing failed for agent {agent_id}: {e!s}",
            ) from e

    def _extract_serial_number_from_cert_pem(self, cert_pem: str) -> str:
        """从PEM格式证书中提取序列号

        Args:
            cert_pem: PEM格式的证书字符串

        Returns:
            str: 16进制格式的序列号
        """
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
            return format(cert.serial_number, "x").upper()
        except Exception as e:
            raise AcmeException(
                status_code=500,
                error_name=AcmeError.SERVER_INTERNAL,
                error_msg=f"Failed to extract serial number from certificate: {e!s}",
            ) from e

    def _extract_subject_from_csr(self, csr: x509.CertificateSigningRequest) -> dict[str, Any]:
        """从 CSR 提取主体信息"""
        subject_dict = {}
        for attribute in csr.subject:
            if attribute.oid == NameOID.COMMON_NAME:
                subject_dict["CN"] = attribute.value
            elif attribute.oid == NameOID.ORGANIZATION_NAME:
                subject_dict["O"] = attribute.value
            elif attribute.oid == NameOID.ORGANIZATIONAL_UNIT_NAME:
                subject_dict["OU"] = attribute.value
            elif attribute.oid == NameOID.COUNTRY_NAME:
                subject_dict["C"] = attribute.value

        return subject_dict

    def _extract_subject_from_cert_pem(self, cert_pem: str) -> dict[str, Any]:
        """从证书PEM中提取主体信息

        Args:
            cert_pem: PEM格式的证书

        Returns:
            Dict[str, Any]: 主体信息字典
        """
        try:
            # 解析证书
            cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())

            # 提取主体信息
            subject_dict = {}
            for attribute in cert.subject:
                if attribute.oid == NameOID.COMMON_NAME:
                    subject_dict["CN"] = attribute.value
                elif attribute.oid == NameOID.ORGANIZATION_NAME:
                    subject_dict["O"] = attribute.value
                elif attribute.oid == NameOID.ORGANIZATIONAL_UNIT_NAME:
                    subject_dict["OU"] = attribute.value
                elif attribute.oid == NameOID.COUNTRY_NAME:
                    subject_dict["C"] = attribute.value
                elif attribute.oid == NameOID.STATE_OR_PROVINCE_NAME:
                    subject_dict["ST"] = attribute.value
                elif attribute.oid == NameOID.LOCALITY_NAME:
                    subject_dict["L"] = attribute.value

            return subject_dict
        except Exception as e:
            raise AcmeException(
                status_code=500,
                error_name=AcmeError.SERVER_INTERNAL,
                error_msg=f"Failed to extract subject from certificate: {e!s}",
            ) from e

    def _generate_serial_number(self) -> str:
        """生成证书序列号"""
        return secrets.token_hex(16)

    async def _create_certificate(self, cert_data: CertificateCreate) -> AcmeCertificate:
        """创建证书记录"""
        cert_id = f"cert_{secrets.token_urlsafe(16)}"

        # 创建ACME证书记录
        certificate = AcmeCertificate(
            cert_id=cert_id,
            order_id=cert_data.order_id,
            serial_number=cert_data.serial_number,
            certificate_pem=cert_data.certificate_pem,
            subject=cert_data.subject,
            not_before=cert_data.not_before,
            not_after=cert_data.not_after,
            aic=cert_data.aic,  # 设置AIC字段
        )

        self.session.add(certificate)

        # 同时在Certificate表中创建记录，用于证书管理和批量吊销
        if cert_data.aic:
            common_cert = Certificate(
                certificate_type=CertificateType.AGENT,  # Agent证书类型
                serial_number=cert_data.serial_number,
                subject=f"CN={self.settings.build_agent_common_name(cert_data.aic)}",
                issuer="Agent Trusted Registration CA",  # 可以根据实际情况调整
                status=CertificateStatus.VALID,
                certificate_pem=cert_data.certificate_pem,
                public_key=self._extract_public_key_from_cert_pem(cert_data.certificate_pem),
                expires_at=cert_data.not_after,
                aic=cert_data.aic,  # 设置AIC字段用于批量查询
                version=await get_next_certificate_version(self.session, cert_data.aic),
            )
            self.session.add(common_cert)

        await self.session.flush()
        await self.session.refresh(certificate)

        return certificate

    def _extract_public_key_from_cert_pem(self, cert_pem: str) -> str:
        """从证书PEM中提取公钥"""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
            public_key = cert.public_key()

            return public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
        except Exception as e:
            logger.error("公钥提取失败", error=str(e))
            return ""  # 返回空字符串作为fallback


# ================== 工厂函数 ==================


def get_nonce_service(session: AsyncSession) -> NonceService:
    """获取 Nonce 服务实例"""
    return NonceService(session)


def get_account_service(session: AsyncSession) -> AccountService:
    """获取账户服务实例"""
    return AccountService(session)


def get_order_service(session: AsyncSession) -> OrderService:
    """获取订单服务实例"""
    return OrderService(session)


def get_authorization_service(session: AsyncSession) -> AuthorizationService:
    """获取授权服务实例"""
    return AuthorizationService(session)


def get_challenge_service(session: AsyncSession) -> ChallengeService:
    """获取挑战服务实例"""
    return ChallengeService(session)


def get_certificate_service(session: AsyncSession) -> CertificateService:
    """获取证书服务实例"""
    return CertificateService(session)


# ================== ACME 工具函数 ==================


def get_configured_acme_base_url(settings: Settings) -> str:
    """获取配置中的 ACME 基础 URL，去除尾部斜杠"""
    return settings.acme_directory_url.rstrip("/")


def build_expected_acme_request_url(settings: Settings, path_suffix: str) -> str:
    """基于配置的外部 ACME 基址构造受保护头中的期望 URL"""
    normalized_suffix = path_suffix if path_suffix.startswith("/") else f"/{path_suffix}"
    return f"{get_configured_acme_base_url(settings)}{normalized_suffix}"


def ensure_post_as_get_uses_empty_payload(request_data: JWSRequest) -> None:
    """确保查询类 POST-as-GET 请求使用真正的空 payload"""
    if request_data.payload != "":
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED_REQUEST,
            error_msg="POST-as-GET requests must use an empty payload",
        )


async def create_acme_response(
    data: dict[str, Any],
    nonce_service: NonceService,
    status_code: int = 200,
) -> JSONResponse:
    """创建 ACME 响应"""
    new_nonce = await nonce_service.generate_nonce()
    response = ACMEResponse(data, status_code).add_nonce(new_nonce).to_json_response()
    if not isinstance(response, JSONResponse):
        raise TypeError("ACMEResponse.to_json_response() must return JSONResponse")
    return response
