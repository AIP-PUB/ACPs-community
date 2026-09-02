"""真实数据库集成测试：sync protocol 边界。"""

from __future__ import annotations

import json
import uuid

import pytest

from app.sync import api_protocol
from app.sync.model import ChangeLog
from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.integration


async def test_changes_wait_timeout_returns_204_with_next_seq(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        ChangeLog(
            seq=1,
            type="acs",
            op="upsert",
            id="agent-1",
            version=1,
            payload={"aic": "agent-1", "active": True},
        )
    )
    await db_session.commit()

    monkeypatch.setattr(api_protocol, "POLL_INTERVAL_SECONDS", 0.05)

    response = await client.get(
        "/acps-dsp-v2/changes",
        params={"seq": 1, "limit": 10, "types": "acs", "wait": "1"},
    )

    assert response.status_code == 204
    assert response.text == ""
    assert response.headers["X-Next-Seq"] == "1"


async def test_snapshot_from_seq_returns_only_incremental_records(client, db_session) -> None:
    creator = await create_user(
        db_session,
        username=f"snapshot-incremental-{uuid.uuid4().hex[:8]}",
        password=DEFAULT_LOGIN_VALUE,
    )
    first_agent = await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name="Snapshot Incremental Agent 1",
        created_by=creator,
        end_points=[{"url": "https://agent-1.example.com", "transport": "JSONRPC", "security": []}],
    )
    second_agent = await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name="Snapshot Incremental Agent 2",
        created_by=creator,
        end_points=[{"url": "https://agent-2.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    response = await client.get(
        "/acps-dsp-v2/snapshots",
        params={"types": "acs", "limit": 10, "from_seq": first_agent.acs_last_seq},
    )

    assert response.status_code == 200
    assert response.headers["X-Snapshot-Chunk-Index"] == "0"
    assert response.headers["X-Snapshot-Chunk-Total"] == "1"
    assert response.headers["X-Snapshot-Object-Count"] == "1"

    chunk = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert len(chunk) == 1
    assert chunk[0]["id"] == second_agent.aic
    assert chunk[0]["seq"] == second_agent.acs_last_seq
