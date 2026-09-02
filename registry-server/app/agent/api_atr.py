from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.model import RoleType, User
from app.agent.exception import AtrError, AtrErrorCode
from app.agent.model import ApprovalStatus
from app.agent.schema import EntityRegistrationRequest, EntityRegistrationResponse
from app.agent.service import register_entity_async
from app.agent.service_query import get_agent_by_aic_async
from app.core.auth import get_current_user
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.config import settings
from app.core.db_session import get_session
from app.utils.aic import is_ontology_aic, validate_aic

# 为 Agent Trusted Registration 协议创建 ATR 路由
router_public = APIRouter()
router_mtls = APIRouter()
router = router_public
DbSession = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
TEST_PEER_AIC_HEADER = "X-ATR-Test-Peer-AIC"


def _problem_response(description: str) -> dict[str, object]:
    return {"description": description, "content": {PROBLEM_JSON_MEDIA_TYPE: {}}}


BAD_REQUEST_RESPONSE = _problem_response("ATR request is invalid")
UNAUTHORIZED_RESPONSE = _problem_response("ATR authentication failed")
FORBIDDEN_RESPONSE = _problem_response("ATR access denied")
NOT_FOUND_RESPONSE = _problem_response("ATR resource not found")
CONFLICT_RESPONSE = _problem_response("ATR entity registration conflict")
VALIDATION_RESPONSE = _problem_response("Request validation failed")


def _has_staff_or_admin_role(user: User) -> bool:
    return any(role.name in {RoleType.STAFF, RoleType.ADMIN} for role in user.roles)


def _ensure_entity_registration_access(ontology_owner_id: object, current_user: User, ontology_aic: str) -> None:
    if ontology_owner_id == current_user.id or _has_staff_or_admin_role(current_user):
        return

    raise AtrError(
        code=AtrErrorCode.ACCESS_DENIED,
        message="Current user is not allowed to manage the specified ontology",
        http_status=status.HTTP_403_FORBIDDEN,
        data={
            "ontologyAic": ontology_aic,
            "userId": str(current_user.id),
        },
    )


def _resolve_peer_ontology_aic(http_request: Request) -> str | None:
    if settings.app_env == "testing":
        header_value = http_request.headers.get(TEST_PEER_AIC_HEADER, "").strip()
        if header_value:
            return header_value.upper()

    peer_common_name = getattr(http_request.state, "peer_common_name", None)
    if isinstance(peer_common_name, str) and peer_common_name.strip():
        return peer_common_name.strip().upper()

    return None


def _ensure_entity_certificate_matches(http_request: Request, ontology_aic: str) -> None:
    peer_ontology_aic = _resolve_peer_ontology_aic(http_request)
    if peer_ontology_aic is None:
        raise AtrError(
            code=AtrErrorCode.UNAUTHORIZED,
            message="Valid ontology client certificate is required",
            http_status=status.HTTP_401_UNAUTHORIZED,
            data={"ontologyAic": ontology_aic},
        )

    if not validate_aic(peer_ontology_aic):
        raise AtrError(
            code=AtrErrorCode.UNAUTHORIZED,
            message="Client certificate identity is invalid",
            http_status=status.HTTP_401_UNAUTHORIZED,
            data={"ontologyAic": ontology_aic},
        )

    if not is_ontology_aic(peer_ontology_aic):
        raise AtrError(
            code=AtrErrorCode.ACCESS_DENIED,
            message="Entity or service certificates are not allowed on this endpoint",
            http_status=status.HTTP_403_FORBIDDEN,
            data={
                "ontologyAic": ontology_aic,
                "certificateOntologyAic": peer_ontology_aic,
            },
        )

    if peer_ontology_aic != ontology_aic:
        raise AtrError(
            code=AtrErrorCode.ACCESS_DENIED,
            message="Client certificate ontology AIC does not match request ontology",
            http_status=status.HTTP_403_FORBIDDEN,
            data={
                "ontologyAic": ontology_aic,
                "certificateOntologyAic": peer_ontology_aic,
            },
        )


# -------------------------------------------------------------------
# ATR 端点 - Agent Trusted Registration API
# -------------------------------------------------------------------


@router_public.get(
    "/acs/{agent_aic}",
    status_code=status.HTTP_200_OK,
    summary="通过 AIC 获取 Agent ACS",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def get_agent_acs_by_aic(
    agent_aic: str,
    db: DbSession,
) -> JSONResponse:
    """
    通过 AIC 获取 Agent 的 ACS 信息（ATR 协议接口）

    API 端点: GET {REGISTRY_SERVER_BASE_URL}/acs/{agent_aic}

    根据 ATR-Registry-Server.md 规范：
    - 200: 返回 AgentInfo（ACS 结构）
    - 404: agent_aic 对应的智能体不存在
    - 403: agent_aic 对应的智能体非 active 状态

    响应数据格式是ACS结构。
    """
    # 验证 AIC 格式
    if not validate_aic(agent_aic):
        raise AtrError(
            code=AtrErrorCode.INVALID_REQUEST,
            message="Invalid AIC format or checksum",
            http_status=status.HTTP_400_BAD_REQUEST,
            data={"agentAic": agent_aic},
        )

    # 根据 AIC 查询 Agent
    agent = await get_agent_by_aic_async(db, agent_aic, raise_exception=False)
    if not agent:
        raise AtrError(
            code=AtrErrorCode.AGENT_NOT_FOUND,
            message="Agent not found with the provided AIC",
            http_status=status.HTTP_404_NOT_FOUND,
            data={"agentAic": agent_aic},
        )

    # 检查是否有 ACS 数据
    if not agent.acs:
        raise AtrError(
            code=AtrErrorCode.AGENT_ACS_MISSING,
            message="Agent ACS not found",
            http_status=status.HTTP_404_NOT_FOUND,
            data={"agentAic": agent_aic},
        )

    # ACS 现在是 JSONB 类型，直接使用 dict
    atr_response = agent.acs

    # 如果 Agent 不是 active 状态，返回 403 Forbidden
    if atr_response.get("active") is not True:
        raise AtrError(
            code=AtrErrorCode.AGENT_INACTIVE,
            message="Agent status is not active",
            http_status=status.HTTP_403_FORBIDDEN,
            data={
                "agentAic": agent_aic,
                "active": atr_response.get("active"),
            },
        )

    return JSONResponse(content=atr_response)


@router_mtls.post(
    "/entity",
    response_model=EntityRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册新的 ATR 实体 Agent",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def register_entity_endpoint(
    request: EntityRegistrationRequest,
    http_request: Request,
    db: DbSession,
    current_user: CurrentUserDep,
) -> JSONResponse:
    """
    注册新的智能体实体（ATR 协议接口）

    API 端点: POST {REGISTRY_SERVER_BASE_URL}/entity

    根据 ATR-Registry-Server.md 规范：
    - 请求方通过 mTLS 认证（使用本体证书）
    - 本体 AIC 必须存在且处于 active 状态
    - 系统为新实体分配唯一的实体 AIC
    - 基于本体 ACS 和请求中的增量信息，创建实体 ACS

    请求体 (EntityRegistrationRequest):
    - ontologyAic: 本体 AIC（必填）
    - endPoints: 实体的服务端点列表（可选）
    - entityMeta: 实体的额外元数据（可选）
    - certificate: 实体证书签发配置（可选，写入实体 ACS）

    响应体 (EntityRegistrationResponse):
    - status: "ok" | "error"
    - result: { ontologyAic, entityAic, endPoints, entityMeta, certificate }
    - error: { code, message, data } (仅当 status 为 "error" 时)

    响应码:
    - 201: 注册成功
    - 400: 请求参数格式错误或缺少必填字段
    - 401: mTLS 认证失败（证书无效或未提供）
    - 403: 本体已被禁用或吊销 / 实体数量已达配额上限
    - 404: 本体 AIC 不存在
    - 409: 服务端点 URL 与已有实体冲突
    """
    # 验证 ontologyAic 格式
    ontology_aic = request.ontologyAic.strip().upper()
    if not validate_aic(ontology_aic):
        raise AtrError(
            code=AtrErrorCode.INVALID_REQUEST,
            message="Invalid ontology AIC format or checksum",
            http_status=status.HTTP_400_BAD_REQUEST,
            data={"ontologyAic": ontology_aic},
        )

    # 验证是否为本体 AIC（实例序列号应为全 0；长度取决于 AIC 规范/实现）
    if not is_ontology_aic(ontology_aic):
        raise AtrError(
            code=AtrErrorCode.INVALID_REQUEST,
            message="The provided AIC is not an ontology AIC (instance serial should be all zeros)",
            http_status=status.HTTP_400_BAD_REQUEST,
            data={"ontologyAic": ontology_aic},
        )

    _ensure_entity_certificate_matches(http_request, ontology_aic)

    ontology_agent = await get_agent_by_aic_async(db, ontology_aic, raise_exception=False)
    if ontology_agent is None:
        raise AtrError(
            code=AtrErrorCode.ONTOLOGY_NOT_FOUND,
            message="Ontology AIC does not exist",
            http_status=status.HTTP_404_NOT_FOUND,
            data={"ontologyAic": ontology_aic},
        )

    if not ontology_agent.is_ontology:
        raise AtrError(
            code=AtrErrorCode.INVALID_REQUEST,
            message="The specified AIC is not an ontology",
            http_status=status.HTTP_400_BAD_REQUEST,
            data={"ontologyAic": ontology_aic},
        )

    if (
        not ontology_agent.is_active
        or ontology_agent.is_disabled
        or ontology_agent.is_deleted
        or ontology_agent.approval_status != ApprovalStatus.APPROVED
    ):
        raise AtrError(
            code=AtrErrorCode.ONTOLOGY_INACTIVE,
            message="Ontology is inactive, disabled, deleted or not approved",
            http_status=status.HTTP_403_FORBIDDEN,
            data={
                "ontologyAic": ontology_aic,
                "isActive": ontology_agent.is_active,
                "isDisabled": ontology_agent.is_disabled,
                "isDeleted": ontology_agent.is_deleted,
                "approvalStatus": ontology_agent.approval_status.value,
            },
        )

    _ensure_entity_registration_access(ontology_agent.created_by_id, current_user, ontology_aic)

    # 转换 endPoints 为字典列表（如果提供）
    end_points = None
    if request.endPoints:
        end_points = [ep.model_dump() for ep in request.endPoints]

    certificate = None
    if request.certificate is not None:
        dumped_certificate = request.certificate.model_dump(mode="json", exclude_none=True)
        certificate = dumped_certificate or None

    # 调用服务层进行实体注册
    result = await register_entity_async(
        session=db,
        ontology_aic=ontology_aic,
        end_points=end_points,
        entity_meta=request.entityMeta,
        entity_user_id=request.entityUserId,
        certificate=certificate,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "status": "ok",
            "result": result,
        },
    )
