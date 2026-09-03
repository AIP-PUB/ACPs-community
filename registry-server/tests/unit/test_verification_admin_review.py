from __future__ import annotations

import asyncio
import uuid
from typing import cast
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.model import User
from app.utils.utils import get_beijing_time
from app.verification import service as verification_service
from app.verification.exception import VerificationErrorCode
from app.verification.model import (
    IdentityDocumentType,
    IdentityVerification,
    OrgVerification,
    VerificationMethod,
    VerificationStatus,
)
from app.verification.schema import VerificationDecisionRequest, VerificationRejectRequest

pytestmark = pytest.mark.unit


class DummyExecuteResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class DummyAsyncSession:
    def __init__(self) -> None:
        self._queue: list[object | None] = []
        self.added: list[object] = []
        self.flushed = False

    def queue_result(self, value: object | None) -> None:
        self._queue.append(value)

    def add(self, item: object) -> None:
        self.added.append(item)

    async def execute(self, statement: object) -> DummyExecuteResult:
        del statement
        await asyncio.sleep(0)
        return DummyExecuteResult(self._queue.pop(0) if self._queue else None)

    async def flush(self) -> None:
        await asyncio.sleep(0)
        self.flushed = True


def _as_async_session(session: DummyAsyncSession) -> AsyncSession:
    return cast("AsyncSession", session)


def _build_user() -> User:
    now = get_beijing_time()
    return User(
        id=uuid.uuid4(),
        username=f"user-{uuid.uuid4().hex[:8]}",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _build_identity_record(
    user_id: uuid.UUID,
    *,
    status: VerificationStatus = VerificationStatus.PENDING,
) -> IdentityVerification:
    now = get_beijing_time()
    return IdentityVerification(
        id=uuid.uuid4(),
        user_id=user_id,
        id_type=IdentityDocumentType.CN_ID_CARD,
        id_number_hash="salt$hash",
        real_name_encrypted="cipher",
        method=VerificationMethod.AUTO,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _build_org_record(
    user_id: uuid.UUID,
    *,
    status: VerificationStatus = VerificationStatus.PENDING,
) -> OrgVerification:
    now = get_beijing_time()
    return OrgVerification(
        id=uuid.uuid4(),
        user_id=user_id,
        org_name="ACPs Org",
        usci="91310000123456789X",
        legal_rep_name_encrypted="cipher",
        legal_rep_id_hash="salt$hash",
        method=VerificationMethod.AUTO,
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_verification_decision_request_trims_blank_remark_to_none() -> None:
    payload = VerificationDecisionRequest(remark="   ")
    assert payload.remark is None


def test_verification_reject_request_rejects_blank_remark() -> None:
    with pytest.raises(ValidationError):
        VerificationRejectRequest(remark="   ")


async def test_get_admin_identity_verification_detail_returns_none_when_user_has_no_record() -> None:
    session = DummyAsyncSession()
    user = _build_user()
    session.queue_result(user)
    session.queue_result(None)

    record = await verification_service.get_admin_identity_verification_detail(_as_async_session(session), user.id)

    assert record is None


async def test_approve_identity_verification_updates_user_and_record() -> None:
    session = DummyAsyncSession()
    user = _build_user()
    record = _build_identity_record(user.id)
    session.queue_result(record)
    session.queue_result(user)
    session.queue_result(record)

    result = await verification_service.approve_identity_verification(
        _as_async_session(session),
        verification_id=record.id,
        reviewer_id=uuid.uuid4(),
        remark="materials checked",
    )

    assert result.status == VerificationStatus.APPROVED
    assert result.method == VerificationMethod.MANUAL
    assert result.remark == "materials checked"
    assert user.identity_verified is True
    assert user.current_identity_id == record.id
    assert session.flushed is True


async def test_reject_identity_verification_requires_non_blank_remark() -> None:
    session = DummyAsyncSession()
    user = _build_user()
    record = _build_identity_record(user.id)
    session.queue_result(record)
    session.queue_result(record)

    with pytest.raises(Exception) as exc_info:
        await verification_service.reject_identity_verification(
            _as_async_session(session),
            verification_id=record.id,
            reviewer_id=uuid.uuid4(),
            remark="   ",
        )

    assert getattr(exc_info.value, "error_name", None) == VerificationErrorCode.VERIFICATION_REJECT_REMARK_REQUIRED


async def test_approve_identity_verification_rejects_stale_record() -> None:
    session = DummyAsyncSession()
    user = _build_user()
    target_record = _build_identity_record(user.id)
    latest_record = _build_identity_record(user.id)
    session.queue_result(target_record)
    session.queue_result(user)
    session.queue_result(latest_record)

    with pytest.raises(Exception) as exc_info:
        await verification_service.approve_identity_verification(
            _as_async_session(session),
            verification_id=target_record.id,
            reviewer_id=uuid.uuid4(),
        )

    assert getattr(exc_info.value, "error_name", None) == VerificationErrorCode.VERIFICATION_STALE_RECORD


async def test_approve_org_verification_requires_valid_identity() -> None:
    session = DummyAsyncSession()
    user = _build_user()
    org_record = _build_org_record(user.id)
    session.queue_result(org_record)
    session.queue_result(user)
    session.queue_result(org_record)

    with pytest.raises(Exception) as exc_info:
        await verification_service.approve_org_verification(
            _as_async_session(session),
            verification_id=org_record.id,
            reviewer_id=uuid.uuid4(),
            remark="org ok",
        )

    assert getattr(exc_info.value, "error_name", None) == VerificationErrorCode.ORG_REQUIRES_IDENTITY_APPROVED


def test_decrypt_identity_real_name_raises_when_ciphertext_is_invalid() -> None:
    record = _build_identity_record(uuid.uuid4())

    with (
        patch("app.verification.service.settings") as mocked_settings,
        patch("app.verification.service.sm4_decrypt", side_effect=ValueError("bad cipher")),
        pytest.raises(Exception) as exc_info,
    ):
        mocked_settings.sm4_encryption_key = "0123456789abcdeffedcba9876543210"
        verification_service.decrypt_identity_real_name(record)

    assert getattr(exc_info.value, "error_name", None) == VerificationErrorCode.VERIFICATION_DECRYPT_FAILED
