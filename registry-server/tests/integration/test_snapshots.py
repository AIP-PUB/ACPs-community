"""真实数据库集成测试：snapshot 主路径。"""

import json

import pytest
from sqlalchemy import select

from app.agent.model import Agent
from app.sync.model import Snapshot
from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.integration


async def test_snapshot_create_chunk_and_delete_round_trip(client, db_session) -> None:
    creator = await create_user(db_session, username="snapshot-owner", password=DEFAULT_LOGIN_VALUE)
    await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name="Snapshot Agent 1",
        created_by=creator,
        end_points=[{"url": "https://agent-1.example.com", "transport": "JSONRPC", "security": []}],
    )
    await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name="Snapshot Agent 2",
        created_by=creator,
        end_points=[{"url": "https://agent-2.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    create_response = await client.get("/acps-dsp-v2/snapshots", params={"types": "acs", "limit": 1})

    assert create_response.status_code == 200
    snapshot_id = create_response.headers["X-Snapshot-Id"]
    assert create_response.headers["X-Snapshot-Chunk-Index"] == "0"
    assert create_response.headers["X-Snapshot-Chunk-Total"] == "2"
    assert create_response.headers["X-Snapshot-Object-Count"] == "2"

    first_chunk = [json.loads(line) for line in create_response.text.splitlines() if line.strip()]
    assert len(first_chunk) == 1

    second_chunk_response = await client.get(
        "/acps-dsp-v2/snapshots",
        params={"id": snapshot_id, "chunk": 1, "limit": 1},
    )

    assert second_chunk_response.status_code == 200
    second_chunk = [json.loads(line) for line in second_chunk_response.text.splitlines() if line.strip()]
    assert len(second_chunk) == 1
    assert first_chunk[0]["id"] != second_chunk[0]["id"]

    db_session.expire_all()
    snapshot = await db_session.get(Snapshot, snapshot_id)
    assert snapshot is not None
    assert snapshot.object_count == 2

    delete_response = await client.delete(f"/acps-dsp-v2/snapshots/{snapshot_id}")
    assert delete_response.status_code == 204

    db_session.expire_all()
    deleted_snapshot = await db_session.get(Snapshot, snapshot_id)
    assert deleted_snapshot is not None
    assert deleted_snapshot.is_deleted is True

    agent_result = await db_session.execute(select(Agent).order_by(Agent.aic))
    assert len(list(agent_result.scalars().all())) == 2
