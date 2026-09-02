import uuid
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.account.exception_account import AccountError, AccountErrorCode
from app.account.model import User
from app.core.config import settings
from app.core.crypto import generate_sm3_salt, sm3_hash, sm4_decrypt, sm4_encrypt
from app.utils.utils import get_beijing_time
from app.verification.exception import (
    IdentityAlreadyVerifiedError,
    IdentityVerificationPendingError,
    IdentityVerificationRequiredError,
    OrganizationAlreadyVerifiedError,
    OrganizationVerificationPendingError,
    OrgRequiresIdentityApprovedError,
    VerificationAlreadyDecidedError,
    VerificationCurrentRecordConflictError,
    VerificationDecryptFailedError,
    VerificationRecordNotFoundError,
    VerificationRejectRemarkRequiredError,
    VerificationStaleRecordError,
)
from app.verification.model import (
    IdentityVerification,
    OrgVerification,
    VerificationMethod,
    VerificationStatus,
)

if TYPE_CHECKING:
    from app.verification.schema import IdentityVerificationRequest, OrgVerificationRequest


type VerificationWhereClause = ColumnElement[bool]


IDENTITY_USER_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", IdentityVerification.user_id)
IDENTITY_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", IdentityVerification.id)
IDENTITY_DELETED_AT_COLUMN = cast("QueryableAttribute[datetime | None]", IdentityVerification.deleted_at)
IDENTITY_CREATED_AT_COLUMN = cast("QueryableAttribute[datetime]", IdentityVerification.created_at)
ORG_USER_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", OrgVerification.user_id)
ORG_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", OrgVerification.id)
ORG_DELETED_AT_COLUMN = cast("QueryableAttribute[datetime | None]", OrgVerification.deleted_at)
ORG_CREATED_AT_COLUMN = cast("QueryableAttribute[datetime]", OrgVerification.created_at)
USER_ID_COLUMN = cast("QueryableAttribute[uuid.UUID]", User.id)


def _as_verification_clause(value: ColumnElement[bool] | bool) -> VerificationWhereClause:
    return cast("VerificationWhereClause", value)


def _hash_with_salt(value: str) -> str:
    salt = generate_sm3_salt()
    return f"{salt}${sm3_hash(value, salt)}"


def _ensure_identity_submission_allowed(user: User, latest_record: IdentityVerification | None) -> None:
    if user.identity_verified or (latest_record and latest_record.status == VerificationStatus.APPROVED):
        raise IdentityAlreadyVerifiedError(user_id=str(user.id))

    if latest_record and latest_record.status == VerificationStatus.PENDING:
        raise IdentityVerificationPendingError(
            user_id=str(user.id),
            verification_id=str(latest_record.id),
        )


def _ensure_org_submission_allowed(user: User, latest_record: OrgVerification | None) -> None:
    if user.org_verified or (latest_record and latest_record.status == VerificationStatus.APPROVED):
        raise OrganizationAlreadyVerifiedError(user_id=str(user.id))

    if latest_record and latest_record.status == VerificationStatus.PENDING:
        raise OrganizationVerificationPendingError(
            user_id=str(user.id),
            verification_id=str(latest_record.id),
        )

    if not user.identity_verified:
        raise IdentityVerificationRequiredError(user_id=str(user.id))


def _auto_approve_identity_record(user: User, record: IdentityVerification, now: datetime) -> None:
    record.status = VerificationStatus.APPROVED
    record.decided_at = now
    user.identity_verified = True
    user.identity_verified_at = now
    user.current_identity_id = record.id
    user.updated_at = now


def _auto_approve_org_record(user: User, record: OrgVerification, now: datetime) -> None:
    record.status = VerificationStatus.APPROVED
    record.decided_at = now
    user.org_verified = True
    user.org_verified_at = now
    user.current_org_id = record.id
    user.updated_at = now


async def _get_latest_identity_verification(session: AsyncSession, user_id: uuid.UUID) -> IdentityVerification | None:
    stmt = _build_latest_identity_verification_stmt(user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_latest_org_verification(session: AsyncSession, user_id: uuid.UUID) -> OrgVerification | None:
    stmt = _build_latest_org_verification_stmt(user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_latest_identity_verification_stmt(user_id: uuid.UUID) -> Select[tuple[IdentityVerification]]:
    return (
        select(IdentityVerification)
        .where(
            _as_verification_clause(user_id == IDENTITY_USER_ID_COLUMN),
            IDENTITY_DELETED_AT_COLUMN.is_(None),
        )
        .order_by(IDENTITY_CREATED_AT_COLUMN.desc())
        .limit(1)
    )


def _build_latest_org_verification_stmt(user_id: uuid.UUID) -> Select[tuple[OrgVerification]]:
    return (
        select(OrgVerification)
        .where(
            _as_verification_clause(user_id == ORG_USER_ID_COLUMN),
            ORG_DELETED_AT_COLUMN.is_(None),
        )
        .order_by(ORG_CREATED_AT_COLUMN.desc())
        .limit(1)
    )


async def _get_latest_identity_verification_for_update(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> IdentityVerification | None:
    result = await session.execute(_build_latest_identity_verification_stmt(user_id).with_for_update())
    return result.scalar_one_or_none()


async def _get_latest_org_verification_for_update(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> OrgVerification | None:
    result = await session.execute(_build_latest_org_verification_stmt(user_id).with_for_update())
    return result.scalar_one_or_none()


async def _get_identity_verification_by_id_for_update(
    session: AsyncSession,
    verification_id: uuid.UUID,
) -> IdentityVerification:
    stmt = (
        select(IdentityVerification)
        .where(
            _as_verification_clause(IDENTITY_ID_COLUMN == verification_id),  # noqa: SIM300
            IDENTITY_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise VerificationRecordNotFoundError(verification_id=str(verification_id))
    return record


async def _get_org_verification_by_id_for_update(
    session: AsyncSession,
    verification_id: uuid.UUID,
) -> OrgVerification:
    stmt = (
        select(OrgVerification)
        .where(
            _as_verification_clause(ORG_ID_COLUMN == verification_id),  # noqa: SIM300
            ORG_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise VerificationRecordNotFoundError(verification_id=str(verification_id))
    return record


async def _get_user_for_update(session: AsyncSession, user_id: uuid.UUID) -> User:
    stmt = select(User).where(_as_verification_clause(USER_ID_COLUMN == user_id)).limit(1).with_for_update()  # noqa: SIM300
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError(
            status_code=404,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )
    return user


async def _get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    stmt = select(User).where(_as_verification_clause(USER_ID_COLUMN == user_id)).limit(1)  # noqa: SIM300
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError(
            status_code=404,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )
    return user


def _ensure_pending_record(status_value: VerificationStatus, verification_id: uuid.UUID) -> None:
    if status_value != VerificationStatus.PENDING:
        raise VerificationAlreadyDecidedError(
            verification_id=str(verification_id),
            status_value=status_value.value,
        )


def _ensure_reject_remark(remark: str | None, verification_id: uuid.UUID) -> str:
    normalized = remark.strip() if isinstance(remark, str) else ""
    if not normalized:
        raise VerificationRejectRemarkRequiredError(verification_id=str(verification_id))
    return normalized


async def _ensure_latest_identity_record(
    session: AsyncSession,
    record: IdentityVerification,
) -> None:
    latest = await _get_latest_identity_verification_for_update(session, record.user_id)
    if latest is None:
        raise VerificationRecordNotFoundError(verification_id=str(record.id))
    if latest.id != record.id:
        raise VerificationStaleRecordError(
            verification_id=str(record.id),
            latest_verification_id=str(latest.id),
        )


async def _ensure_latest_org_record(session: AsyncSession, record: OrgVerification) -> None:
    latest = await _get_latest_org_verification_for_update(session, record.user_id)
    if latest is None:
        raise VerificationRecordNotFoundError(verification_id=str(record.id))
    if latest.id != record.id:
        raise VerificationStaleRecordError(
            verification_id=str(record.id),
            latest_verification_id=str(latest.id),
        )


def _ensure_identity_current_record_conflict(user: User, record: IdentityVerification) -> None:
    if user.current_identity_id is not None and user.current_identity_id != record.id:
        raise VerificationCurrentRecordConflictError(
            user_id=str(user.id),
            current_verification_id=str(user.current_identity_id),
            attempted_verification_id=str(record.id),
        )


def _ensure_org_current_record_conflict(user: User, record: OrgVerification) -> None:
    if user.current_org_id is not None and user.current_org_id != record.id:
        raise VerificationCurrentRecordConflictError(
            user_id=str(user.id),
            current_verification_id=str(user.current_org_id),
            attempted_verification_id=str(record.id),
        )


async def _ensure_user_identity_is_approved(
    session: AsyncSession,
    user: User,
) -> IdentityVerification:
    if not user.identity_verified or user.current_identity_id is None:
        raise OrgRequiresIdentityApprovedError(user_id=str(user.id))

    stmt = (
        select(IdentityVerification)
        .where(
            _as_verification_clause(IDENTITY_ID_COLUMN == user.current_identity_id),  # noqa: SIM300
            IDENTITY_DELETED_AT_COLUMN.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None or record.user_id != user.id or record.status != VerificationStatus.APPROVED:
        raise OrgRequiresIdentityApprovedError(
            user_id=str(user.id),
            current_identity_id=str(user.current_identity_id),
        )
    return record


def decrypt_identity_real_name(record: IdentityVerification) -> str:
    try:
        return sm4_decrypt(record.real_name_encrypted, settings.sm4_encryption_key)
    except Exception as exc:
        raise VerificationDecryptFailedError(
            verification_id=str(record.id),
            field_name="real_name",
        ) from exc


def decrypt_org_legal_rep_name(record: OrgVerification) -> str | None:
    if not record.legal_rep_name_encrypted:
        return None

    try:
        return sm4_decrypt(record.legal_rep_name_encrypted, settings.sm4_encryption_key)
    except Exception as exc:
        raise VerificationDecryptFailedError(
            verification_id=str(record.id),
            field_name="legal_rep_name",
        ) from exc


async def submit_identity_verification(
    session: AsyncSession,
    user: User,
    request: IdentityVerificationRequest,
) -> IdentityVerification:
    latest_record = await _get_latest_identity_verification(session, user.id)
    _ensure_identity_submission_allowed(user, latest_record)

    now = get_beijing_time()
    record = IdentityVerification(
        user_id=user.id,
        id_type=request.id_type,
        id_number_hash=_hash_with_salt(request.id_number),
        real_name_encrypted=sm4_encrypt(request.real_name, settings.sm4_encryption_key),
        method=VerificationMethod.AUTO,
        provider=("AUTO_APPROVE" if settings.auto_approve_identity_verification else None),
        status=VerificationStatus.PENDING,
    )

    if settings.auto_approve_identity_verification:
        _auto_approve_identity_record(user, record, now)
        session.add(user)

    session.add(record)
    await session.flush()
    return record


async def submit_org_verification(
    session: AsyncSession,
    user: User,
    request: OrgVerificationRequest,
) -> OrgVerification:
    latest_record = await _get_latest_org_verification(session, user.id)
    _ensure_org_submission_allowed(user, latest_record)

    now = get_beijing_time()
    record = OrgVerification(
        user_id=user.id,
        org_name=request.org_name,
        usci=request.usci,
        org_registration_number=request.org_registration_number,
        legal_rep_name_encrypted=(
            sm4_encrypt(request.legal_rep_name, settings.sm4_encryption_key) if request.legal_rep_name else None
        ),
        legal_rep_id_hash=(_hash_with_salt(request.legal_rep_id_number) if request.legal_rep_id_number else None),
        method=VerificationMethod.AUTO,
        provider="AUTO_APPROVE" if settings.auto_approve_org_verification else None,
        status=VerificationStatus.PENDING,
    )

    if settings.auto_approve_org_verification:
        _auto_approve_org_record(user, record, now)
        session.add(user)

    session.add(record)
    await session.flush()
    return record


async def get_identity_verification_status(
    session: AsyncSession,
    user: User,
) -> IdentityVerification | None:
    return await _get_latest_identity_verification(session, user.id)


async def get_org_verification_status(session: AsyncSession, user: User) -> OrgVerification | None:
    return await _get_latest_org_verification(session, user.id)


async def get_admin_identity_verification_detail(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> IdentityVerification | None:
    await _get_user(session, user_id)
    return await _get_latest_identity_verification(session, user_id)


async def get_admin_org_verification_detail(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> OrgVerification | None:
    await _get_user(session, user_id)
    return await _get_latest_org_verification(session, user_id)


async def approve_identity_verification(
    session: AsyncSession,
    verification_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    remark: str | None = None,
) -> IdentityVerification:
    record = await _get_identity_verification_by_id_for_update(session, verification_id)
    user = await _get_user_for_update(session, record.user_id)
    await _ensure_latest_identity_record(session, record)
    _ensure_pending_record(record.status, record.id)
    _ensure_identity_current_record_conflict(user, record)

    now = get_beijing_time()
    record.method = VerificationMethod.MANUAL
    record.status = VerificationStatus.APPROVED
    record.reviewer_id = reviewer_id
    record.decided_at = now
    record.remark = remark
    record.updated_at = now

    user.identity_verified = True
    user.identity_verified_at = now
    user.current_identity_id = record.id
    user.updated_at = now

    session.add(record)
    session.add(user)
    await session.flush()
    return record


async def reject_identity_verification(
    session: AsyncSession,
    verification_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    remark: str | None,
) -> IdentityVerification:
    record = await _get_identity_verification_by_id_for_update(session, verification_id)
    await _ensure_latest_identity_record(session, record)
    _ensure_pending_record(record.status, record.id)

    now = get_beijing_time()
    record.method = VerificationMethod.MANUAL
    record.status = VerificationStatus.REJECTED
    record.reviewer_id = reviewer_id
    record.decided_at = now
    record.remark = _ensure_reject_remark(remark, record.id)
    record.updated_at = now

    session.add(record)
    await session.flush()
    return record


async def approve_org_verification(
    session: AsyncSession,
    verification_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    remark: str | None = None,
) -> OrgVerification:
    record = await _get_org_verification_by_id_for_update(session, verification_id)
    user = await _get_user_for_update(session, record.user_id)
    await _ensure_latest_org_record(session, record)
    _ensure_pending_record(record.status, record.id)
    _ensure_org_current_record_conflict(user, record)
    await _ensure_user_identity_is_approved(session, user)

    now = get_beijing_time()
    record.method = VerificationMethod.MANUAL
    record.status = VerificationStatus.APPROVED
    record.reviewer_id = reviewer_id
    record.decided_at = now
    record.remark = remark
    record.updated_at = now

    user.org_verified = True
    user.org_verified_at = now
    user.current_org_id = record.id
    user.updated_at = now

    session.add(record)
    session.add(user)
    await session.flush()
    return record


async def reject_org_verification(
    session: AsyncSession,
    verification_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    remark: str | None,
) -> OrgVerification:
    record = await _get_org_verification_by_id_for_update(session, verification_id)
    await _ensure_latest_org_record(session, record)
    _ensure_pending_record(record.status, record.id)

    now = get_beijing_time()
    record.method = VerificationMethod.MANUAL
    record.status = VerificationStatus.REJECTED
    record.reviewer_id = reviewer_id
    record.decided_at = now
    record.remark = _ensure_reject_remark(remark, record.id)
    record.updated_at = now

    session.add(record)
    await session.flush()
    return record
