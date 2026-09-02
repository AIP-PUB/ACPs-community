from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.model import RoleType, User
from app.core.auth import check_user_role
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.db_session import get_session
from app.verification.schema import (
    AdminIdentityVerificationResponse,
    AdminOrgVerificationResponse,
    IdentityVerificationRequest,
    IdentityVerificationResponse,
    OrgVerificationRequest,
    OrgVerificationResponse,
    VerificationDecisionRequest,
    VerificationRejectRequest,
)

if TYPE_CHECKING:
    from app.verification.model import IdentityVerification, OrgVerification
from app.verification.service import (
    approve_identity_verification,
    approve_org_verification,
    decrypt_identity_real_name,
    decrypt_org_legal_rep_name,
    get_admin_identity_verification_detail,
    get_admin_org_verification_detail,
    get_identity_verification_status,
    get_org_verification_status,
    reject_identity_verification,
    reject_org_verification,
    submit_identity_verification,
    submit_org_verification,
)

router = APIRouter(prefix="/verification", tags=["verification"])

DbSession = Annotated[AsyncSession, Depends(get_session)]
CurrentClientUser = Annotated[User, Depends(check_user_role([RoleType.CLIENT]))]
AdminOrStaffUser = Annotated[User, Depends(check_user_role([RoleType.STAFF, RoleType.ADMIN]))]


def _to_identity_verification_response(record: IdentityVerificationResponse | object) -> IdentityVerificationResponse:
    return IdentityVerificationResponse.model_validate(record)


def _to_org_verification_response(record: OrgVerificationResponse | object) -> OrgVerificationResponse:
    return OrgVerificationResponse.model_validate(record)


def _to_admin_identity_verification_response(record: IdentityVerification) -> AdminIdentityVerificationResponse:
    verification_record = IdentityVerificationResponse.model_validate(record)
    return AdminIdentityVerificationResponse(
        **verification_record.model_dump(),
        real_name=decrypt_identity_real_name(record),
        provider_request_id=getattr(record, "provider_request_id", None),
        reviewer_id=getattr(record, "reviewer_id", None),
        attachment_urls=getattr(record, "attachment_urls", None),
    )


def _to_admin_org_verification_response(record: OrgVerification) -> AdminOrgVerificationResponse:
    verification_record = OrgVerificationResponse.model_validate(record)
    return AdminOrgVerificationResponse(
        **verification_record.model_dump(),
        legal_rep_name=decrypt_org_legal_rep_name(record),
        provider_request_id=getattr(record, "provider_request_id", None),
        reviewer_id=getattr(record, "reviewer_id", None),
        attachment_urls=getattr(record, "attachment_urls", None),
    )


def _problem_response(description: str) -> dict[str, object]:
    return {"description": description, "content": {PROBLEM_JSON_MEDIA_TYPE: {}}}


UNAUTHORIZED_RESPONSE = _problem_response("Authentication required")
FORBIDDEN_RESPONSE = _problem_response("Insufficient permissions")
CONFLICT_RESPONSE = _problem_response("Verification state conflict")
VALIDATION_RESPONSE = _problem_response("Request validation failed")


@router.post(
    "/identity",
    status_code=status.HTTP_201_CREATED,
    summary="提交身份审核申请",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def create_identity_verification(
    request: IdentityVerificationRequest,
    db: DbSession,
    current_user: CurrentClientUser,
) -> IdentityVerificationResponse:
    record = await submit_identity_verification(db, current_user, request)
    return _to_identity_verification_response(record)


@router.get(
    "/identity",
    status_code=status.HTTP_200_OK,
    summary="查询最新身份审核状态",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
    },
)
async def read_identity_verification(
    db: DbSession,
    current_user: CurrentClientUser,
) -> IdentityVerificationResponse | None:
    record = await get_identity_verification_status(db, current_user)
    return _to_identity_verification_response(record) if record else None


@router.post(
    "/org",
    status_code=status.HTTP_201_CREATED,
    summary="提交组织审核申请",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def create_org_verification(
    request: OrgVerificationRequest,
    db: DbSession,
    current_user: CurrentClientUser,
) -> OrgVerificationResponse:
    record = await submit_org_verification(db, current_user, request)
    return _to_org_verification_response(record)


@router.get(
    "/org",
    status_code=status.HTTP_200_OK,
    summary="查询最新组织审核状态",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
    },
)
async def read_org_verification(
    db: DbSession,
    current_user: CurrentClientUser,
) -> OrgVerificationResponse | None:
    record = await get_org_verification_status(db, current_user)
    return _to_org_verification_response(record) if record else None


@router.get(
    "/admin/users/{user_id}/identity",
    status_code=status.HTTP_200_OK,
    summary="管理端查询用户最新个人实名认证详情",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("User not found"),
    },
)
async def admin_read_identity_verification(
    user_id: uuid.UUID,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminIdentityVerificationResponse | None:
    del current_user
    record = await get_admin_identity_verification_detail(db, user_id=user_id)
    return _to_admin_identity_verification_response(record) if record else None


@router.post(
    "/admin/identity/{verification_id}/approve",
    status_code=status.HTTP_200_OK,
    summary="管理端批准个人实名认证",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("Verification not found"),
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def admin_approve_identity_verification(
    verification_id: uuid.UUID,
    request: VerificationDecisionRequest,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminIdentityVerificationResponse:
    record = await approve_identity_verification(
        db,
        verification_id=verification_id,
        reviewer_id=current_user.id,
        remark=request.remark,
    )
    return _to_admin_identity_verification_response(record)


@router.post(
    "/admin/identity/{verification_id}/reject",
    status_code=status.HTTP_200_OK,
    summary="管理端驳回个人实名认证",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("Verification not found"),
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def admin_reject_identity_verification(
    verification_id: uuid.UUID,
    request: VerificationRejectRequest,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminIdentityVerificationResponse:
    record = await reject_identity_verification(
        db,
        verification_id=verification_id,
        reviewer_id=current_user.id,
        remark=request.remark,
    )
    return _to_admin_identity_verification_response(record)


@router.get(
    "/admin/users/{user_id}/org",
    status_code=status.HTTP_200_OK,
    summary="管理端查询用户最新组织认证详情",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("User not found"),
    },
)
async def admin_read_org_verification(
    user_id: uuid.UUID,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminOrgVerificationResponse | None:
    del current_user
    record = await get_admin_org_verification_detail(db, user_id=user_id)
    return _to_admin_org_verification_response(record) if record else None


@router.post(
    "/admin/org/{verification_id}/approve",
    status_code=status.HTTP_200_OK,
    summary="管理端批准组织认证",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("Verification not found"),
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def admin_approve_org_verification(
    verification_id: uuid.UUID,
    request: VerificationDecisionRequest,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminOrgVerificationResponse:
    record = await approve_org_verification(
        db,
        verification_id=verification_id,
        reviewer_id=current_user.id,
        remark=request.remark,
    )
    return _to_admin_org_verification_response(record)


@router.post(
    "/admin/org/{verification_id}/reject",
    status_code=status.HTTP_200_OK,
    summary="管理端驳回组织认证",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: _problem_response("Verification not found"),
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def admin_reject_org_verification(
    verification_id: uuid.UUID,
    request: VerificationRejectRequest,
    db: DbSession,
    current_user: AdminOrStaffUser,
) -> AdminOrgVerificationResponse:
    record = await reject_org_verification(
        db,
        verification_id=verification_id,
        reviewer_id=current_user.id,
        remark=request.remark,
    )
    return _to_admin_org_verification_response(record)
