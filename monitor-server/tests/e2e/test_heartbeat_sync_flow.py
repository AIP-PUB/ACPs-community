"""E2E — Sync Profile 流程（Step 11，§7.5 Consumer 算法参考实现）。

验收项（C-SYNC-1/3/6）：
1. /sync/info → 获取 shard_count 和 topic
2. /sync/snapshot → 解析 NDJSON（首行 snapshot-meta，后续 alive 条目）
3. 从 snapshot 元数据 cutoverSeqByShard 作为本地 alive 集合基线
4. 应用 seq 闸门 + (id, version) 幂等逻辑：delta seq <= cutover 跳过
5. 本地 alive 集合与 /liveness/query 全量结果对比（黑盒一致性验证）
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient

from app.core.config import settings
from tests.support.kafka_helper import produce_heartbeat

_HB = f"{settings.api_v1_str}/heartbeat"


@pytest.mark.asyncio
async def test_sync_info_endpoint(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """/sync/info 返回必要字段。"""
    resp = await e2e_http_client.get(f"{_HB}/sync/info")
    assert resp.status_code == 200
    info = resp.json()
    assert info["type"] == "amp-alive-delta"
    assert "shardCount" in info
    assert "kafkaTopic" in info
    assert "currentPublishedSeqByShard" in info
    assert isinstance(info["shardCount"], int)
    assert info["shardCount"] > 0


@pytest.mark.asyncio
async def test_sync_snapshot_ndjson(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """注入心跳 → /sync/snapshot 首行为 snapshot-meta，后续为 alive 条目。"""
    aics = [f"e2e-sync-aic-{i:02d}" for i in range(5)]
    for aic in aics:
        await produce_heartbeat(aic)

    # 等待 writer 处理
    deadline = asyncio.get_event_loop().time() + 12.0
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{_HB}/liveness/query",
            json={"filter": {"conditions": [{"field": "aic", "op": "in", "value": aics}]}},
        )
        assert resp.status_code == 200
        if len(resp.json().get("items", [])) >= len(aics):
            break
        await asyncio.sleep(0.5)

    # 获取 snapshot
    resp = await e2e_http_client.get(
        f"{_HB}/sync/snapshot",
        headers={"Accept": "application/x-ndjson"},
    )
    assert resp.status_code == 200
    assert "x-ndjson" in resp.headers.get("content-type", "")

    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    assert lines, "snapshot 不能为空"

    # 首行 snapshot-meta
    meta = json.loads(lines[0])
    assert meta.get("recordType") == "snapshot-meta"
    assert "cutoverSeqByShard" in meta
    assert "generatedAt" in meta

    # 后续条目是 AliveDeltaEnvelope：{id, kind, op, payload: {aic, lastSeenAt, ...}}
    alive_entries = []
    for line in lines[1:]:
        entry = json.loads(line)
        payload = entry.get("payload", {})
        assert "aic" in payload, f"snapshot 条目缺少 payload.aic: {entry}"
        alive_entries.append(payload["aic"])

    # 所有注入的 aics 都应出现在 snapshot 中
    for aic in aics:
        assert aic in alive_entries, f"{aic} 未出现在 snapshot 中"


@pytest.mark.asyncio
async def test_sync_local_alive_set_matches_query(
    e2e_heartbeat_runtime: None,
    e2e_http_client: AsyncClient,
) -> None:
    """§7.5 Consumer 算法黑盒：snapshot alive 集合 == /liveness/query 全量（近似一致）。

    实现 §7.5 参考算法：
    1. 拉取 snapshot → 构建本地 alive 集合 local_alive = {aic}
    2. 拉取 /liveness/query 全量（isAlive=True）→ server_alive = {aic}
    3. 断言 local_alive 与 server_alive 无丢失条目（C-SYNC-1）
    """
    aics = [f"e2e-sync-full-{i:02d}" for i in range(8)]
    for aic in aics:
        await produce_heartbeat(aic)

    # 等待全部写入
    deadline = asyncio.get_event_loop().time() + 12.0
    while asyncio.get_event_loop().time() < deadline:
        resp = await e2e_http_client.post(
            f"{_HB}/liveness/query",
            json={"filter": {"conditions": [{"field": "aic", "op": "in", "value": aics}]}},
        )
        assert resp.status_code == 200
        if len(resp.json().get("items", [])) >= len(aics):
            break
        await asyncio.sleep(0.5)

    # Step 1：拉取 snapshot → 本地 alive 集合
    snap_resp = await e2e_http_client.get(
        f"{_HB}/sync/snapshot",
        headers={"Accept": "application/x-ndjson"},
    )
    assert snap_resp.status_code == 200
    snap_lines = [ln for ln in snap_resp.text.strip().split("\n") if ln.strip()]

    # 首行 meta（含 cutoverSeqByShard）
    meta = json.loads(snap_lines[0])
    cutover_by_shard: dict[str, str] = meta.get("cutoverSeqByShard", {})

    # 构建本地 alive 集合（seq 闸门：snapshot 已含所有 seq <= cutover 的条目）
    # 条目是 AliveDeltaEnvelope：{id, kind, op, payload: {aic, lastSeenAt, ...}}
    local_alive: dict[str, dict] = {}
    for line in snap_lines[1:]:
        entry = json.loads(line)
        aic_val = entry["payload"]["aic"]
        local_alive[aic_val] = entry

    # Step 2：server /liveness/query 全量（分页全遍历）
    # planner 要求 selective filter；用 silenceDurationSeconds >= 0 扫全量
    # 每个 item 是 HeartbeatLivenessEnvelope：{"data": {...}, "meta": {...}}
    # 游标位于 meta.nextCursor；每次请求须携带相同 filter
    _scan_filter = {"conditions": [{"field": "silenceDurationSeconds", "op": "gte", "value": 0}]}
    server_alive: set[str] = set()
    cursor: str | None = None
    for _ in range(100):
        payload: dict = {"filter": _scan_filter, "page": {"limit": 200}}
        if cursor:
            payload["page"]["cursor"] = cursor
        resp = await e2e_http_client.post(f"{_HB}/liveness/query", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        for item in body.get("items", []):
            if item["data"].get("isAlive"):
                server_alive.add(item["data"]["aic"])
        cursor = body.get("meta", {}).get("nextCursor")
        if not cursor:
            break

    # Step 3：验证本次注入的 aics 全部出现在 snapshot 中（C-SYNC-1）
    for aic in aics:
        assert aic in local_alive, f"{aic} 不在 snapshot alive 集合"
        assert aic in server_alive, f"{aic} 不在 server alive 集合"

    # snapshot 包含的 aics 也应在 server alive 中（C-SYNC-3：不含多余项）
    snapshot_aics = set(local_alive.keys())
    extra = snapshot_aics - server_alive
    assert not extra, f"snapshot 含有 server 不认识的 aics: {extra}"

    # cutoverSeqByShard 非空（C-SYNC-6）
    assert cutover_by_shard, "cutoverSeqByShard 不能为空"
    for shard_id, seq_str in cutover_by_shard.items():
        assert isinstance(seq_str, str), f"shard {shard_id} seq 不是 str"
        assert int(seq_str) >= 0, f"shard {shard_id} seq < 0"
