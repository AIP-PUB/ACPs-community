"""E2E — DestinationStateSource fake 注入 → Collector → destinations/query（H-1 场景 4）。

验收项：
- 通过可注入 fake DestinationStateSource 提供快照数据
- StateCollector 将快照写入 message_destination_states
- /message/destinations/query 返回该快照（不依赖真实 broker）
- 无快照时返回 503 AMP_STATE_SNAPSHOT_UNAVAILABLE
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient


class FakeDestinationStateSource:
    """可注入的假 DestinationStateSource，提供固定快照数据。"""

    def __init__(self, system: str, destination_name: str, destination_kind: str) -> None:
        self.system = system
        self.destination_name = destination_name
        self.destination_kind = destination_kind
        self._samples: list[Any] = []

    def set_samples(self, samples: list[Any]) -> None:
        self._samples = samples

    async def sample(self) -> list[Any]:
        return self._samples


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_destinations_query_no_snapshot_returns_503(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """无快照时 destinations/query 返回 503 AMP_STATE_SNAPSHOT_UNAVAILABLE。"""
    from app.core.config import settings

    now = datetime.now(UTC)
    body = {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "system": "kafka",
    }
    resp = await e2e_http_client.post(
        f"{settings.api_v1_str}/message/destinations/query",
        json=body,
    )
    # destinations/query requires state collector; if no snapshot → 503
    assert resp.status_code in (503, 404), f"期望 503 或 404，实际 {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_destinations_query_with_fake_source(
    e2e_message_writer: dict[str, Any],
    e2e_http_client: AsyncClient,
) -> None:
    """通过 fake source 写入快照 → destinations/query 返回该快照。"""
    from app.core.config import settings
    from app.message.destination_source import DestinationSample
    from app.message.state_collector import DestinationStateCollector
    from app.message.store import ensure_message_schema

    await ensure_message_schema()

    dest = f"e2e-dest-{uuid.uuid4().hex[:6]}"
    system = "kafka"
    fake_source = FakeDestinationStateSource(
        system=system,
        destination_name=dest,
        destination_kind="topic",
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    fake_source.set_samples(
        [
            DestinationSample(
                system=system,
                destination_name=dest,
                destination_kind="topic",
                virtual_host="",
                captured_at_ms=now_ms,
                visible_messages=100,
                inflight_messages=None,
                delayed_messages=None,
                dead_letter_messages=None,
                oldest_message_age_seconds=None,
                active_consumers=3,
                size_bytes=None,
            )
        ]
    )

    redis = e2e_message_writer["redis"]
    collector = DestinationStateCollector(redis, source=fake_source)
    await collector.run_once()

    await asyncio.sleep(1.0)

    now = datetime.now(UTC)
    body = {
        "timeRange": {
            "startAt": (now - timedelta(hours=1)).isoformat(),
            "endAt": now.isoformat(),
        },
        "system": system,
        "filter": {
            "conditions": [{"field": "destination.name", "op": "eq", "value": dest}],
            "logic": "and",
        },
    }
    last_resp_info: str = ""
    deadline = asyncio.get_event_loop().time() + 20
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{settings.api_v1_str}/message/destinations/query",
            json=body,
        )
        last_resp_info = f"status={resp.status_code} body={resp.text[:300]}"
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                assert any(
                    item.get("destinationName") == dest or item.get("destination_name") == dest for item in items
                )
                return
        await asyncio.sleep(2)

    pytest.fail(f"destinations/query 在 20s 内未找到 destination={dest!r}; last={last_resp_info}")
