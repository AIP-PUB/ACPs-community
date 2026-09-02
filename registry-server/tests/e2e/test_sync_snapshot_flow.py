"""黑盒 E2E：DSP snapshot 全量恢复并衔接后续增量。"""

from __future__ import annotations

import json

import pytest

from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.e2e


def _parse_ndjson(payload: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


async def test_snapshot_flow_supports_chunk_pull_and_incremental_resume(client, db_session, e2e_run_id: str) -> None:
    owner = await create_user(
        db_session,
        username=f"snapshot-owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="Snapshot Owner",
    )

    await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name=f"Snapshot Agent A {e2e_run_id}",
        created_by=owner,
        end_points=[{"url": "https://snapshot-a.example.com", "transport": "JSONRPC", "security": []}],
    )
    await create_agent_with_change_log(
        db_session,
        aic=generate_aic(),
        name=f"Snapshot Agent B {e2e_run_id}",
        created_by=owner,
        end_points=[{"url": "https://snapshot-b.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    create_response = await client.get("/acps-dsp-v2/snapshots", params={"types": "acs", "limit": 1})
    assert create_response.status_code == 200

    snapshot_id = create_response.headers["X-Snapshot-Id"]
    snapshot_seq = int(create_response.headers["X-Snapshot-Seq"])
    assert create_response.headers["X-Snapshot-Chunk-Total"] == "2"

    first_chunk = _parse_ndjson(create_response.text)
    assert len(first_chunk) == 1

    second_chunk_response = await client.get(
        "/acps-dsp-v2/snapshots",
        params={"id": snapshot_id, "chunk": 1, "limit": 1},
    )
    assert second_chunk_response.status_code == 200

    second_chunk = _parse_ndjson(second_chunk_response.text)
    assert len(second_chunk) == 1
    assert first_chunk[0]["id"] != second_chunk[0]["id"]

    later_aic = generate_aic()
    await create_agent_with_change_log(
        db_session,
        aic=later_aic,
        name=f"Snapshot Agent C {e2e_run_id}",
        created_by=owner,
        end_points=[{"url": "https://snapshot-c.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    incremental_response = await client.get(
        "/acps-dsp-v2/changes",
        params={"seq": snapshot_seq, "limit": 10, "types": "acs"},
    )
    assert incremental_response.status_code == 200

    incremental_batch = _parse_ndjson(incremental_response.text)
    assert [item["id"] for item in incremental_batch] == [later_aic]

    delete_response = await client.delete(f"/acps-dsp-v2/snapshots/{snapshot_id}")
    assert delete_response.status_code == 204
