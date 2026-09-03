from enum import IntEnum, StrEnum
from typing import Any

from app.core.acps_exception import AcpsError
from app.core.base_exception import AppError


class AgentErrorCode(StrEnum):
    """Agent 模块错误码。"""

    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    INVALID_ACS = "INVALID_ACS"
    ACS_NOT_EXISTED = "ACS_NOT_EXISTED"
    AGENT_INACTIVE = "AGENT_INACTIVE"
    AGENT_NAME_VERSION_EXISTS = "AGENT_NAME_VERSION_EXISTS"
    AGENT_NAME_ALREADY_CLAIMED = "AGENT_NAME_ALREADY_CLAIMED"
    AGENT_CREATE_FAILED = "AGENT_CREATE_FAILED"
    AGENT_UPDATE_FAILED = "AGENT_UPDATE_FAILED"
    UNAUTHORIZED_ACCESS = "UNAUTHORIZED_ACCESS"
    NON_APPROVED_AGENT_REQUIRES_AUTH = "NON_APPROVED_AGENT_REQUIRES_AUTH"
    ACCESS_DENIED_NOT_OWNER = "ACCESS_DENIED_NOT_OWNER"
    ACCESS_DENIED_OTHER_USER_AGENTS = "ACCESS_DENIED_OTHER_USER_AGENTS"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    PROCESSOR_NOT_FOUND = "PROCESSOR_NOT_FOUND"
    PROCESSOR_NOT_STAFF = "PROCESSOR_NOT_STAFF"
    LLM_CLIENT_NOT_INITIALIZED = "LLM_CLIENT_NOT_INITIALIZED"
    EMBEDDING_GENERATION_FAILED = "EMBEDDING_GENERATION_FAILED"
    INVALID_EMBEDDING_RESPONSE = "INVALID_EMBEDDING_RESPONSE"
    REMOTE_CERT_REVOKE_FAILED = "REMOTE_CERT_REVOKE_FAILED"
    SCHEMA_FILE_MISSING = "SCHEMA_FILE_MISSING"
    GENERATE_AIC_FAILED = "GENERATE_AIC_FAILED"
    AIC_OWNER_NOT_FOUND = "AIC_OWNER_NOT_FOUND"


class AgentError(AppError):
    """Agent 相关异常的基类。"""

    def __init__(
        self,
        *,
        status_code: int = 400,
        code: str | AgentErrorCode | None = None,
        title: str | None = None,
        detail: str | None = None,
        input_params: dict[str, Any] | None = None,
        error_name: str | AgentErrorCode | None = None,
        error_msg: str | None = None,
    ) -> None:
        resolved_code = str(code or error_name or "AGENT_ERROR")
        resolved_detail = detail or error_msg or "An error occurred with agent operation"
        super().__init__(
            status_code=status_code,
            code=resolved_code,
            title=title,
            detail=resolved_detail,
            type_=f"urn:acps:error:agent:{resolved_code.lower()}",
            extensions={
                "error_group": "agent",
                "input_params": input_params or {},
            },
        )


class SchemaFileMissingError(AgentError):
    """打包的 ACS schema 文件缺失时抛出的异常。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=500,
            error_name=AgentErrorCode.SCHEMA_FILE_MISSING,
            error_msg="Schema file not found",
        )


class PublicAgentNotFoundError(AgentError):
    """公开 Agent 查询应返回不存在时抛出的异常。"""

    def __init__(self, *, agent_id: str) -> None:
        super().__init__(
            status_code=404,
            error_name=AgentErrorCode.AGENT_NOT_FOUND,
            error_msg="Agent not found or not approved",
            input_params={"agent_id": agent_id},
        )


class AccessDeniedNotOwnerError(AgentError):
    """客户端访问其他用户未审批 Agent 时抛出的异常。"""

    def __init__(self, *, agent_id: str, request_user_id: str) -> None:
        super().__init__(
            status_code=403,
            error_name=AgentErrorCode.ACCESS_DENIED_NOT_OWNER,
            error_msg="Access denied: you can only view non-approved agents that you created",
            input_params={
                "agent_id": agent_id,
                "request_user_id": request_user_id,
            },
        )


class AtrErrorCode(IntEnum):
    """与规范保持一致的 ATR 协议数字错误码。"""

    INVALID_REQUEST = 40001  # 请求参数格式错误或缺少必填字段
    UNAUTHORIZED = 40101  # mTLS 认证失败，证书无效或未提供
    ACCESS_DENIED = 40301  # 当前 Provider 无权管理指定本体
    ONTOLOGY_INACTIVE = 40302  # 本体已被禁用或吊销
    ENTITY_LIMIT_EXCEEDED = 40303  # 实体数量已达本体配额上限
    ONTOLOGY_NOT_FOUND = 40401  # 本体 AIC 不存在
    ENDPOINT_CONFLICT = 40901  # 服务端点 URL 与已有实体冲突

    # 其他 ATR 场景下使用的补充错误码
    AGENT_NOT_FOUND = 40410
    AGENT_INACTIVE = 40310
    AGENT_UNSUPPORTED = 40411
    AGENT_ACS_MISSING = 40412
    GENERATE_AIC_FAILED = 50001
    DATABASE_ERROR = 50002
    INTERNAL_ERROR = 50000


class AtrError(AcpsError):
    """供 ATR 协议处理器使用的专用异常类型。"""

    def __init__(
        self,
        *,
        code: AtrErrorCode,
        message: str,
        http_status: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            protocol="atr",
            code=int(code),
            message=message,
            http_status=http_status,
            data=data,
        )
