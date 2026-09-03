"""真实数据库集成测试：sync admin 管理端点。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.account.model import RoleType
from app.sync.model import ChangeLog, Snapshot
from app.utils.utils import get_beijing_time
from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user
from tests.support.http import response_json_string_map

pytestmark = pytest.mark.integration


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _login(client, *, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response_json_string_map(response)


async def test_staff_can_list_inspect_and_cleanup_snapshots(client, db_session) -> None:
    staff = await create_user(
        db_session,
        username="sync-admin-staff",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Sync Admin Staff",
    )
    owner = await create_user(
        db_session,
        username="sync-snapshot-owner",
        password=DEFAULT_LOGIN_VALUE,
        name="Snapshot Owner",
    )
    await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name="Admin Snapshot Agent",
        created_by=owner,
        end_points=[{"url": "https://snapshot.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    create_response = await client.get("/acps-dsp-v2/snapshots", params={"types": "acs", "limit": 1})
    assert create_response.status_code == 200
    snapshot_id = create_response.headers["X-Snapshot-Id"]

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(staff_tokens["access_token"])

    list_response = await client.get("/acps-dsp-v2/admin/snapshots", headers=headers)
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert list_payload["items"][0]["id"] == snapshot_id

    info_response = await client.get(f"/acps-dsp-v2/admin/snapshots/{snapshot_id}", headers=headers)
    assert info_response.status_code == 200
    assert info_response.json()["id"] == snapshot_id
    assert info_response.json()["object_count"] == 1

    db_session.expire_all()
    snapshot = await db_session.get(Snapshot, snapshot_id)
    assert snapshot is not None
    snapshot.expire_at = get_beijing_time() - timedelta(minutes=1)
    db_session.add(snapshot)
    await db_session.commit()

    cleanup_response = await client.post("/acps-dsp-v2/admin/snapshots/cleanup", headers=headers)
    assert cleanup_response.status_code == 200
    assert cleanup_response.json()["cleaned_count"] == 1


async def test_staff_can_list_and_cleanup_changelogs(client, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    staff = await create_user(
        db_session,
        username="sync-changelog-staff",
        password=DEFAULT_LOGIN_VALUE,
        roles=(RoleType.STAFF,),
        name="Changelog Staff",
    )
    db_session.add_all(
        [
            ChangeLog(
                seq=1,
                ts=get_beijing_time() - timedelta(hours=2),
                type="acs",
                op="upsert",
                id="agent-old",
                version=1,
                payload={"aic": "agent-old", "active": True},
            ),
            ChangeLog(
                seq=2,
                ts=get_beijing_time(),
                type="acs",
                op="upsert",
                id="agent-new",
                version=1,
                payload={"aic": "agent-new", "active": True},
            ),
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(
        "app.sync.api_admin.settings",
        SimpleNamespace(dsp_retention_window_hours=1, dsp_retention_max_records=100),
    )

    staff_tokens = await _login(client, username=staff.username or "", password=DEFAULT_LOGIN_VALUE)
    headers = _auth_headers(staff_tokens["access_token"])

    list_response = await client.get("/acps-dsp-v2/admin/changelogs", headers=headers, params={"data_type": "acs"})
    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["total"] == 2
    assert payload["items"][0]["seq"] == 2
    assert payload["items"][1]["seq"] == 1

    cleanup_response = await client.post("/acps-dsp-v2/admin/changelogs/cleanup", headers=headers)
    assert cleanup_response.status_code == 200
    cleanup_payload = cleanup_response.json()
    assert cleanup_payload["cleaned_count"] == 1
    assert cleanup_payload["retention_config"] == {"window_hours": 1, "max_records": 100}
