import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import ConfigDict, ValidationInfo, field_validator
from sqlalchemy import TIMESTAMP
from sqlmodel import Column, Field, Relationship, SQLModel
from sqlmodel._compat import SQLModelConfig

from app.core.base_model import TimestampMixin, UUIDMixin

# 条件导入以避免循环引用
if TYPE_CHECKING:
    from app.agent.model import Agent


class RoleType(StrEnum):
    CLIENT = "CLIENT"
    STAFF = "STAFF"
    ADMIN = "ADMIN"


class Role(UUIDMixin, SQLModel, table=True):
    __tablename__ = "account_role"  # pyright: ignore[reportAssignmentType, reportIncompatibleVariableOverride]
    name: RoleType = Field(index=True)
    description: str | None = None

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(arbitrary_types_allowed=True))


# 用户与角色多对多关系的关联表
class UserRoleLink(SQLModel, table=True):
    __tablename__ = "account_user_role_link"  # pyright: ignore[reportAssignmentType, reportIncompatibleVariableOverride]

    user_id: uuid.UUID | None = Field(default=None, foreign_key="account_user.id", primary_key=True)
    role_id: uuid.UUID | None = Field(default=None, foreign_key="account_role.id", primary_key=True)


class VerificationCode(UUIDMixin, TimestampMixin, SQLModel, table=True):
    __tablename__ = "account_verification_code"  # pyright: ignore[reportAssignmentType, reportIncompatibleVariableOverride]

    phone: str = Field(index=True, unique=True, max_length=20)
    code: str = Field(max_length=10)
    expires_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True)),
    )

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(arbitrary_types_allowed=True))


class User(UUIDMixin, TimestampMixin, SQLModel, table=True):
    __tablename__ = "account_user"  # pyright: ignore[reportAssignmentType, reportIncompatibleVariableOverride]
    username: str | None = Field(default=None, index=True, unique=True)
    email: str | None = Field(default=None, index=True, unique=True)
    phone: str | None = Field(default=None, index=True, unique=True)
    hashed_password: str | None = None
    auth_provider: str = Field(default="local", index=True)
    external_issuer: str | None = Field(default=None, index=True)
    external_subject: str | None = Field(default=None, index=True)
    external_principal_id: str | None = Field(default=None, index=True, unique=True)
    external_username: str | None = None

    # 个人资料信息
    name: str | None = None
    avatar: str | None = None

    # 组织信息
    org_name: str | None = None
    org_code: str | None = None
    org_address: str | None = None

    # AIC 第7级供应商序号；可空，非空值唯一。由发号与管理接口写入并规范化为大写。
    aic_provider_code: str | None = Field(default=None, max_length=6, unique=True)

    # Token 信息
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),  # 使用带时区的时间戳
    )
    last_login_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True)))

    # 状态信息
    is_active: bool = Field(default=True)
    identity_verified: bool = Field(default=False)
    identity_verified_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True)))
    current_identity_id: uuid.UUID | None = Field(default=None, index=True)
    org_verified: bool = Field(default=False)
    org_verified_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True)))
    current_org_id: uuid.UUID | None = Field(default=None, index=True)
    # 基本关系
    roles: list[Role] = Relationship(link_model=UserRoleLink)

    # Agent 关系 - 创建的 Agents
    created_agents: list[Agent] = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "Agent.created_by_id",
            "back_populates": "created_by",
        }
    )

    # Agent 关系 - 处理的 Agents
    processed_agents: list[Agent] = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "Agent.processed_by_id",
            "back_populates": "processed_by",
        }
    )

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(arbitrary_types_allowed=True))

    @field_validator("username", "phone")
    @classmethod
    def validate_identification(cls, v: str | None, info: ValidationInfo) -> str | None:
        # 该校验器会同时用于 username 和 phone 字段
        # 对 phone 字段进行校验时，需要同时检查 username 是否已提供
        values: dict[str, Any] = info.data
        if (
            v is None
            and "username" in values
            and values["username"] is None
            and "phone" in values
            and values["phone"] is None
        ):
            raise ValueError("Either username or phone must be provided")
        return v


# 解析循环引用 - 只需要导入 Agent 以完成关系解析
