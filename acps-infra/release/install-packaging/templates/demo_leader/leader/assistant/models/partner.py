"""
Leader Agent Platform - Partner（ACS 驱动的能力与端点）

本模块定义 Partner 相关的数据模型，包括端点解析和运行时状态。

ACS 相关的核心类型（AgentCapabilitySpec、AgentSkill、AgentEndPoint、AgentCapabilities）
直接使用 acps_sdk.acs 中的标准定义，避免重复定义和不一致。
"""

from typing import Any

# 直接使用 acps_sdk.acs 中的标准 ACS 模型
from acps_sdk.acs import AgentCapabilitySpec
from pydantic import BaseModel, ConfigDict, Field

from .base import AgentAic, IsoDateTimeString

# =============================================================================
# 端点解析
# =============================================================================


class ResolvedPartnerEndpoint(BaseModel):
 """
    Leader 选择出来的"可用端点"。

    把 ACS 原始声明收敛成"这次会话要用哪一个"。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner AIC",
    )
    url: str = Field(..., description="被选择的端点 URL")
    transport: str = Field(default="JSONRPC", description="传输协议")
    reason: str = Field(..., description="选择理由（用于可观测性/排障）")

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 身份校验
# =============================================================================


class PeerIdentityVerification(BaseModel):
 """
    mTLS 对端身份校验结果。
 """

    verified_at: IsoDateTimeString = Field(
        ...,
        alias="verifiedAt",
        description="校验时间",
    )
    verified: bool = Field(..., description="是否通过校验")
    peer_aic_from_cert: AgentAic | None = Field(
        default=None,
        alias="peerAicFromCert",
        description="证书中解析到的对端 AIC",
    )
    expected_peer_aic: AgentAic | None = Field(
        default=None,
        alias="expectedPeerAic",
        description="逻辑上期望的对端 AIC",
    )
    reason: str | None = Field(default=None, description="未通过时的原因")

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 可用性详情
# =============================================================================


class PartnerAvailabilityDetails(BaseModel):
 """
    Leader 对"为什么可用/为什么不可用"的结构化解释。

    用于 UI/日志/排障。
 """

    acs_active: bool = Field(
        ...,
        alias="acsActive",
        description="ACS 是否 active",
    )
    mode_supported: bool = Field(
        ...,
        alias="modeSupported",
        description="是否支持当前 Session mode",
    )
    has_json_rpc_endpoint: bool = Field(
        ...,
        alias="hasJsonRpcEndpoint",
        description="是否存在可用的 JSONRPC 端点",
    )
    has_mtls_security: bool = Field(
        ...,
        alias="hasMtlsSecurity",
        description="是否存在可满足 mTLS 的 security 组合",
    )
    reason: str | None = Field(default=None, description="人类可读原因")

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# 最近错误
# =============================================================================


class PartnerLastError(BaseModel):
 """
    Partner 最近一次错误记录。
 """

    code: str = Field(..., description="Leader 内部错误码")
    message: str = Field(..., description="错误消息")
    details: Any | None = Field(default=None, description="错误详情")
    happened_at: IsoDateTimeString = Field(
        ...,
        alias="happenedAt",
        description="发生时间",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# Partner 运行时状态
# =============================================================================


class PartnerRuntimeState(BaseModel):
 """
    会话内 Partner 的运行时状态（最小集）。
 """

    partner_aic: AgentAic = Field(
        ...,
        alias="partnerAic",
        description="Partner 的主键",
    )
    acs: AgentCapabilitySpec = Field(
        ...,
        description="会话期缓存的 ACS（使用 acps_sdk.acs 标准模型）",
    )
    resolved_endpoint: ResolvedPartnerEndpoint | None = Field(
        default=None,
        alias="resolvedEndpoint",
        description="Direct 模式下选出来的 RPC 端点",
    )
    availability_details: PartnerAvailabilityDetails | None = Field(
        default=None,
        alias="availabilityDetails",
        description="可用性的结构化解释",
    )
    available: bool = Field(
        default=True,
        description="可用性（Leader 的主观判断）",
    )
    last_ok_at: IsoDateTimeString | None = Field(
        default=None,
        alias="lastOkAt",
        description="最近一次成功交互时间",
    )
    last_error: PartnerLastError | None = Field(
        default=None,
        alias="lastError",
        description="最近一次错误",
    )
    last_identity_verification: PeerIdentityVerification | None = Field(
        default=None,
        alias="lastIdentityVerification",
        description="最近一次身份校验结果",
    )

    model_config = ConfigDict(populate_by_name=True)
