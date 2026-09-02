import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.account.schema_account import UserResponse  # noqa: TC001
from app.agent.model import ApprovalStatus  # noqa: TC001
from app.utils import aic
from app.utils.utils import BEIJING_TIMEZONE, utc_to_beijing


class AgentBase(BaseModel):
    name: str = Field(..., max_length=255)
    version: str = Field(..., max_length=255)
    description: str | None = None

    logo_url: str | None = Field(None, max_length=1000)
    acs: str | dict[str, Any] | None = None
    # 是否为本体 (Ontology)
    # True = 本体，可通过 ATR 实体注册 API 派生实体
    # False = 传统 Agent（本体与实体合一）或实体
    is_ontology: bool = False

    # 协议支持字段（is_acp_support/is_a2a_support/is_anp_support）已从表结构移除。


class AgentCreate(AgentBase):
    pass


class AgentUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    version: str | None = Field(None, max_length=255)
    description: str | None = None

    logo_url: str | None = Field(None, max_length=1000)
    acs: str | dict[str, Any] | None = None
    # 注意：is_ontology 一旦设置后不应该被修改，因此不在 Update 中提供
    # 如果需要更改，应该删除并重新创建

    # 协议支持字段移除后，不再基于 is_*_support 进行条件校验


class AgentProcessRequest(BaseModel):
    approve: bool
    comments: str | None = Field(None, max_length=2000)


class AgentSearchQuery(BaseModel):
    query: str
    page_num: int = 1
    page_size: int = 10


class AgentListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_num: int = Field(1, ge=1)
    page_size: int = Field(10, ge=1, le=100)
    statuses: list[ApprovalStatus] | None = None
    name: str | None = None
    version: str | None = None
    aic: str | None = None
    name_like: str | None = None
    version_like: str | None = None
    aic_like: str | None = None
    is_active: str | None = None
    is_deleted: str | None = None
    is_disabled: str | None = None
    with_users: bool = False
    is_ontology: bool | None = None
    org_name: str | None = None


class StaffAgentListQuery(AgentListQuery):
    create_by_id: uuid.UUID | None = None
    process_by_id: uuid.UUID | None = None
    processed_by_me: bool = False


class AgentFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_num: int = Field(1, ge=1)
    page_size: int = Field(10, ge=1, le=100)
    statuses: list[ApprovalStatus] | None = None
    name: str | None = None
    version: str | None = None
    aic: str | None = None
    name_like: str | None = None
    version_like: str | None = None
    aic_like: str | None = None
    create_by_ids: list[uuid.UUID] | None = None
    create_by_id: uuid.UUID | None = None
    process_by_id: uuid.UUID | None = None
    with_users: bool = False
    is_active: bool | None = None
    is_deleted: bool | None = None
    is_disabled: bool | None = None
    is_ontology: bool | None = None
    org_name: str | None = None


class AgentResponse(AgentBase):
    id: uuid.UUID
    aic: str | None = Field(None)
    acs_hash: str | None = Field(None, max_length=256)
    acs_version: int = 1
    acs_last_seq: int | None = None
    is_active: bool
    is_deleted: bool = False
    deleted_at: datetime | None = None
    deleted_reason: str | None = Field(None, max_length=255)
    is_disabled: bool = False
    disabled_at: datetime | None = None
    disabled_reason: str | None = Field(None, max_length=255)
    approval_status: ApprovalStatus
    created_by_ids: list[uuid.UUID] | None = None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    processed_by_id: uuid.UUID | None = None
    processed_at: datetime | None = None
    process_comments: str | None = Field(None, max_length=2000)
    vector_id: str | None = None

    @field_validator("acs", mode="before")
    @classmethod
    def normalize_acs(cls, v: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
        if v is None:
            return None
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else v
            except json.JSONDecodeError:
                return v
        return v

    # 添加datetime字段的验证器，确保返回时带有北京时区信息并采用ISO 8601格式
    @field_validator(
        "created_at",
        "updated_at",
        "submitted_at",
        "processed_at",
        "deleted_at",
        "disabled_at",
        mode="before",
    )
    @classmethod
    def convert_datetime_to_beijing(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            # 将UTC时间转换为北京时间，并确保带有时区信息
            beijing_time = utc_to_beijing(v)
            # 确保时间以ISO 8601格式返回带时区信息
            if beijing_time.tzinfo is not None:
                return beijing_time
            # 如果没有时区信息，添加北京时区信息
            return beijing_time.replace(tzinfo=BEIJING_TIMEZONE)
        return v

    model_config = ConfigDict(from_attributes=True)


class AgentDetailResponse(AgentResponse):
    """包含完整用户信息的 Agent 响应模型，仅用于详情接口"""

    created_by: UserResponse | None = None
    processed_by: UserResponse | None = None


class AgentListResponse(BaseModel):
    items: list[AgentDetailResponse]
    total: int
    page_num: int = 1
    page_size: int = 10


class AgentSearchResponse(BaseModel):
    items: list[AgentDetailResponse]
    total: int
    page_num: int
    page_size: int


class AgentDeleteResponse(BaseModel):
    message: str


class AgentBatchDeleteFailure(BaseModel):
    id: str
    reason: str


class AgentBatchDeleteResponse(BaseModel):
    success: list[str]
    failed: list[AgentBatchDeleteFailure]


# -------------------------------------------------------------------
# ATR 实体注册相关 Schema（按照 ATR-Registry-Server.md 规范定义）
# -------------------------------------------------------------------


class EndPoint(BaseModel):
    """
    服务端点定义
    """

    url: str = Field(..., description="服务端点 URL")
    security: list[dict[str, Any]] = Field(
        default_factory=list,
        description="安全方案配置，例如 [{'mtls': []}]",
    )
    transport: str = Field(
        default="JSONRPC",
        description="传输协议，可选值：JSONRPC, REST, GRPC",
    )


class CertificateAltNames(BaseModel):
    """需要写入实体证书 SAN 的 DNS/IP 条目。"""

    model_config = ConfigDict(extra="forbid")

    dns: list[str] | None = Field(None, description="需要加入证书 DNS SAN 的主机名列表")
    ip: list[str] | None = Field(None, description="需要加入证书 IP SAN 的地址列表")


class AgentCertificate(BaseModel):
    """实体证书签发配置，对应 ACS `certificate` 字段。"""

    model_config = ConfigDict(extra="forbid")

    altNames: CertificateAltNames | None = Field(  # noqa: N815
        None,
        description="需要写入证书 SAN 的 DNS/IP 条目",
    )
    requestedValidity: int | None = Field(  # noqa: N815
        None,
        ge=1,
        description="证书期望有效期（天）。最终值由 CA Server 按系统上限裁剪。",
    )


class EntityRegistrationRequest(BaseModel):
    """
    实体注册请求模型（ATR-Registry-Server.md 规范）

    用于基于已审批的本体 AIC，自动注册新的实体并获得实体 AIC。
    请求方需通过 mTLS 认证（使用本体证书）。
    """

    ontologyAic: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="本体 AIC（必填，指向所属本体）",
    )
    endPoints: list[EndPoint] | None = Field(  # noqa: N815
        None,
        description="实体的服务端点列表（可选）。如果 Agent 对外提供 API 服务，实体需要有自己独立的服务端点。",
    )
    entityUserId: str | None = Field(  # noqa: N815
        None,
        max_length=255,
        description="用于标识实体绑定的终端用户 ID（可选）",
    )
    entityMeta: dict[str, Any] | None = Field(  # noqa: N815
        None,
        description="实体的额外元数据（可选）。可包含地理位置、环境信息、用户绑定关系等。",
    )
    certificate: AgentCertificate | None = Field(
        None,
        description="实体证书签发配置（可选）。写入实体 ACS，供 CA 签发实体服务端证书时读取 SAN 与期望有效期。",
    )

    @field_validator("ontologyAic")
    @classmethod
    def validate_ontology_aic_format(cls, v: str) -> str:
        """验证本体 AIC 格式：ACPs-spec-AIC-v02.02（点分10段 + CRC16）。"""
        v = v.strip()
        if not aic.validate_aic(v):
            raise ValueError("Invalid ontologyAic format")
        if not aic.is_ontology_aic(v):
            raise ValueError("ontologyAic must be an ontology AIC")
        return v


class EntityRegistrationResult(BaseModel):
    """
    实体注册结果（包含在响应的 result 字段中）
    """

    ontologyAic: str = Field(..., description="本体 AIC")  # noqa: N815
    entityAic: str = Field(..., description="新分配的实体 AIC")  # noqa: N815
    endPoints: list[EndPoint] | None = Field(None, description="实体的服务端点列表")  # noqa: N815
    entityUserId: str | None = Field(  # noqa: N815
        None,
        description="实体绑定的终端用户 ID（如果提供）",
    )
    entityMeta: dict[str, Any] | None = Field(None, description="实体的额外元数据")  # noqa: N815
    certificate: AgentCertificate | None = Field(None, description="实体证书签发配置")


class EntityRegistrationError(BaseModel):
    """
    实体注册错误信息
    """

    code: int = Field(..., description="错误代码")
    message: str = Field(..., description="错误消息")
    data: dict[str, Any] | None = Field(None, description="可选的错误数据")


class EntityRegistrationResponse(BaseModel):
    """
    实体注册响应模型（ATR-Registry-Server.md 规范）

    遵循 CommonResponse 结构，status 为 'ok' 时返回 result，
    status 为 'error' 时返回 error。
    """

    status: str = Field(
        ...,
        description="响应状态，可能的值包括 'ok' 和 'error'",
    )
    result: EntityRegistrationResult | None = Field(
        None,
        description="方法调用的结果，如果调用成功则包含结果",
    )
    error: EntityRegistrationError | None = Field(
        None,
        description="错误信息，如果调用失败则包含错误对象",
    )
