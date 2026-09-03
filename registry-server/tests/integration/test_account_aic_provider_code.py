"""真实数据库：aic_provider_code 可空唯一约束。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.account.model import User
from tests.support.database import create_user

pytestmark = pytest.mark.integration


async def test_multiple_users_can_share_null_aic_provider_code(db_session) -> None:
    first = await create_user(db_session, username=f"null-a-{uuid.uuid4().hex[:8]}")
    second = await create_user(db_session, username=f"null-b-{uuid.uuid4().hex[:8]}")
    await db_session.flush()

    assert first.aic_provider_code is None
    assert second.aic_provider_code is None


async def test_duplicate_non_null_aic_provider_code_is_rejected(db_session) -> None:
    first = await create_user(db_session, username=f"dup-a-{uuid.uuid4().hex[:8]}")
    second = await create_user(db_session, username=f"dup-b-{uuid.uuid4().hex[:8]}")
    first.aic_provider_code = "34C2"
    await db_session.flush()

    second.aic_provider_code = "34C2"
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_user_model_accepts_leading_zero_provider_code(db_session) -> None:
    user = await create_user(db_session, username=f"zero-{uuid.uuid4().hex[:8]}")
    user.aic_provider_code = "0001"
    await db_session.flush()

    persisted = await db_session.get(User, user.id)
    assert persisted is not None
    assert persisted.aic_provider_code == "0001"
