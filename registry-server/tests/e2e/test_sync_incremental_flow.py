"""黑盒 E2E：DSP changes 增量同步流。"""

from __future__ import annotations

import json

import pytest

from tests.support.aic import generate_aic
from tests.support.constants import DEFAULT_LOGIN_VALUE
from tests.support.database import create_agent_with_change_log, create_user

pytestmark = pytest.mark.e2e


def _parse_ndjson(payload: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


async def test_incremental_changes_flow_advances_seq_and_times_out(client, db_session, e2e_run_id: str) -> None:
    owner = await create_user(
        db_session,
        username=f"sync-owner-{e2e_run_id}",
        password=DEFAULT_LOGIN_VALUE,
        name="Sync Owner",
    )
    first_aic = generate_aic()
    second_aic = generate_aic()

    await create_agent_with_change_log(
        db_session,
        aic=first_aic,
        name=f"Sync Agent A {e2e_run_id}",
        created_by=owner,
        end_points=[{"url": "https://sync-a.example.com", "transport": "JSONRPC", "security": []}],
    )
    await create_agent_with_change_log(
        db_session,
        aic=second_aic,
        name=f"Sync Agent B {e2e_run_id}",
        created_by=owner,
        end_points=[{"url": "https://sync-b.example.com", "transport": "JSONRPC", "security": []}],
    )
    await db_session.commit()

    first_response = await client.get("/acps-dsp-v2/changes", params={"limit": 1, "types": "acs"})
    assert first_response.status_code == 200
    assert first_response.headers["X-Next-Seq"] == "1"

    first_batch = _parse_ndjson(first_response.text)
    assert len(first_batch) == 1
    assert first_batch[0]["id"] == first_aic

    second_response = await client.get(
        "/acps-dsp-v2/changes",
        params={"seq": 1, "limit": 10, "types": "acs"},
    )
    assert second_response.status_code == 200
    assert second_response.headers["X-Next-Seq"] == "2"

    second_batch = _parse_ndjson(second_response.text)
    assert len(second_batch) == 1
    assert second_batch[0]["id"] == second_aic

    timeout_response = await client.get(
        "/acps-dsp-v2/changes",
        params={"seq": 2, "limit": 10, "types": "acs", "wait": "1"},
    )
    assert timeout_response.status_code == 204
    assert timeout_response.headers["X-Next-Seq"] == "2"
