import uuid
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.model import User
from app.agent import model as _agent_model
from app.core.config import settings as core_settings
from app.utils.utils import get_beijing_time
from app.verification import service as verification_service
from app.verification.exception import VerificationError, VerificationErrorCode
from app.verification.model import (
    IdentityDocumentType,
    IdentityVerification,
    OrgVerification,
    VerificationStatus,
)
from app.verification.schema import IdentityVerificationRequest, OrgVerificationRequest

pytestmark = pytest.mark.unit


del _agent_model


class DummyQuery:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def filter(self, *args: object, **kwargs: object) -> DummyQuery:
        del args, kwargs
        return self

    def order_by(self, *args: object, **kwargs: object) -> DummyQuery:
        del args, kwargs
        return self

    def first(self) -> object | None:
        return self.items[0] if self.items else None


class DummyAsyncResult:
    def __init__(self, item: object | None) -> None:
        self.item = item

    def scalar_one_or_none(self) -> object | None:
        return self.item


class DummyDb:
    def __init__(self) -> None:
        self.identity_records: list[IdentityVerification] = []
        self.org_records: list[OrgVerification] = []
        self.users: list[User] = []
        self.committed = False
        self.flushed = False

    def add(self, item: object) -> None:
        if isinstance(item, IdentityVerification):
            self.identity_records.insert(0, item)
        elif isinstance(item, OrgVerification):
            self.org_records.insert(0, item)
        elif isinstance(item, User):
            self.users.append(item)

    def commit(self) -> None:
        self.committed = True

    async def flush(self) -> None:
        self.flushed = True

    def refresh(self, item: object) -> None:
        del item
        return

    async def execute(self, statement: object) -> DummyAsyncResult:
        entity = cast("Any", statement).column_descriptions[0].get("entity")
        if entity is IdentityVerification:
            return DummyAsyncResult(self.identity_records[0] if self.identity_records else None)
        if entity is OrgVerification:
            return DummyAsyncResult(self.org_records[0] if self.org_records else None)
        return DummyAsyncResult(None)


def _as_async_session(db: DummyDb) -> AsyncSession:
    return cast("AsyncSession", db)


def _build_user() -> User:
    now = get_beijing_time()
    return User(
        id=uuid.uuid4(),
        username=f"user-{uuid.uuid4().hex[:8]}",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


async def test_submit_identity_verification_auto_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = _build_user()
    request = IdentityVerificationRequest(
        id_type=IdentityDocumentType.CN_ID_CARD,
        id_number="310101199001011234",
        real_name="Alice Zhang",
    )

    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_identity_verification",
        property(lambda self: True),
    )

    record = await verification_service.submit_identity_verification(_as_async_session(db), user, request)

    assert db.flushed is True
    assert db.committed is False
    assert record.status == VerificationStatus.APPROVED
    assert user.identity_verified is True
    assert user.current_identity_id == record.id
    assert record.real_name_encrypted != "Alice Zhang"
    assert "$" in record.id_number_hash


async def test_submit_identity_verification_rejects_existing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = DummyDb()
    user = _build_user()
    db.identity_records.append(
        IdentityVerification(
            user_id=user.id,
            id_type=IdentityDocumentType.CN_ID_CARD,
            id_number_hash="salt$hash",
            real_name_encrypted="cipher",
            status=VerificationStatus.PENDING,
        )
    )
    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_identity_verification",
        property(lambda self: False),
    )

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_identity_verification(
            _as_async_session(db),
            user,
            IdentityVerificationRequest(
                id_number="310101199001011234",
                real_name="Alice Zhang",
            ),
        )

    assert exc_info.value.error_name == VerificationErrorCode.IDENTITY_PENDING


async def test_submit_identity_verification_rejects_already_verified_user() -> None:
    db = DummyDb()
    user = _build_user()
    user.identity_verified = True

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_identity_verification(
            _as_async_session(db),
            user,
            IdentityVerificationRequest(
                id_number="310101199001011234",
                real_name="Alice Zhang",
            ),
        )

    assert exc_info.value.error_name == VerificationErrorCode.IDENTITY_ALREADY_VERIFIED


async def test_submit_identity_verification_rejects_latest_approved_record_with_stale_user_flag() -> None:
    db = DummyDb()
    user = _build_user()
    db.identity_records.append(
        IdentityVerification(
            user_id=user.id,
            id_type=IdentityDocumentType.CN_ID_CARD,
            id_number_hash="salt$hash",
            real_name_encrypted="cipher",
            status=VerificationStatus.APPROVED,
        )
    )

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_identity_verification(
            _as_async_session(db),
            user,
            IdentityVerificationRequest(
                id_number="310101199001011234",
                real_name="Alice Zhang",
            ),
        )

    assert exc_info.value.error_name == VerificationErrorCode.IDENTITY_ALREADY_VERIFIED


async def test_submit_org_verification_requires_identity_verified() -> None:
    db = DummyDb()
    user = _build_user()

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_org_verification(
            _as_async_session(db),
            user,
            OrgVerificationRequest(org_name="ACPs Org", usci="91310000123456789X"),
        )

    assert exc_info.value.error_name == VerificationErrorCode.IDENTITY_NOT_VERIFIED


async def test_submit_org_verification_auto_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = _build_user()
    user.identity_verified = True

    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_org_verification",
        property(lambda self: True),
    )

    record = await verification_service.submit_org_verification(
        _as_async_session(db),
        user,
        OrgVerificationRequest(
            org_name="ACPs Org",
            usci="91310000123456789X",
            legal_rep_name="Bob Li",
            legal_rep_id_number="310101199201019999",
        ),
    )

    assert db.flushed is True
    assert db.committed is False
    assert record.status == VerificationStatus.APPROVED
    assert user.org_verified is True
    assert user.current_org_id == record.id
    assert record.legal_rep_name_encrypted != "Bob Li"
    assert "$" in (record.legal_rep_id_hash or "")


async def test_submit_org_verification_rejects_already_verified_user() -> None:
    db = DummyDb()
    user = _build_user()
    user.identity_verified = True
    user.org_verified = True

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_org_verification(
            _as_async_session(db),
            user,
            OrgVerificationRequest(org_name="ACPs Org", usci="91310000123456789X"),
        )

    assert exc_info.value.error_name == VerificationErrorCode.ORG_ALREADY_VERIFIED


async def test_submit_org_verification_rejects_latest_approved_record_with_stale_user_flag() -> None:
    db = DummyDb()
    user = _build_user()
    user.identity_verified = True
    db.org_records.append(
        OrgVerification(
            user_id=user.id,
            org_name="ACPs Org",
            usci="91310000123456789X",
            status=VerificationStatus.APPROVED,
        )
    )

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_org_verification(
            _as_async_session(db),
            user,
            OrgVerificationRequest(org_name="ACPs Org", usci="91310000123456789X"),
        )

    assert exc_info.value.error_name == VerificationErrorCode.ORG_ALREADY_VERIFIED


async def test_submit_org_verification_rejects_existing_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = _build_user()
    user.identity_verified = True
    db.org_records.append(
        OrgVerification(
            user_id=user.id,
            org_name="ACPs Org",
            usci="91310000123456789X",
            status=VerificationStatus.PENDING,
        )
    )
    monkeypatch.setattr(
        type(core_settings),
        "auto_approve_org_verification",
        property(lambda self: False),
    )

    with pytest.raises(VerificationError) as exc_info:
        await verification_service.submit_org_verification(
            _as_async_session(db),
            user,
            OrgVerificationRequest(org_name="ACPs Org", usci="91310000123456789X"),
        )

    assert exc_info.value.error_name == VerificationErrorCode.ORG_PENDING


async def test_get_identity_verification_status_returns_latest_record() -> None:
    db = DummyDb()
    user = _build_user()
    record = IdentityVerification(
        user_id=user.id,
        id_type=IdentityDocumentType.CN_ID_CARD,
        id_number_hash="salt$hash",
        real_name_encrypted="cipher",
        status=VerificationStatus.PENDING,
    )
    db.identity_records.append(record)

    current_record = await verification_service.get_identity_verification_status(_as_async_session(db), user)

    assert current_record is record


def test_identity_verification_request_rejects_whitespace_only_required_fields() -> None:
    with pytest.raises(ValidationError):
        IdentityVerificationRequest(
            id_number="   ",
            real_name="  Alice Zhang  ",
        )


def test_org_verification_request_normalizes_blank_optional_fields() -> None:
    request = OrgVerificationRequest(
        org_name="  ACPs Org  ",
        usci="   ",
        org_registration_number="  REG-001  ",
        legal_rep_name="   ",
        legal_rep_id_number="  310101199201019999  ",
    )

    assert request.org_name == "ACPs Org"
    assert request.usci is None
    assert request.org_registration_number == "REG-001"
    assert request.legal_rep_name is None
    assert request.legal_rep_id_number == "310101199201019999"
