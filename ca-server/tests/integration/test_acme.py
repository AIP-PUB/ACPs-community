"""
ACME API 完整测试套件

此测试文件包含所有ACME协议相关的测试：
1. 基础ACME协议功能（目录、nonce、基本流程）
2. 账户管理功能
3. JWS签名验证
4. Agent标识符验证
5. 证书签发功能
6. 证书策略验证
"""

import base64
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, TypedDict, cast
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.acme.jws_verifier import JWSVerifier
from app.acme.model import (
    AccountStatus,
    AcmeAccount,
    AcmeAuthorization,
    AcmeCertificate,
    AcmeNonce,
    AcmeOrder,
    AuthorizationStatus,
    OrderStatus,
)
from app.acme.registry_client import AgentInfo, RegistryClient
from app.common import CertificateStatus, RevocationReason, beijing_now
from app.core.config import Settings, get_settings
from app.main import app


class _RevokeContext(TypedDict):
    """revoke-cert 测试上下文数据结构。"""

    account: AcmeAccount
    account_private_key: rsa.RSAPrivateKey
    account_jwk: dict[str, str]
    order: AcmeOrder
    certificate: AcmeCertificate
    certificate_private_key: rsa.RSAPrivateKey
    certificate_der: bytes
    certificate_jwk: dict[str, str]


class _AuthorizationContext(TypedDict):
    """authorization deactivation 测试上下文数据结构。"""

    account: AcmeAccount
    account_private_key: rsa.RSAPrivateKey
    order: AcmeOrder
    authorization: AcmeAuthorization


# ================== 基础ACME协议功能测试 ==================


class TestACMEDirectory:
    """测试 ACME 目录服务"""

    def test_get_directory(self) -> None:
        """测试获取 ACME 目录"""
        with TestClient(app) as client:
            response = client.get("/acps-atr-v2/acme/directory")

            assert response.status_code == 200
            data = response.json()

            assert "newNonce" in data
            assert "newAccount" in data
            assert "newOrder" in data
            assert "revokeCert" in data
            assert "keyChange" in data
            assert "meta" in data

            # 检查 meta 信息
            meta = data["meta"]
            assert "externalAccountRequired" in meta


class TestACMENonce:
    """测试 ACME nonce 服务"""

    def test_get_new_nonce_head(self) -> None:
        """测试 HEAD 方法获取 nonce"""
        with TestClient(app) as client:
            response = client.head("/acps-atr-v2/acme/new-nonce")

            assert response.status_code == 200
            assert "Replay-Nonce" in response.headers
            assert len(response.headers["Replay-Nonce"]) > 0
            assert "Cache-Control" in response.headers
            assert response.headers["Cache-Control"] == "no-store"

    def test_get_new_nonce_get(self) -> None:
        """测试 GET 方法获取 nonce"""
        with TestClient(app) as client:
            response = client.get("/acps-atr-v2/acme/new-nonce")

            assert response.status_code == 200
            assert "Replay-Nonce" in response.headers
            assert len(response.headers["Replay-Nonce"]) > 0
            assert "Cache-Control" in response.headers
            assert response.headers["Cache-Control"] == "no-store"


class TestACMEAccount:
    """测试 ACME 账户服务"""

    def test_create_account_missing_payload(self) -> None:
        """测试创建账户时缺少 payload"""
        with TestClient(app) as client:
            # 无效的 JWS 请求
            jws_data = {"protected": "", "payload": "", "signature": ""}

            response = client.post("/acps-atr-v2/acme/new-account", json=jws_data)

            # 应该返回错误，因为这不是有效的 JWS 格式
            assert response.status_code in [400, 422]


class TestACMEBasicFlow:
    """测试基本的 ACME 流程"""

    def test_directory_and_nonce_flow(self) -> None:
        """测试获取目录和 nonce 的基本流程"""
        with TestClient(app) as client:
            # 1. 获取目录
            dir_response = client.get("/acps-atr-v2/acme/directory")
            assert dir_response.status_code == 200
            directory = dir_response.json()

            # 2. 获取 nonce
            nonce_response = client.get("/acps-atr-v2/acme/new-nonce")
            assert nonce_response.status_code == 200
            assert "Replay-Nonce" in nonce_response.headers

            # 3. 验证目录中的 URL 结构
            # 检查URL包含正确的端点名称
            assert "new-nonce" in directory["newNonce"]
            assert "new-account" in directory["newAccount"]
            assert "new-order" in directory["newOrder"]

    def test_health_check(self) -> None:
        """测试健康检查端点"""
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert data["status"] == "healthy"


def _build_rsa_jwk(public_key: rsa.RSAPublicKey) -> dict[str, str]:
    """将 RSA 公钥编码为 JWK。"""
    numbers = public_key.public_numbers()
    modulus_length = (numbers.n.bit_length() + 7) // 8
    exponent_length = (numbers.e.bit_length() + 7) // 8

    return {
        "kty": "RSA",
        "n": base64.urlsafe_b64encode(numbers.n.to_bytes(modulus_length, "big")).decode("ascii").rstrip("="),
        "e": base64.urlsafe_b64encode(numbers.e.to_bytes(exponent_length, "big")).decode("ascii").rstrip("="),
    }


def _build_test_jws_request(
    private_key: rsa.RSAPrivateKey,
    protected: Mapping[str, object],
    payload: Mapping[str, object] | None,
) -> dict[str, str]:
    """构造测试用 JWS 请求。"""
    protected_json = json.dumps(protected, separators=(",", ":")).encode("utf-8")

    protected_b64 = base64.urlsafe_b64encode(protected_json).decode("ascii").rstrip("=")
    if payload is None:
        payload_b64 = ""
    else:
        payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")

    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    return {
        "protected": protected_b64,
        "payload": payload_b64,
        "signature": signature_b64,
    }


def _acme_url(path_suffix: str) -> str:
    """构造测试环境中的 ACME 外部 URL。"""
    normalized_suffix = path_suffix if path_suffix.startswith("/") else f"/{path_suffix}"
    return f"{get_settings().acme_directory_url.rstrip('/')}{normalized_suffix}"


def _create_test_certificate() -> tuple[rsa.RSAPrivateKey, bytes, str, str]:
    """创建 revoke-cert 测试用证书。"""
    certificate_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "revoke-test-agent"),
            x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "ACPs Test"),
        ]
    )
    now = beijing_now()

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(certificate_private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(certificate_private_key, hashes.SHA256())
    )

    cert_der = certificate.public_bytes(serialization.Encoding.DER)
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    serial_number = f"{certificate.serial_number:X}"

    return certificate_private_key, cert_der, cert_pem, serial_number


async def _create_revoke_test_context(async_db_session) -> _RevokeContext:
    """创建 revoke-cert 测试所需的账户、订单和证书数据。"""
    account_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    account_jwk = _build_rsa_jwk(account_private_key.public_key())
    account_key_id = JWSVerifier().compute_jwk_thumbprint(account_jwk)
    suffix = uuid4().hex

    account = AcmeAccount(
        key_id=account_key_id,
        public_key=json.dumps(account_jwk),
        contact=[f"mailto:{suffix[:8]}@example.com"],
        terms_of_service_agreed=True,
        aic=f"AGENT{suffix[:28].upper()}",
    )
    async_db_session.add(account)
    await async_db_session.flush()
    assert account.id is not None

    order = AcmeOrder(
        order_id=f"order-{suffix}",
        account_id=account.id,
        status=OrderStatus.VALID,
        identifiers=[{"type": "agent", "value": account.aic or "UNKNOWN"}],
        authorizations=[],
        finalize=_acme_url(f"/finalize/{suffix}"),
    )
    async_db_session.add(order)
    await async_db_session.flush()
    assert order.id is not None

    certificate_private_key, cert_der, cert_pem, serial_number = _create_test_certificate()
    issued_at = beijing_now()

    certificate = AcmeCertificate(
        cert_id=f"cert-{suffix}",
        order_id=order.id,
        serial_number=serial_number,
        certificate_pem=cert_pem,
        status=CertificateStatus.VALID,
        subject={"CN": "revoke-test-agent", "O": "ACPs Test"},
        not_before=issued_at - timedelta(minutes=1),
        not_after=issued_at + timedelta(days=30),
        aic=account.aic,
    )
    async_db_session.add(certificate)
    await async_db_session.commit()

    return {
        "account": account,
        "account_private_key": account_private_key,
        "account_jwk": account_jwk,
        "order": order,
        "certificate": certificate,
        "certificate_private_key": certificate_private_key,
        "certificate_der": cert_der,
        "certificate_jwk": _build_rsa_jwk(certificate_private_key.public_key()),
    }


async def _insert_test_nonce(async_db_session, nonce: str) -> None:
    """为 ACME 请求插入一次性 nonce。"""
    async_db_session.add(AcmeNonce(nonce=nonce))
    await async_db_session.commit()


async def _cleanup_revoke_test_context(
    async_db_session, certificate_id: int | None, order_id: int | None, account_ids: list[int | None]
) -> None:
    """清理 revoke-cert 测试生成的 ACME 数据。"""
    account_id_column = cast("Any", AcmeAccount.id)
    nonce_column = cast("Any", AcmeNonce.nonce)

    certificate_id_column = cast("Any", AcmeCertificate.id)
    order_id_column = cast("Any", AcmeOrder.id)

    await async_db_session.execute(delete(AcmeCertificate).where(certificate_id_column == certificate_id))
    await async_db_session.execute(delete(AcmeOrder).where(order_id_column == order_id))
    await async_db_session.execute(delete(AcmeAccount).where(account_id_column.in_(account_ids)))
    await async_db_session.execute(delete(AcmeNonce).where(nonce_column.like("test-revoke-%")))
    await async_db_session.commit()


async def _create_authorization_test_context(async_db_session) -> _AuthorizationContext:
    """创建 authorization deactivation 测试所需数据。"""
    account_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    account_jwk = _build_rsa_jwk(account_private_key.public_key())
    account_key_id = JWSVerifier().compute_jwk_thumbprint(account_jwk)
    suffix = uuid4().hex

    account = AcmeAccount(
        key_id=account_key_id,
        public_key=json.dumps(account_jwk),
        contact=[f"mailto:{suffix[:8]}@example.com"],
        terms_of_service_agreed=True,
        aic=f"AGENT{suffix[:28].upper()}",
    )
    async_db_session.add(account)
    await async_db_session.flush()
    assert account.id is not None

    order = AcmeOrder(
        order_id=f"authz-order-{suffix}",
        account_id=account.id,
        status=OrderStatus.READY,
        identifiers=[{"type": "agent", "value": account.aic or "UNKNOWN"}],
        authorizations=[],
        finalize=_acme_url(f"/finalize/{suffix}"),
    )
    async_db_session.add(order)
    await async_db_session.flush()
    assert order.id is not None

    authorization = AcmeAuthorization(
        authz_id=f"authz-{suffix}",
        order_id=order.id,
        identifier={"type": "agent", "value": account.aic or "UNKNOWN"},
        status=AuthorizationStatus.VALID,
        expires=beijing_now() + timedelta(days=1),
    )
    async_db_session.add(authorization)
    await async_db_session.commit()

    return {
        "account": account,
        "account_private_key": account_private_key,
        "order": order,
        "authorization": authorization,
    }


async def _cleanup_authorization_test_context(
    async_db_session,
    authorization_id: int | None,
    order_id: int | None,
    account_ids: list[int | None],
) -> None:
    """清理 authorization deactivation 测试生成的数据。"""
    account_id_column = cast("Any", AcmeAccount.id)
    nonce_column = cast("Any", AcmeNonce.nonce)

    authorization_id_column = cast("Any", AcmeAuthorization.id)
    cleanup_order_id_column = cast("Any", AcmeOrder.id)

    await async_db_session.execute(delete(AcmeAuthorization).where(authorization_id_column == authorization_id))
    await async_db_session.execute(delete(AcmeOrder).where(cleanup_order_id_column == order_id))
    await async_db_session.execute(delete(AcmeAccount).where(account_id_column.in_(account_ids)))
    await async_db_session.execute(delete(AcmeNonce).where(nonce_column.like("test-revoke-%")))
    await async_db_session.commit()


class TestACMERevokeCertificate:
    """测试 ACME revoke-cert 的双路径认证模型。"""

    async def test_revoke_cert_accepts_account_key(self, client, async_db_session) -> None:
        """测试账户私钥可以撤销所属证书。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            request_payload = {
                "certificate": base64.urlsafe_b64encode(context["certificate_der"]).decode("ascii").rstrip("="),
                "reason": 1,
            }
            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url("/revoke-cert"),
            }

            response = client.post(
                "/acps-atr-v2/acme/revoke-cert",
                json=_build_test_jws_request(context["account_private_key"], protected, request_payload),
            )

            assert response.status_code == 200

            await async_db_session.refresh(certificate)
            assert certificate.status == CertificateStatus.REVOKED
            assert certificate.revocation_reason == RevocationReason.KEY_COMPROMISE
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_revoke_cert_accepts_certificate_key(self, client, async_db_session) -> None:
        """测试证书私钥可以直接撤销该证书。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            request_payload = {
                "certificate": base64.urlsafe_b64encode(context["certificate_der"]).decode("ascii").rstrip("="),
                "reason": 1,
            }
            protected = {
                "alg": "RS256",
                "jwk": context["certificate_jwk"],
                "nonce": nonce,
                "url": _acme_url("/revoke-cert"),
            }

            response = client.post(
                "/acps-atr-v2/acme/revoke-cert",
                json=_build_test_jws_request(context["certificate_private_key"], protected, request_payload),
            )

            assert response.status_code == 200

            await async_db_session.refresh(certificate)
            assert certificate.status == CertificateStatus.REVOKED
            assert certificate.revocation_reason == RevocationReason.KEY_COMPROMISE
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_revoke_cert_rejects_other_account_key(self, client, async_db_session) -> None:
        """测试无关账户不能撤销他人证书。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        unauthorized_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        unauthorized_jwk = _build_rsa_jwk(unauthorized_private_key.public_key())
        unauthorized_key_id = JWSVerifier().compute_jwk_thumbprint(unauthorized_jwk)
        unauthorized_account = AcmeAccount(
            key_id=unauthorized_key_id,
            public_key=json.dumps(unauthorized_jwk),
            contact=[f"mailto:unauth-{uuid4().hex[:8]}@example.com"],
            terms_of_service_agreed=True,
            aic=f"AGENT{uuid4().hex[:28].upper()}",
        )
        async_db_session.add(unauthorized_account)
        await async_db_session.commit()
        await async_db_session.refresh(unauthorized_account)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            request_payload = {
                "certificate": base64.urlsafe_b64encode(context["certificate_der"]).decode("ascii").rstrip("="),
                "reason": 1,
            }
            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{unauthorized_account.id}"),
                "nonce": nonce,
                "url": _acme_url("/revoke-cert"),
            }

            response = client.post(
                "/acps-atr-v2/acme/revoke-cert",
                json=_build_test_jws_request(unauthorized_private_key, protected, request_payload),
            )

            assert response.status_code == 403

            await async_db_session.refresh(certificate)
            assert certificate.status == CertificateStatus.VALID
            assert certificate.revocation_reason is None
        finally:
            await _cleanup_revoke_test_context(
                async_db_session,
                certificate.id,
                order.id,
                [account.id, unauthorized_account.id],
            )


class TestACMEDeactivatedAccount:
    """测试 deactivated 账户的后续请求拒绝语义。"""

    async def test_deactivated_account_cannot_access_order(self, client, async_db_session) -> None:
        """测试已停用账户不能继续查询订单。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        account.status = AccountStatus.DEACTIVATED
        async_db_session.add(account)
        await async_db_session.commit()

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/order/{order.order_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/order/{order.order_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 403
            assert "deactivated" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_deactivated_account_cannot_access_certificate(self, client, async_db_session) -> None:
        """测试已停用账户不能继续获取证书资源。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        account.status = AccountStatus.DEACTIVATED
        async_db_session.add(account)
        await async_db_session.commit()

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/cert/{certificate.cert_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/cert/{certificate.cert_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 403
            assert "deactivated" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_deactivated_account_cannot_access_account_resource(self, client, async_db_session) -> None:
        """测试已停用账户不能继续查询自身账户资源。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        account.status = AccountStatus.DEACTIVATED
        async_db_session.add(account)
        await async_db_session.commit()

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/acct/{account.id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/acct/{account.id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 403
            assert "deactivated" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_only_return_existing_still_returns_deactivated_account(self, client, async_db_session) -> None:
        """测试 onlyReturnExisting 查询已有账户时不受停用拦截影响。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        account.status = AccountStatus.DEACTIVATED
        async_db_session.add(account)
        await async_db_session.commit()

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "jwk": context["account_jwk"],
                "nonce": nonce,
                "url": _acme_url("/new-account"),
            }

            response = client.post(
                "/acps-atr-v2/acme/new-account",
                json=_build_test_jws_request(
                    context["account_private_key"],
                    protected,
                    {"onlyReturnExisting": True},
                ),
            )

            assert response.status_code in {200, 201}
            assert response.json()["status"] == AccountStatus.DEACTIVATED
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_order_query_rejects_forged_signature(self, client, async_db_session) -> None:
        """测试订单查询不能只凭 kid 绕过签名校验。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        attacker_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/order/{order.order_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/order/{order.order_id}",
                json=_build_test_jws_request(attacker_private_key, protected, None),
            )

            assert response.status_code == 400
            assert "bad_signature" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_account_update_rejects_forged_signature(self, client, async_db_session) -> None:
        """测试账户更新不能只凭 kid 绕过签名校验。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        attacker_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        original_contact = list(account.contact or [])

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/acct/{account.id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/acct/{account.id}",
                json=_build_test_jws_request(
                    attacker_private_key,
                    protected,
                    {"contact": [f"mailto:forged-{uuid4().hex[:8]}@example.com"]},
                ),
            )

            assert response.status_code == 400
            assert "bad_signature" in response.text.lower()

            await async_db_session.refresh(account)
            assert account.contact == original_contact
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])


class TestACMEAuthorizationDeactivation:
    """兼容层保留测试：authorization deactivation 协议语义仍按 RFC 8555 只读保留。"""

    async def test_authorization_owner_can_deactivate_authorization(self, client, async_db_session) -> None:
        """测试 authorization 所属账户可以停用该授权。"""
        context = await _create_authorization_test_context(async_db_session)
        account = context["account"]
        order = context["order"]
        authorization = context["authorization"]

        assert isinstance(account, AcmeAccount)
        assert isinstance(order, AcmeOrder)
        assert isinstance(authorization, AcmeAuthorization)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/authz/{authorization.authz_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/authz/{authorization.authz_id}",
                json=_build_test_jws_request(
                    context["account_private_key"],
                    protected,
                    {"status": "deactivated"},
                ),
            )

            assert response.status_code == 200
            assert response.json()["status"] == AuthorizationStatus.DEACTIVATED

            await async_db_session.refresh(authorization)
            await async_db_session.refresh(order)
            assert authorization.status == AuthorizationStatus.DEACTIVATED
            assert order.status == OrderStatus.INVALID
        finally:
            await _cleanup_authorization_test_context(async_db_session, authorization.id, order.id, [account.id])

    async def test_authorization_deactivation_rejects_other_account(self, client, async_db_session) -> None:
        """测试无关账户不能停用他人的 authorization。"""
        context = await _create_authorization_test_context(async_db_session)
        account = context["account"]
        order = context["order"]
        authorization = context["authorization"]

        assert isinstance(account, AcmeAccount)
        assert isinstance(order, AcmeOrder)
        assert isinstance(authorization, AcmeAuthorization)

        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_jwk = _build_rsa_jwk(other_private_key.public_key())
        other_account = AcmeAccount(
            key_id=JWSVerifier().compute_jwk_thumbprint(other_jwk),
            public_key=json.dumps(other_jwk),
            contact=[f"mailto:other-{uuid4().hex[:8]}@example.com"],
            terms_of_service_agreed=True,
            aic=f"AGENT{uuid4().hex[:28].upper()}",
        )
        async_db_session.add(other_account)
        await async_db_session.commit()
        await async_db_session.refresh(other_account)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{other_account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/authz/{authorization.authz_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/authz/{authorization.authz_id}",
                json=_build_test_jws_request(other_private_key, protected, {"status": "deactivated"}),
            )

            assert response.status_code == 403

            await async_db_session.refresh(authorization)
            await async_db_session.refresh(order)
            assert authorization.status == AuthorizationStatus.VALID
            assert order.status == OrderStatus.READY
        finally:
            await _cleanup_authorization_test_context(
                async_db_session,
                authorization.id,
                order.id,
                [account.id, other_account.id],
            )

    async def test_authorization_deactivation_is_idempotent(self, client, async_db_session) -> None:
        """测试重复停用 authorization 返回稳定结果。"""
        context = await _create_authorization_test_context(async_db_session)
        account = context["account"]
        order = context["order"]
        authorization = context["authorization"]

        assert isinstance(account, AcmeAccount)
        assert isinstance(order, AcmeOrder)
        assert isinstance(authorization, AcmeAuthorization)

        try:
            first_nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, first_nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": first_nonce,
                "url": _acme_url(f"/authz/{authorization.authz_id}"),
            }

            first_response = client.post(
                f"/acps-atr-v2/acme/authz/{authorization.authz_id}",
                json=_build_test_jws_request(
                    context["account_private_key"],
                    protected,
                    {"status": "deactivated"},
                ),
            )

            assert first_response.status_code == 200

            second_nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, second_nonce)
            protected["nonce"] = second_nonce

            second_response = client.post(
                f"/acps-atr-v2/acme/authz/{authorization.authz_id}",
                json=_build_test_jws_request(
                    context["account_private_key"],
                    protected,
                    {"status": "deactivated"},
                ),
            )

            assert second_response.status_code == 200
            assert second_response.json()["status"] == AuthorizationStatus.DEACTIVATED

            await async_db_session.refresh(authorization)
            await async_db_session.refresh(order)
            assert authorization.status == AuthorizationStatus.DEACTIVATED
            assert order.status == OrderStatus.INVALID
        finally:
            await _cleanup_authorization_test_context(async_db_session, authorization.id, order.id, [account.id])


class TestACMEProtectedHeaderURL:
    """测试 ACME protected header 中的 URL 完整性校验。"""

    async def test_revoke_cert_rejects_url_mismatch(self, client, async_db_session) -> None:
        """测试 revoke-cert 请求在 protected url 不匹配时被拒绝。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url("/wrong-revoke-path"),
            }
            payload = {
                "certificate": base64.urlsafe_b64encode(context["certificate_der"]).decode("ascii").rstrip("="),
                "reason": 1,
            }

            response = client.post(
                "/acps-atr-v2/acme/revoke-cert",
                json=_build_test_jws_request(context["account_private_key"], protected, payload),
            )

            assert response.status_code == 400
            assert "url mismatch" in response.text.lower()

            await async_db_session.refresh(certificate)
            assert certificate.status == CertificateStatus.VALID
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_order_query_rejects_url_mismatch(self, client, async_db_session) -> None:
        """测试订单查询在 protected url 不匹配时被拒绝。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url("/order/not-the-real-order"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/order/{order.order_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 400
            assert "url mismatch" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_account_query_rejects_url_mismatch(self, client, async_db_session) -> None:
        """测试账户查询在 protected url 不匹配时被拒绝。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url("/acct/not-the-real-account"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/acct/{account.id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 400
            assert "url mismatch" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])


class TestACMEPostAsGetPayload:
    """测试查询类 ACME 端点的 POST-as-GET 空 payload 约束。"""

    async def test_account_query_accepts_empty_payload(self, client, async_db_session) -> None:
        """测试账户查询接受真正的空 payload。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/acct/{account.id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/acct/{account.id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 200
            assert response.json()["status"] == account.status
            assert response.json()["contact"] == account.contact
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_account_query_rejects_json_object_payload(self, client, async_db_session) -> None:
        """测试账户查询拒绝 JSON 对象形式的伪 POST-as-GET。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/acct/{account.id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/acct/{account.id}",
                json=_build_test_jws_request(context["account_private_key"], protected, {}),
            )

            assert response.status_code == 400
            assert "empty payload" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_order_query_accepts_empty_payload(self, client, async_db_session) -> None:
        """测试订单查询接受真正的空 payload。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/order/{order.order_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/order/{order.order_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 200
            assert response.json()["status"] == OrderStatus.VALID
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_order_query_rejects_json_object_payload(self, client, async_db_session) -> None:
        """测试订单查询拒绝 JSON 对象形式的伪 POST-as-GET。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/order/{order.order_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/order/{order.order_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, {}),
            )

            assert response.status_code == 400
            assert "empty payload" in response.text.lower()
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])

    async def test_authorization_query_accepts_empty_payload(self, client, async_db_session) -> None:
        """兼容层保留测试：authorization 查询继续接受真正的空 payload。"""
        context = await _create_authorization_test_context(async_db_session)
        account = context["account"]
        order = context["order"]
        authorization = context["authorization"]

        assert isinstance(account, AcmeAccount)
        assert isinstance(order, AcmeOrder)
        assert isinstance(authorization, AcmeAuthorization)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/authz/{authorization.authz_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/authz/{authorization.authz_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 200
            assert response.json()["status"] == AuthorizationStatus.VALID
        finally:
            await _cleanup_authorization_test_context(async_db_session, authorization.id, order.id, [account.id])

    async def test_certificate_query_accepts_empty_payload(self, client, async_db_session) -> None:
        """测试证书查询接受真正的空 payload。"""
        context = await _create_revoke_test_context(async_db_session)
        certificate = context["certificate"]
        order = context["order"]
        account = context["account"]

        assert isinstance(certificate, AcmeCertificate)
        assert isinstance(order, AcmeOrder)
        assert isinstance(account, AcmeAccount)

        try:
            nonce = f"test-revoke-{uuid4().hex}"
            await _insert_test_nonce(async_db_session, nonce)

            protected = {
                "alg": "RS256",
                "kid": _acme_url(f"/acct/{account.id}"),
                "nonce": nonce,
                "url": _acme_url(f"/cert/{certificate.cert_id}"),
            }

            response = client.post(
                f"/acps-atr-v2/acme/cert/{certificate.cert_id}",
                json=_build_test_jws_request(context["account_private_key"], protected, None),
            )

            assert response.status_code == 200
            assert "BEGIN CERTIFICATE" in response.text
        finally:
            await _cleanup_revoke_test_context(async_db_session, certificate.id, order.id, [account.id])


# ================== JWS签名验证测试 ==================


class TestJWSVerification:
    """测试JWS签名验证功能"""

    def setup_method(self):
        """设置测试"""
        self.jws_verifier = JWSVerifier()

        # 生成测试RSA密钥对
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()

        # 创建JWK
        public_numbers = self.public_key.public_numbers()
        n = public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
        e = public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")

        self.jwk = {
            "kty": "RSA",
            "n": base64.urlsafe_b64encode(n).decode("ascii").rstrip("="),
            "e": base64.urlsafe_b64encode(e).decode("ascii").rstrip("="),
        }

    def test_base64url_decode(self) -> None:
        """测试base64url解码"""
        data = "SGVsbG8gV29ybGQ"
        decoded = self.jws_verifier.base64url_decode(data)
        assert decoded == b"Hello World"

    def test_base64url_encode(self) -> None:
        """测试base64url编码"""
        data = b"Hello World"
        encoded = self.jws_verifier.base64url_encode(data)
        assert encoded == "SGVsbG8gV29ybGQ"

    def test_jwk_to_public_key(self) -> None:
        """测试JWK转换为公钥"""
        converted_key = self.jws_verifier._jwk_to_public_key(self.jwk)

        # 验证转换的公钥与原始公钥匹配
        assert isinstance(converted_key, rsa.RSAPublicKey)
        assert converted_key.public_numbers().n == self.public_key.public_numbers().n
        assert converted_key.public_numbers().e == self.public_key.public_numbers().e

    def test_jwk_thumbprint(self) -> None:
        """测试JWK指纹计算"""
        thumbprint = self.jws_verifier.compute_jwk_thumbprint(self.jwk)

        # 指纹应该是base64url编码的字符串
        assert isinstance(thumbprint, str)
        assert len(thumbprint) > 0

        # 同样的JWK应该产生相同的指纹
        thumbprint2 = self.jws_verifier.compute_jwk_thumbprint(self.jwk)
        assert thumbprint == thumbprint2

    def create_test_jws(self, payload_dict: dict, protected_dict: dict) -> str:
        """创建测试JWS"""
        # 编码protected header
        protected_json = json.dumps(protected_dict, separators=(",", ":"))
        protected_b64 = base64.urlsafe_b64encode(protected_json.encode()).decode().rstrip("=")

        # 编码payload
        payload_json = json.dumps(payload_dict, separators=(",", ":"))
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")

        # 创建签名
        signing_input = f"{protected_b64}.{payload_b64}".encode()
        signature = self.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        signature_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")

        return f"{protected_b64}.{payload_b64}.{signature_b64}"

    def test_valid_jws_verification(self) -> None:
        """测试有效JWS验证"""
        protected = {
            "alg": "RS256",
            "jwk": self.jwk,
            "nonce": "test-nonce",
            "url": "https://example.com/test",
        }
        payload = {"test": "data"}

        jws = self.create_test_jws(payload, protected)

        # 验证JWS
        result = self.jws_verifier.verify_jws_signature(jws, self.jwk, "test-nonce", "https://example.com/test")

        assert result == payload

    def test_valid_ec_jws_verification(self) -> None:
        """测试有效的 EC JWS 签名验证"""
        # 生成 EC 密钥对
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        public_key = private_key.public_key()

        # 获取公钥参数
        numbers = public_key.public_numbers()
        x = numbers.x
        y = numbers.y

        # 构建 JWK
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": self.jws_verifier.base64url_encode(x.to_bytes(32, byteorder="big")),
            "y": self.jws_verifier.base64url_encode(y.to_bytes(32, byteorder="big")),
        }

        # 构建 payload
        payload = {"test": "ec_data"}
        payload_json = json.dumps(payload)
        payload_b64 = self.jws_verifier.base64url_encode(payload_json.encode("utf-8"))

        # 构建 protected header
        protected = {
            "alg": "ES256",
            "jwk": jwk,
            "nonce": "test-ec-nonce",
            "url": "https://example.com/ec-test",
        }
        protected_json = json.dumps(protected)
        protected_b64 = self.jws_verifier.base64url_encode(protected_json.encode("utf-8"))

        # 签名
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))

        # 将 DER 签名转换为 Raw (R||S) 格式，符合 JWS 规范
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        r, s = decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, byteorder="big") + s.to_bytes(32, byteorder="big")

        signature_b64 = self.jws_verifier.base64url_encode(raw_signature)

        # 构建 JWS
        jws_data = f"{protected_b64}.{payload_b64}.{signature_b64}"

        # 验证
        result = self.jws_verifier.verify_jws_signature(
            jws_data,
            jwk,
            expected_nonce="test-ec-nonce",
            expected_url="https://example.com/ec-test",
        )

        assert result == payload


# ================== Agent注册服务测试 ==================


class TestRegistryClient:
    """测试Agent注册服务客户端"""

    def setup_method(self):
        """设置测试"""
        with patch("app.acme.registry_client.get_settings") as mock_settings:
            mock_settings.return_value.registry_server_url = "http://test-registry"
            mock_settings.return_value.registry_server_timeout = 10
            mock_settings.return_value.registry_server_internal_api_token = "test-token"
            mock_settings.return_value.external_service_max_retries = 3
            mock_settings.return_value.external_service_retry_delays_list = [1, 2, 4]
            # 确保Mock模式被禁用
            mock_settings.return_value.registry_server_mock = False

            self.client = RegistryClient()

    def test_agent_info_creation(self) -> None:
        """测试 AgentInfo 数据类 - 使用 ACS 格式"""
        # 测试 ACS 数据结构
        test_data_acs = {
            "aic": "AGENT001TEST2024XYZ123456ABCDEF78",
            "active": True,
            "name": "Test Agent Service",
            "version": "1.0.0",
            "provider": {
                "organization": "Test Corp",
                "department": "AI Services",
                "countryCode": "US",
            },
            "securitySchemes": {
                "mtls": {
                    "description": "智能体间mTLS双向认证",
                    "type": "mutualTLS",
                }
            },
            "endPoints": [
                {
                    "url": "https://agent.example.com/acps-aip-v2/rpc",
                    "security": [{"mtls": []}],
                    "transport": "JSONRPC",
                }
            ],
            "capabilities": {"communication": ["jsonrpc"]},
            "skills": [],
        }

        agent_info = AgentInfo(test_data_acs)

        assert agent_info.aic == "AGENT001TEST2024XYZ123456ABCDEF78"
        assert agent_info.is_valid() is True
        assert agent_info.name == "Test Agent Service"
        assert agent_info.organization == "Test Corp"
        assert agent_info.department == "AI Services"
        assert agent_info.country_code == "US"

        # 测试证书Subject DN组件
        components = agent_info.get_certificate_subject_components()
        expected = {
            "CN": "AGENT001TEST2024XYZ123456ABCDEF78",
            "O": "Test Corp",
            "OU": "AI Services",
            "C": "US",
        }
        assert components == expected

        # 测试另一个 ACS 格式的数据
        test_data_2 = {
            "aic": "AGENT002OLD2024LEGACY987654321ABC",
            "active": True,
            "name": "Legacy Agent Service",
            "version": "2.0.0",
            "provider": {
                "organization": "Old Corp",
                "department": "Legacy Services",
                "countryCode": "CA",
            },
            "securitySchemes": {
                "mtls": {
                    "description": "智能体间mTLS双向认证",
                    "type": "mutualTLS",
                }
            },
            "endPoints": [
                {
                    "url": "https://old-agent.example.com/acps-aip-v2/rpc",
                    "security": [{"mtls": []}],
                    "transport": "JSONRPC",
                }
            ],
            "capabilities": {},
            "skills": [],
        }

        agent_info_2 = AgentInfo(test_data_2)
        assert agent_info_2.aic == "AGENT002OLD2024LEGACY987654321ABC"
        assert agent_info_2.organization == "Old Corp"

    async def test_validate_aic_success(self) -> None:
        """测试成功的AIC验证"""
        test_response_data = {
            "aic": "AGENTTEST123SUCCESS4567890ABCDEF12",
            "active": True,
            "name": "Test Agent Service",
            "version": "1.0.0",
            "provider": {
                "organization": "Test Corp",
                "department": "AI Services",
                "countryCode": "US",
            },
            "securitySchemes": {
                "mtls": {
                    "description": "智能体间mTLS双向认证",
                    "type": "mutualTLS",
                }
            },
            "endPoints": [
                {
                    "url": "https://agent.example.com/acps-aip-v2/rpc",
                    "security": [{"mtls": []}],
                    "transport": "JSONRPC",
                }
            ],
            "capabilities": {"communication": ["jsonrpc"]},
            "skills": [],
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = test_response_data

            mock_request = mock_client.return_value.__aenter__.return_value.request
            mock_request.return_value = mock_response

            result = await self.client.validate_aic_and_get_info("AGENTTEST123SUCCESS4567890ABCDEF12")

            assert result is not None
            assert result.aic == "AGENTTEST123SUCCESS4567890ABCDEF12"
            assert result.is_valid() is True

            # 验证请求 URL 是否包含 /acs/ 路径
            mock_request.assert_called()
            args, _ = mock_request.call_args
            assert args[0] == "GET"
            assert args[1] == "http://test-registry/acs/AGENTTEST123SUCCESS4567890ABCDEF12"

    async def test_validate_aic_not_found(self) -> None:
        """测试AIC不存在的情况"""
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 404

            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=mock_response)

            result = await self.client.validate_aic_and_get_info("nonexistent-agent")

            assert result is None


# ================== 证书签发功能测试 ==================


class TestCertificateIssuing:
    """测试证书签发功能"""

    def setup_method(self):
        """设置测试"""
        from app.acme.service import CertificateService
        from app.core.ca_manager import CAManager

        self.ca_manager = CAManager()

        # 模拟数据库会话
        self.mock_session = Mock()
        self.cert_service = CertificateService(self.mock_session)

    def create_test_csr(self, key_size=2048, use_ec=False):
        """创建测试CSR"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from cryptography.x509.oid import NameOID

        if use_ec:
            # 使用 ECDSA P-256
            private_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey = ec.generate_private_key(ec.SECP256R1())
        else:
            # 使用 RSA
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "agent-001"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
            ]
        )

        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

        return csr, private_key

    def test_validate_csr_rsa_2048_allowed(self) -> None:
        """测试RSA 2048位密钥被允许"""
        csr, _ = self.create_test_csr(key_size=2048)

        # 应该不抛出异常
        try:
            self.ca_manager._validate_csr_public_key(csr)
        except Exception as e:
            pytest.fail(f"RSA 2048 should be allowed, but got error: {e}")

    def test_validate_csr_rsa_1024_rejected(self) -> None:
        """测试RSA 1024位密钥被拒绝"""
        csr, _ = self.create_test_csr(key_size=1024)

        with pytest.raises(ValueError, match="RSA key size 1024 is too small"):
            self.ca_manager._validate_csr_public_key(csr)

    def test_validate_csr_ecdsa_p256_allowed(self) -> None:
        """测试ECDSA P-256被允许"""
        csr, _ = self.create_test_csr(use_ec=True)

        # 应该不抛出异常
        try:
            self.ca_manager._validate_csr_public_key(csr)
        except Exception as e:
            pytest.fail(f"ECDSA P-256 should be allowed, but got error: {e}")

    def test_validate_csr_ed25519_allowed(self) -> None:
        """Ed25519 CSR 应通过校验（RFC 8410 白名单）。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.x509.oid import NameOID

        private_key = Ed25519PrivateKey.generate()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-ed25519")])
        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, None)

        try:
            self.ca_manager._validate_csr_public_key(csr)
        except Exception as e:
            pytest.fail(f"Ed25519 should be allowed, but got error: {e}")

    def test_sign_certificate_ed25519_key_usage(self) -> None:
        """Ed25519 签发的证书：KeyUsage 只含 digitalSignature，不含 keyEncipherment（RFC 8410 §3）。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.x509.oid import NameOID

        private_key = Ed25519PrivateKey.generate()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TESTAGENT-ED25519")])
        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, None)

        cert_pem = self.ca_manager.sign_certificate(csr, ["TESTAGENT-ED25519"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())

        key_usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        assert key_usage.digital_signature is True, "Ed25519 证书必须有 digitalSignature"
        assert key_usage.key_encipherment is False, "Ed25519 证书不得有 keyEncipherment（RFC 8410 §3）"

    async def test_single_agent_certificate_generation(self) -> None:
        """测试单Agent证书生成"""
        import secrets

        from app.acme.model import AcmeOrder
        from app.acme.registry_client import AgentInfo

        # 创建模拟订单
        order = Mock(spec=AcmeOrder)
        order.id = 1
        order.identifiers = [{"type": "agent", "value": "agent-001"}]

        # 创建模拟Agent信息 - 使用当前 EAB 主链路的 ACS 数据结构
        agent_data = {
            "aic": "agent-001",
            "valid": True,
            "acs": {
                "name": "Test Agent",
                "provider": "Test Org",
                "organizationName": "Test Org",
                "country": "US",
                "status": "active",
            },
        }
        agent_info = AgentInfo(agent_data)

        # 创建CSR
        csr, _ = self.create_test_csr()
        csr_der = csr.public_bytes(serialization.Encoding.DER)

        # Mock数据库操作和证书生成，使用简化的主体信息
        mock_cert = Mock()
        mock_cert.cert_id = "cert_" + secrets.token_urlsafe(16)
        mock_cert.serial_number = secrets.token_hex(16)

        with (
            patch.object(self.cert_service, "_create_certificate", new=AsyncMock(return_value=mock_cert)),
            patch.object(
                self.cert_service,
                "_generate_certificate_for_agent",
                return_value="-----BEGIN CERTIFICATE-----\nMOCK_CERT\n-----END CERTIFICATE-----",
            ),
            patch.object(
                self.cert_service,
                "_extract_subject_from_cert_pem",
                return_value={
                    "CN": "agent-001.acps.pub",
                    "O": "Test Org",
                },
            ),
            patch.object(
                self.cert_service,
                "_extract_serial_number_from_cert_pem",
                return_value="1234567890ABCDEF",
            ),
        ):
            certificates = await self.cert_service.issue_certificate(order, csr_der, [agent_info])

            assert len(certificates) == 1
            assert certificates[0] == mock_cert

    async def test_multi_agent_certificate_generation(self) -> None:
        """测试多Agent证书生成（每个Agent一张证书）"""
        import secrets

        from app.acme.model import AcmeOrder
        from app.acme.registry_client import AgentInfo

        # 创建模拟订单，包含两个Agent
        order = Mock(spec=AcmeOrder)
        order.id = 1
        order.identifiers = [
            {"type": "agent", "value": "agent-001"},
            {"type": "agent", "value": "agent-002"},
        ]

        # 创建模拟Agent信息 - 使用当前 EAB 主链路的 ACS 数据结构
        agent_infos = [
            AgentInfo(
                {
                    "aic": "agent-001",
                    "valid": True,
                    "acs": {
                        "name": "Test Agent 1",
                        "provider": "Test Org 1",
                        "organizationName": "Test Org 1",
                        "country": "US",
                        "status": "active",
                    },
                }
            ),
            AgentInfo(
                {
                    "aic": "agent-002",
                    "valid": True,
                    "acs": {
                        "name": "Test Agent 2",
                        "provider": "Test Org 2",
                        "organizationName": "Test Org 2",
                        "country": "US",
                        "status": "active",
                    },
                }
            ),
        ]

        # 创建CSR
        csr, _ = self.create_test_csr()
        csr_der = csr.public_bytes(serialization.Encoding.DER)

        # Mock数据库操作
        mock_certs = [
            Mock(cert_id="cert_" + secrets.token_urlsafe(16)),
            Mock(cert_id="cert_" + secrets.token_urlsafe(16)),
        ]

        with (
            patch.object(self.cert_service, "_create_certificate", new=AsyncMock(side_effect=mock_certs)),
            patch.object(
                self.cert_service,
                "_generate_certificate_for_agent",
                return_value="-----BEGIN CERTIFICATE-----\nMOCK_CERT\n-----END CERTIFICATE-----",
            ),
            patch.object(
                self.cert_service,
                "_extract_subject_from_cert_pem",
                return_value={"CN": "agent.acps.pub", "O": "Test Org"},
            ),
            patch.object(
                self.cert_service,
                "_extract_serial_number_from_cert_pem",
                return_value="1234567890ABCDEF",
            ),
        ):
            certificates = await self.cert_service.issue_certificate(order, csr_der, agent_infos)

            # 应该为每个Agent签发一张证书
            assert len(certificates) == 2
            assert certificates[0] == mock_certs[0]
            assert certificates[1] == mock_certs[1]

    def test_certificate_subject_built_from_agent_info(self) -> None:
        """测试证书Subject DN根据Agent注册信息构造"""
        from app.acme.registry_client import AgentInfo

        agent_data = {
            "aic": "ABCD1234EFGH5678IJKL9012MNOP3456",
            "active": True,
            "name": "ACME Agent Service",
            "version": "1.0.0",
            "provider": {
                "organization": "ACME Corp",
                "department": "Engineering",
                "countryCode": "US",
            },
            "securitySchemes": {"mtls": {"type": "mutualTLS"}},
            "endPoints": [],
            "capabilities": [],
            "skills": [],
        }
        agent_info = AgentInfo(agent_data)

        subject_components = agent_info.get_certificate_subject_components()
        subject = self.ca_manager._build_certificate_subject("ABCD1234EFGH5678IJKL9012MNOP3456", subject_components)

        # 验证Subject DN包含Agent信息
        subject_dict = {attr.oid._name: attr.value for attr in subject}
        assert subject_dict.get("commonName") == "ABCD1234EFGH5678IJKL9012MNOP3456"
        assert subject_dict.get("organizationName") == "ACME Corp"
        assert subject_dict.get("organizationalUnitName") == "Engineering"
        assert subject_dict.get("countryName") == "US"


# ================== 证书策略测试 ==================


class TestCertificatePolicy:
    """测试证书策略"""

    def test_certificate_validity_period(self) -> None:
        """测试签发的证书有效期：默认 49 天，指定 validity_days 时以实际值为准"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        from app.core.ca_manager import CAManager

        ca_manager = CAManager()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TESTAGENT001")])
        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

        # 默认有效期应为 49 天
        cert_pem = ca_manager.sign_certificate(csr, ["TESTAGENT001"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert delta.days == 49

        # 指定 30 天有效期
        cert_pem_30 = ca_manager.sign_certificate(csr, ["TESTAGENT001"], validity_days=30)
        cert_30 = x509.load_pem_x509_certificate(cert_pem_30.encode())
        delta_30 = cert_30.not_valid_after_utc - cert_30.not_valid_before_utc
        assert delta_30.days == 30

    def test_certificate_includes_revocation_discovery_extensions(self) -> None:
        """测试签发证书包含 OCSP 与 CRL 发现扩展。"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

        from app.core.ca_manager import CAManager
        from app.core.config import get_settings

        ca_manager = CAManager()
        settings = get_settings()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TESTAGENT001")])
        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

        cert_pem = ca_manager.sign_certificate(csr, ["TESTAGENT001"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())

        authority_information_access = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        ocsp_urls = [
            description.access_location.value
            for description in authority_information_access
            if description.access_method == AuthorityInformationAccessOID.OCSP
        ]
        crl_distribution_points = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
        crl_urls = [
            name.value
            for distribution_point in crl_distribution_points
            for name in distribution_point.full_name or []
            if isinstance(name, x509.UniformResourceIdentifier)
        ]

        assert ocsp_urls == [settings.ocsp_responder_url]
        assert crl_urls == [settings.crl_distribution_point_url]


class TestCertificateDiscoveryConfig:
    """测试证书发现地址在生产环境下的配置约束。"""

    def test_production_discovery_urls_reject_placeholders(self) -> None:
        """测试 production 环境下占位发现地址会在配置加载时失败。"""
        with pytest.raises(ValidationError, match="externally reachable hostname"):
            Settings(APP_ENV="production")

    def test_production_discovery_urls_accept_explicit_hosts(self) -> None:
        """测试 production 环境下显式公网主机名可以通过校验。"""
        settings = Settings()
        settings.app_env = "production"
        settings._toml.setdefault("ca", {})["ocsp_responder_url"] = "https://ca.acps.example.org/acps-atr-v2/ocsp"
        settings._toml["ca"]["crl_distribution_point_url"] = "https://ca.acps.example.org/acps-atr-v2/crl/current"

        settings.validate_certificate_discovery_urls()

    def test_san_extension_includes_agent_info(self) -> None:
        """测试签发的证书 SAN 包含 acps:// URI、自定义 DNS 和 IP 条目（v2.1.0）"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        from app.core.ca_manager import CAManager

        ca_manager = CAManager()
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TESTAGENT001")])
        csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

        agent_id = "TESTAGENT001"
        cert_pem = ca_manager.sign_certificate(
            csr,
            [agent_id],
            dns_names=["agent.example.com"],
            ip_addresses=["192.168.1.1"],
        )

        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        assert f"acps://{agent_id}" in uris

        dns_names = san.get_values_for_type(x509.DNSName)
        assert "agent.example.com" in dns_names

        ip_addresses = san.get_values_for_type(x509.IPAddress)
        assert str(ip_addresses[0]) == "192.168.1.1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ================== AgentInfo v2.1.0 certificate 字段测试 ==================


class TestAgentInfoCertificateFields:
    """测试 AgentInfo v2.1.0 新增的 certificate 字段解析与方法"""

    def _make_base_data(self, **kwargs) -> dict:
        data = {
            "aic": "TESTAGENT12345678901234567890ABCD",
            "active": True,
            "name": "Test Agent",
            "version": "1.0.0",
            "provider": {"organization": "Test Org", "countryCode": "CN"},
        }
        data.update(kwargs)
        return data

    def test_certificate_fields_default_when_key_absent(self) -> None:
        """certificate 键缺失时各字段应为默认值"""
        agent_info = AgentInfo(self._make_base_data())
        assert agent_info.certificate_alt_names_dns == []
        assert agent_info.certificate_alt_names_ip == []
        assert agent_info.certificate_requested_validity is None

    def test_certificate_fields_default_when_key_is_none(self) -> None:
        """certificate 键为 None 时各字段应为默认值"""
        agent_info = AgentInfo(self._make_base_data(certificate=None))
        assert agent_info.certificate_alt_names_dns == []
        assert agent_info.certificate_alt_names_ip == []
        assert agent_info.certificate_requested_validity is None

    def test_certificate_dns_names_parsed(self) -> None:
        """certificate.altNames.dns 应正确解析"""
        data = self._make_base_data(
            certificate={
                "altNames": {"dns": ["agent.example.com", "alt.example.com"], "ip": []},
                "requestedValidity": None,
            }
        )
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_dns_names() == [
            "agent.example.com",
            "alt.example.com",
        ]

    def test_certificate_ip_addresses_parsed(self) -> None:
        """certificate.altNames.ip 应正确解析"""
        data = self._make_base_data(
            certificate={
                "altNames": {"dns": [], "ip": ["10.0.0.1", "192.168.1.100"]},
                "requestedValidity": None,
            }
        )
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_ip_addresses() == [
            "10.0.0.1",
            "192.168.1.100",
        ]

    def test_certificate_empty_alt_names(self) -> None:
        """certificate.altNames 为空时返回空列表"""
        data = self._make_base_data(certificate={"altNames": {"dns": [], "ip": []}, "requestedValidity": None})
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_dns_names() == []
        assert agent_info.get_certificate_ip_addresses() == []

    def test_get_certificate_validity_days_default(self) -> None:
        """未指定 requestedValidity 时默认 49 天"""
        agent_info = AgentInfo(self._make_base_data())
        assert agent_info.get_certificate_validity_days() == 49

    def test_get_certificate_validity_days_custom(self) -> None:
        """指定 requestedValidity=30 时返回 30"""
        data = self._make_base_data(certificate={"altNames": {"dns": [], "ip": []}, "requestedValidity": 30})
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_validity_days() == 30

    def test_get_certificate_validity_days_capped_by_max(self) -> None:
        """requestedValidity 超出 max_days 时截断到上限"""
        data = self._make_base_data(certificate={"altNames": {"dns": [], "ip": []}, "requestedValidity": 9999})
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_validity_days(max_days=365) == 365

    def test_get_certificate_validity_days_zero_falls_back(self) -> None:
        """requestedValidity=0 时回退到默认 49 天"""
        data = self._make_base_data(certificate={"altNames": {"dns": [], "ip": []}, "requestedValidity": 0})
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_validity_days() == 49

    def test_get_certificate_validity_days_negative_falls_back(self) -> None:
        """requestedValidity 为负数时回退到默认 49 天"""
        data = self._make_base_data(certificate={"altNames": {"dns": [], "ip": []}, "requestedValidity": -10})
        agent_info = AgentInfo(data)
        assert agent_info.get_certificate_validity_days() == 49

    def test_get_certificate_dns_names_returns_copy(self) -> None:
        """get_certificate_dns_names() 返回副本，修改不影响原始数据"""
        data = self._make_base_data(
            certificate={
                "altNames": {"dns": ["agent.example.com"], "ip": []},
                "requestedValidity": None,
            }
        )
        agent_info = AgentInfo(data)
        names = agent_info.get_certificate_dns_names()
        names.append("injected.evil.com")
        assert "injected.evil.com" not in agent_info.get_certificate_dns_names()

    def test_get_certificate_ip_addresses_returns_copy(self) -> None:
        """get_certificate_ip_addresses() 返回副本，修改不影响原始数据"""
        data = self._make_base_data(
            certificate={
                "altNames": {"dns": [], "ip": ["10.0.0.1"]},
                "requestedValidity": None,
            }
        )
        agent_info = AgentInfo(data)
        ips = agent_info.get_certificate_ip_addresses()
        ips.append("99.99.99.99")
        assert "99.99.99.99" not in agent_info.get_certificate_ip_addresses()


# ================== 单一 EKU 扩展测试（v2.1.0）==================


class TestSingleEKUExtension:
    """验证 v2.1.0 单一 EKU 证书行为：clientAuth 或 serverAuth，二选一"""

    def setup_method(self):
        from app.core.ca_manager import CAManager

        self.ca_manager = CAManager()

    def _create_test_csr(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TESTAGENT001")])
        return x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

    def _sign_and_get_eku_oids(self, usage: str) -> list:
        from cryptography import x509

        csr = self._create_test_csr()
        cert_pem = self.ca_manager.sign_certificate(csr, ["TESTAGENT001"], usage=usage)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        return list(eku_ext.value)

    def test_client_auth_eku_only(self) -> None:
        """usage=clientAuth 时证书只含 clientAuth 单一 EKU"""
        from cryptography import x509

        oids = self._sign_and_get_eku_oids("clientAuth")
        assert len(oids) == 1
        assert oids[0] == x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH

    def test_server_auth_eku_only(self) -> None:
        """usage=serverAuth 时证书只含 serverAuth 单一 EKU"""
        from cryptography import x509

        oids = self._sign_and_get_eku_oids("serverAuth")
        assert len(oids) == 1
        assert oids[0] == x509.oid.ExtendedKeyUsageOID.SERVER_AUTH

    def test_default_usage_is_client_auth(self) -> None:
        """不指定 usage 时默认使用 clientAuth"""
        from cryptography import x509

        csr = self._create_test_csr()
        cert_pem = self.ca_manager.sign_certificate(csr, ["TESTAGENT001"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        oids = list(eku_ext.value)
        assert len(oids) == 1
        assert oids[0] == x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH

    def test_no_mixed_eku(self) -> None:
        """clientAuth 证书不含 serverAuth，反之亦然"""
        from cryptography import x509

        oids_client = self._sign_and_get_eku_oids("clientAuth")
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH not in oids_client

        oids_server = self._sign_and_get_eku_oids("serverAuth")
        assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH not in oids_server


# ================== Agent SAN 扩展测试（v2.1.0）==================


class TestAgentSANExtensions:
    """验证 v2.1.0 Agent SAN 扩展行为"""

    def setup_method(self):
        from app.core.ca_manager import CAManager

        self.ca_manager = CAManager()

    def _create_csr(self, cn: str = "TESTAGENT001"):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        return x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

    def _sign_and_get_san(self, agent_id, dns_names=None, ip_addresses=None):
        from cryptography import x509

        csr = self._create_csr(agent_id)
        cert_pem = self.ca_manager.sign_certificate(csr, [agent_id], dns_names=dns_names, ip_addresses=ip_addresses)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    def test_acps_uri_always_included(self) -> None:
        """SAN 始终包含 URI:acps://{AIC}"""
        from cryptography import x509

        agent_id = "TESTAGENT001"
        san = self._sign_and_get_san(agent_id)
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        assert f"acps://{agent_id}" in uris

    def test_no_auto_dns_san_without_param(self) -> None:
        """不传 dns_names 时不自动生成任何 DNS SAN（去除旧 acps.pub 行为）"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001")
        dns_names = san.get_values_for_type(x509.DNSName)
        assert len(dns_names) == 0

    def test_custom_dns_san_included(self) -> None:
        """传入 dns_names 时 DNS SAN 正确写入"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001", dns_names=["agent.example.com", "alt.example.com"])
        dns_names = san.get_values_for_type(x509.DNSName)
        assert "agent.example.com" in dns_names
        assert "alt.example.com" in dns_names

    def test_ipv4_san_included(self) -> None:
        """传入 IPv4 地址时 IP SAN 正确写入"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001", ip_addresses=["192.168.1.100"])
        ip_sans = san.get_values_for_type(x509.IPAddress)
        assert str(ip_sans[0]) == "192.168.1.100"

    def test_ipv6_san_included(self) -> None:
        """传入 IPv6 地址时 IP SAN 正确写入"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001", ip_addresses=["::1"])
        ip_sans = san.get_values_for_type(x509.IPAddress)
        assert str(ip_sans[0]) == "::1"

    def test_invalid_ip_skipped_valid_one_included(self) -> None:
        """无效 IP 地址被跳过，有效 IP 正常写入"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001", ip_addresses=["not-an-ip", "10.0.0.1"])
        ip_sans = san.get_values_for_type(x509.IPAddress)
        assert len(ip_sans) == 1
        assert str(ip_sans[0]) == "10.0.0.1"

    def test_all_invalid_ips_produce_no_ip_san(self) -> None:
        """全部 IP 无效时不写入任何 IP SAN"""
        from cryptography import x509

        san = self._sign_and_get_san("TESTAGENT001", ip_addresses=["bad", "also-bad"])
        ip_sans = san.get_values_for_type(x509.IPAddress)
        assert len(ip_sans) == 0

    def test_combined_dns_and_ip_san(self) -> None:
        """同时传入 dns_names 和 ip_addresses 时两者都写入，URI 也在"""
        from cryptography import x509

        agent_id = "TESTAGENT001"
        san = self._sign_and_get_san(agent_id, dns_names=["agent.example.com"], ip_addresses=["10.10.10.10"])
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        dns_names = san.get_values_for_type(x509.DNSName)
        ip_sans = san.get_values_for_type(x509.IPAddress)

        assert f"acps://{agent_id}" in uris
        assert "agent.example.com" in dns_names
        assert str(ip_sans[0]) == "10.10.10.10"


# ================== 证书动态有效期测试（v2.1.0）==================


class TestCertificateValidityDynamic:
    """验证 v2.1.0 动态证书有效期：来自 agent_info.requestedValidity，受系统上限约束"""

    def setup_method(self):
        from app.acme.service import CertificateService
        from app.core.ca_manager import CAManager

        self.ca_manager = CAManager()
        self.mock_session = Mock()
        self.cert_service = CertificateService(self.mock_session)

    def _create_test_csr(self, cn="agent-001"):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        return x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

    def test_generate_certificate_uses_agent_requested_validity(self) -> None:
        """_generate_certificate_for_agent 使用 agent_info.requestedValidity=90 签发 90 天证书"""
        from cryptography import x509

        agent_info = AgentInfo(
            {
                "aic": "agent-001",
                "active": True,
                "name": "Test",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
                "certificate": {
                    "altNames": {"dns": [], "ip": []},
                    "requestedValidity": 90,
                },
            }
        )
        csr = self._create_test_csr()
        cert_pem = self.cert_service._generate_certificate_for_agent(csr, "agent-001", agent_info)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert delta.days == 90

    def test_generate_certificate_defaults_to_49_days_when_no_validity(self) -> None:
        """未指定 requestedValidity 时默认 49 天"""
        from cryptography import x509

        agent_info = AgentInfo(
            {
                "aic": "agent-001",
                "active": True,
                "name": "Test",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
            }
        )
        csr = self._create_test_csr()
        cert_pem = self.cert_service._generate_certificate_for_agent(csr, "agent-001", agent_info)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert delta.days == 49

    def test_generate_certificate_capped_by_max_validity(self) -> None:
        """requestedValidity 超出系统最大值时截断到 max_certificate_validity_days"""
        from cryptography import x509

        agent_info = AgentInfo(
            {
                "aic": "agent-001",
                "active": True,
                "name": "Test",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
                "certificate": {
                    "altNames": {"dns": [], "ip": []},
                    "requestedValidity": 9999,
                },
            }
        )
        csr = self._create_test_csr()
        # 将系统上限临时设为 30 天
        self.cert_service.settings.max_certificate_validity_days = 30
        cert_pem = self.cert_service._generate_certificate_for_agent(csr, "agent-001", agent_info)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert delta.days == 30

    def test_generate_certificate_passes_dns_and_ip_san(self) -> None:
        """_generate_certificate_for_agent 将 agent_info 的 altNames 写入证书 SAN"""
        from cryptography import x509

        agent_info = AgentInfo(
            {
                "aic": "agent-001",
                "active": True,
                "name": "Test",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
                "certificate": {
                    "altNames": {
                        "dns": ["agent.example.com"],
                        "ip": ["10.0.0.1"],
                    },
                    "requestedValidity": None,
                },
            }
        )
        csr = self._create_test_csr()
        cert_pem = self.cert_service._generate_certificate_for_agent(csr, "agent-001", agent_info)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

        dns_names = san.get_values_for_type(x509.DNSName)
        ip_sans = san.get_values_for_type(x509.IPAddress)
        assert "agent.example.com" in dns_names
        assert str(ip_sans[0]) == "10.0.0.1"


# ================== issue_certificate usage 传递测试（v2.1.0）==================


class TestIssueWithUsage:
    """验证 usage 参数在 issue_certificate 中的传递"""

    def setup_method(self):
        from app.acme.service import CertificateService

        self.mock_session = Mock()
        self.cert_service = CertificateService(self.mock_session)

    def _create_test_csr(self, cn="agent-001"):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        return x509.CertificateSigningRequestBuilder().subject_name(subject).sign(private_key, hashes.SHA256())

    async def _call_issue_and_capture_generate(self, usage, order_identifiers=None):
        """辅助：调用 issue_certificate 并捕获 _generate_certificate_for_agent 的调用参数"""
        import secrets

        from app.acme.model import AcmeOrder

        order = Mock(spec=AcmeOrder)
        order.id = 1
        order.identifiers = order_identifiers or [{"type": "agent", "value": "agent-001"}]

        agent_info = AgentInfo(
            {
                "aic": "agent-001",
                "active": True,
                "name": "Test",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
            }
        )

        csr = self._create_test_csr()
        csr_der = csr.public_bytes(serialization.Encoding.DER)
        mock_cert = Mock()
        mock_cert.cert_id = "cert_" + secrets.token_urlsafe(8)

        with patch.object(
            self.cert_service,
            "_generate_certificate_for_agent",
            return_value="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----",
        ) as mock_gen:
            with (
                patch.object(self.cert_service, "_create_certificate", new=AsyncMock(return_value=mock_cert)),
                patch.object(
                    self.cert_service,
                    "_extract_subject_from_cert_pem",
                    return_value={"CN": "agent-001"},
                ),
                patch.object(
                    self.cert_service,
                    "_extract_serial_number_from_cert_pem",
                    return_value="ABCDEF",
                ),
            ):
                if usage is None:
                    await self.cert_service.issue_certificate(order, csr_der, [agent_info])
                else:
                    await self.cert_service.issue_certificate(order, csr_der, [agent_info], usage=usage)
            return mock_gen.call_args

    async def test_server_auth_usage_propagated_to_generate(self) -> None:
        """usage='serverAuth' 应传递给 _generate_certificate_for_agent"""
        call_args = await self._call_issue_and_capture_generate("serverAuth")
        assert call_args.kwargs.get("usage") == "serverAuth"

    async def test_client_auth_usage_propagated_to_generate(self) -> None:
        """usage='clientAuth' 应传递给 _generate_certificate_for_agent"""
        call_args = await self._call_issue_and_capture_generate("clientAuth")
        assert call_args.kwargs.get("usage") == "clientAuth"

    async def test_default_usage_is_client_auth(self) -> None:
        """不指定 usage 时默认传递 'clientAuth'"""
        call_args = await self._call_issue_and_capture_generate(None)
        assert call_args.kwargs.get("usage") == "clientAuth"


# ================== IdentifierInput schema 测试（v2.1.0）==================


class TestIdentifierInputSchema:
    """验证 IdentifierInput 的 usage 字段（v2.1.0 新增）"""

    def test_identifier_without_usage_defaults_to_none(self) -> None:
        """不传 usage 时默认为 None"""
        from app.acme.schema import IdentifierInput

        identifier = IdentifierInput(type="agent", value="TESTAGENT001")
        assert identifier.usage is None

    def test_identifier_with_client_auth(self) -> None:
        """usage='clientAuth' 可正常解析"""
        from app.acme.schema import IdentifierInput

        identifier = IdentifierInput(type="agent", value="TESTAGENT001", usage="clientAuth")
        assert identifier.usage == "clientAuth"

    def test_identifier_with_server_auth(self) -> None:
        """usage='serverAuth' 可正常解析"""
        from app.acme.schema import IdentifierInput

        identifier = IdentifierInput(type="agent", value="TESTAGENT001", usage="serverAuth")
        assert identifier.usage == "serverAuth"

    def test_identifier_json_roundtrip_with_usage(self) -> None:
        """含 usage 的 Identifier 可以 JSON 序列化/反序列化"""
        from app.acme.schema import IdentifierInput

        identifier = IdentifierInput(type="agent", value="TESTAGENT001", usage="serverAuth")
        restored = IdentifierInput.model_validate_json(identifier.model_dump_json())
        assert restored.usage == "serverAuth"
        assert restored.type == "agent"
        assert restored.value == "TESTAGENT001"

    def test_identifier_usage_none_in_json(self) -> None:
        """usage=None 时序列化后仍可正常反序列化"""
        from app.acme.schema import IdentifierInput

        identifier = IdentifierInput(type="agent", value="TESTAGENT001")
        restored = IdentifierInput.model_validate_json(identifier.model_dump_json())
        assert restored.usage is None


class TestOrderIdentifierNormalization:
    """验证订单标识符规范化不会丢失 usage。"""

    def setup_method(self) -> None:
        from app.acme.service import OrderService

        self.order_service = OrderService(Mock())

    async def test_normalize_and_validate_preserves_usage(self) -> None:
        registry_client = Mock()
        agent_info = AgentInfo(
            {
                "aic": "TESTAGENT001",
                "active": True,
                "name": "Test Agent",
                "version": "1.0.0",
                "provider": {"organization": "Org", "countryCode": "CN"},
            }
        )
        registry_client.validate_aic_and_get_info = AsyncMock(return_value=agent_info)

        normalized_identifiers, validated_agents = await self.order_service.normalize_and_validate_agent_identifiers(
            [{"type": "agent", "value": "testagent001", "usage": "serverAuth"}],
            "TESTAGENT001",
            registry_client,
        )

        assert normalized_identifiers == [{"type": "agent", "value": "TESTAGENT001", "usage": "serverAuth"}]
        registry_client.validate_aic_and_get_info.assert_awaited_once_with("TESTAGENT001")
        assert validated_agents == [agent_info]


class TestAcmeApiUsageFlow:
    """验证 usage 通过 ACME HTTP API 全链路传递到最终证书。"""

    async def test_new_order_server_auth_finalizes_to_server_auth_cert(
        self,
        client,
        async_db_session,
    ) -> None:
        account_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        account_jwk = _build_rsa_jwk(account_private_key.public_key())
        account_key_id = JWSVerifier().compute_jwk_thumbprint(account_jwk)
        suffix = uuid4().hex[:16].upper()
        account_aic = f"TESTSERVERAUTH{suffix}"

        account = AcmeAccount(
            key_id=account_key_id,
            public_key=json.dumps(account_jwk),
            contact=[f"mailto:serverauth-{suffix.lower()}@example.com"],
            terms_of_service_agreed=True,
            aic=account_aic,
        )
        async_db_session.add(account)
        await async_db_session.commit()
        await async_db_session.refresh(account)

        csr_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, account_aic)]))
            .sign(csr_private_key, hashes.SHA256())
        )
        csr_der = csr.public_bytes(serialization.Encoding.DER)

        agent_info = AgentInfo(
            {
                "aic": account_aic,
                "active": True,
                "name": "Server Auth Test Agent",
                "version": "1.0.0",
                "provider": {"organization": "ACPs Test", "countryCode": "CN"},
                "securitySchemes": {"mtls": {"type": "mutualTLS"}},
                "endPoints": [
                    {
                        "url": "https://localhost:9443/acps-atr-v2/entity",
                        "transport": "REST",
                        "security": [{"mtls": []}],
                    }
                ],
                "capabilities": {"streaming": False, "notification": False, "messageQueue": []},
                "skills": [],
                "certificate": {"altNames": {"dns": ["localhost"]}},
            }
        )
        registry_client = Mock()
        registry_client.validate_aic_and_get_info = AsyncMock(return_value=agent_info)
        registry_client.register_certificate_request = AsyncMock(return_value=None)
        registry_client.notify_certificate_issued = AsyncMock(return_value=None)

        try:
            with patch("app.acme.api.get_registry_client", return_value=registry_client):
                order_nonce = f"test-serverauth-{uuid4().hex}"
                await _insert_test_nonce(async_db_session, order_nonce)

                order_response = client.post(
                    "/acps-atr-v2/acme/new-order",
                    json=_build_test_jws_request(
                        account_private_key,
                        {
                            "alg": "RS256",
                            "kid": _acme_url(f"/acct/{account.id}"),
                            "nonce": order_nonce,
                            "url": _acme_url("/new-order"),
                        },
                        {
                            "identifiers": [
                                {
                                    "type": "agent",
                                    "value": account_aic,
                                    "usage": "serverAuth",
                                }
                            ]
                        },
                    ),
                )

                assert order_response.status_code == 201
                order_payload = order_response.json()
                finalize_url = str(order_payload["finalize"])

                finalize_nonce = f"test-serverauth-{uuid4().hex}"
                await _insert_test_nonce(async_db_session, finalize_nonce)

                finalize_response = client.post(
                    urlparse(finalize_url).path,
                    json=_build_test_jws_request(
                        account_private_key,
                        {
                            "alg": "RS256",
                            "kid": _acme_url(f"/acct/{account.id}"),
                            "nonce": finalize_nonce,
                            "url": finalize_url,
                        },
                        {
                            "csr": base64.urlsafe_b64encode(csr_der).decode("ascii").rstrip("="),
                        },
                    ),
                )

                assert finalize_response.status_code == 200
                finalize_payload = finalize_response.json()
                cert_url = str(finalize_payload["certificate"])

                cert_nonce = f"test-serverauth-{uuid4().hex}"
                await _insert_test_nonce(async_db_session, cert_nonce)

                cert_response = client.post(
                    urlparse(cert_url).path,
                    json=_build_test_jws_request(
                        account_private_key,
                        {
                            "alg": "RS256",
                            "kid": _acme_url(f"/acct/{account.id}"),
                            "nonce": cert_nonce,
                            "url": cert_url,
                        },
                        None,
                    ),
                )

                assert cert_response.status_code == 200
                cert_pem = cert_response.content
                pem_header = b"-----BEGIN CERTIFICATE-----"
                next_cert_offset = cert_pem.find(pem_header, len(pem_header))
                leaf_pem = cert_pem[:next_cert_offset] if next_cert_offset > 0 else cert_pem
                certificate = x509.load_pem_x509_certificate(leaf_pem)
                eku_extension = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)

                assert list(eku_extension.value) == [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]
                assert registry_client.validate_aic_and_get_info.await_count >= 1
        finally:
            assert account.id is not None
            order_id_rows = await async_db_session.execute(
                select(cast("Any", AcmeOrder.id)).where(cast("Any", AcmeOrder.account_id) == account.id)
            )
            order_ids = [order_id for order_id in order_id_rows.scalars().all() if order_id is not None]

            if order_ids:
                await async_db_session.execute(
                    delete(AcmeAuthorization).where(cast("Any", AcmeAuthorization.order_id).in_(order_ids))
                )
                await async_db_session.execute(
                    delete(AcmeCertificate).where(cast("Any", AcmeCertificate.order_id).in_(order_ids))
                )

            await async_db_session.execute(delete(AcmeOrder).where(cast("Any", AcmeOrder.account_id) == account.id))
            await async_db_session.execute(delete(AcmeAccount).where(cast("Any", AcmeAccount.id) == account.id))
            await async_db_session.execute(
                delete(AcmeNonce).where(cast("Any", AcmeNonce.nonce).like("test-serverauth-%"))
            )
            await async_db_session.commit()
