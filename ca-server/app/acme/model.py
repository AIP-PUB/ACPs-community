"""ACME 数据库模型：账户、订单、授权、挑战、Nonce、证书签发记录等 ORM 定义"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import DateTime as SADateTime
from sqlmodel import JSON, Column, Field, Relationship, SQLModel, Text

from app.common import CertificateStatus, RevocationReason, beijing_end_of_day, beijing_now


def _aware_datetime_type() -> Any:
    """返回带时区的 datetime SQLAlchemy 类型"""
    return SADateTime(timezone=True)


class AccountStatus(StrEnum):
    """账户状态枚举"""

    VALID = "valid"
    DEACTIVATED = "deactivated"
    REVOKED = "revoked"


class OrderStatus(StrEnum):
    """订单状态枚举"""

    PENDING = "pending"
    READY = "ready"
    PROCESSING = "processing"
    VALID = "valid"
    INVALID = "invalid"


class AuthorizationStatus(StrEnum):
    """授权状态枚举"""

    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    DEACTIVATED = "deactivated"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ChallengeStatus(StrEnum):
    """挑战状态枚举"""

    PENDING = "pending"
    PROCESSING = "processing"
    VALID = "valid"
    INVALID = "invalid"


class ChallengeType(StrEnum):
    """挑战类型枚举"""

    HTTP_01 = "http-01"


class AcmeAccount(SQLModel, table=True):
    """ACME 账户模型"""

    __tablename__: ClassVar[str] = "acme_accounts"

    id: int | None = Field(default=None, primary_key=True)
    # ACME 账户的公钥指纹，用作唯一标识
    key_id: str = Field(unique=True, index=True, max_length=255)
    # JWK 格式的公钥
    public_key: str = Field(sa_column=Column(Text))
    # 账户状态
    status: AccountStatus = Field(default=AccountStatus.VALID)
    # 联系信息，通常是邮箱
    contact: list[str] | None = Field(default=None, sa_column=Column(JSON))
    # 是否同意服务条款
    terms_of_service_agreed: bool = Field(default=False)
    # 外部账户绑定信息
    external_account_binding: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    aic: str | None = Field(default=None, index=True, max_length=255)

    # 创建和更新时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
    updated_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())

    # 关系
    orders: list[AcmeOrder] = Relationship(
        back_populates="account",
        sa_relationship_kwargs={"lazy": "raise"},
    )


class AcmeOrder(SQLModel, table=True):
    """ACME 订单模型"""

    __tablename__: ClassVar[str] = "acme_orders"

    id: int | None = Field(default=None, primary_key=True)
    # 订单的唯一标识符
    order_id: str = Field(unique=True, index=True, max_length=255)
    # 关联的账户ID
    account_id: int = Field(foreign_key="acme_accounts.id")
    # 订单状态
    status: OrderStatus = Field(default=OrderStatus.PENDING)
    # 要申请证书的标识符列表 (Agent IDs)
    identifiers: list[dict[str, str]] = Field(sa_column=Column(JSON))
    # 证书有效期结束时间
    not_before: datetime | None = Field(default=None, sa_type=_aware_datetime_type())
    not_after: datetime | None = Field(default=None, sa_type=_aware_datetime_type())
    # 错误信息
    error: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # 授权链接
    authorizations: list[str] | None = Field(default=None, sa_column=Column(JSON))
    # 完成链接
    finalize: str | None = Field(default=None, max_length=512)
    # 证书链接
    certificate: str | None = Field(default=None, max_length=512)

    # 创建和更新时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
    updated_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())
    expires: datetime = Field(default_factory=beijing_end_of_day, sa_type=_aware_datetime_type())

    # 关系
    account: AcmeAccount = Relationship(
        back_populates="orders",
        sa_relationship_kwargs={"lazy": "raise"},
    )
    authorizations_rel: list[AcmeAuthorization] = Relationship(
        back_populates="order",
        sa_relationship_kwargs={"lazy": "raise"},
    )
    certificates: list[AcmeCertificate] = Relationship(
        back_populates="order",
        sa_relationship_kwargs={"lazy": "raise"},
    )


class AcmeAuthorization(SQLModel, table=True):
    """ACME 授权模型"""

    __tablename__: ClassVar[str] = "acme_authorizations"

    id: int | None = Field(default=None, primary_key=True)
    # 授权的唯一标识符
    authz_id: str = Field(unique=True, index=True, max_length=255)
    # 关联的订单ID
    order_id: int = Field(foreign_key="acme_orders.id")
    # 要验证的标识符 (Agent ID)
    identifier: dict[str, str] = Field(sa_column=Column(JSON))
    # 授权状态
    status: AuthorizationStatus = Field(default=AuthorizationStatus.PENDING)
    # 授权过期时间
    expires: datetime = Field(default_factory=beijing_end_of_day, sa_type=_aware_datetime_type())
    # 是否为通配符授权
    wildcard: bool = Field(default=False)

    # 创建和更新时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
    updated_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())

    # 关系
    order: AcmeOrder = Relationship(
        back_populates="authorizations_rel",
        sa_relationship_kwargs={"lazy": "raise"},
    )
    challenges: list[AcmeChallenge] = Relationship(
        back_populates="authorization",
        sa_relationship_kwargs={"lazy": "raise"},
    )


class AcmeChallenge(SQLModel, table=True):
    """ACME 挑战模型"""

    # 兼容层：只读，不再增强（Phase 4.3）。系统仍保留 challenge 记录与字段形状。
    # 但 Agent CA 认证的主体是 AIC，且 AIC 已在 new-account/EAB 阶段绑定到 account，
    # 后续不再需要用 challenge 证明域名控制权；challenge 会在服务端流程中直接转为 valid。

    __tablename__: ClassVar[str] = "acme_challenges"

    id: int | None = Field(default=None, primary_key=True)
    # 挑战的唯一标识符
    challenge_id: str = Field(unique=True, index=True, max_length=255)
    # 关联的授权ID
    authorization_id: int = Field(foreign_key="acme_authorizations.id")
    # 挑战类型
    type: ChallengeType = Field(default=ChallengeType.HTTP_01)
    # 挑战状态
    status: ChallengeStatus = Field(default=ChallengeStatus.PENDING)
    # 挑战令牌
    token: str = Field(max_length=255)
    # 验证的URL
    url: str | None = Field(default=None, max_length=512)
    # 验证时间
    validated: datetime | None = Field(default=None, sa_type=_aware_datetime_type())
    # 错误信息
    error: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # 创建和更新时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
    updated_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())

    # 关系
    authorization: AcmeAuthorization = Relationship(
        back_populates="challenges",
        sa_relationship_kwargs={"lazy": "raise"},
    )


class AcmeCertificate(SQLModel, table=True):
    """ACME 证书模型"""

    __tablename__: ClassVar[str] = "acme_certificates"

    id: int | None = Field(default=None, primary_key=True)
    # 证书的唯一标识符
    cert_id: str = Field(unique=True, index=True, max_length=255)
    # 关联的订单ID
    order_id: int = Field(foreign_key="acme_orders.id")
    # 证书序列号
    serial_number: str = Field(unique=True, max_length=255)
    # PEM 格式的证书链
    certificate_pem: str = Field(sa_column=Column(Text))
    # 证书状态
    status: CertificateStatus = Field(default=CertificateStatus.VALID)
    # 证书主体信息
    subject: dict[str, Any] = Field(sa_column=Column(JSON))
    # 证书有效期
    not_before: datetime = Field(sa_type=_aware_datetime_type())
    not_after: datetime = Field(sa_type=_aware_datetime_type())
    # 吊销信息
    revoked_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())
    revocation_reason: RevocationReason | None = Field(default=None)
    # Agent Identity Code - 用于批量吊销
    aic: str | None = Field(default=None, index=True, max_length=255)

    # 创建和更新时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
    updated_at: datetime | None = Field(default=None, sa_type=_aware_datetime_type())

    order: AcmeOrder = Relationship(
        back_populates="certificates",
        sa_relationship_kwargs={"lazy": "raise"},
    )


class AcmeNonce(SQLModel, table=True):
    """ACME Nonce 模型"""

    __tablename__: ClassVar[str] = "acme_nonces"

    id: int | None = Field(default=None, primary_key=True)
    # Nonce 值
    nonce: str = Field(unique=True, index=True, max_length=255)
    # 是否已使用
    used: bool = Field(default=False)
    # 过期时间：相对当前时间的 TTL（勿用“当前小时末”，临近 :59 会塌缩到秒级）
    expires: datetime = Field(
        default_factory=lambda: beijing_now() + timedelta(hours=1),
        sa_type=_aware_datetime_type(),
    )

    # 创建时间
    created_at: datetime = Field(default_factory=beijing_now, sa_type=_aware_datetime_type())
