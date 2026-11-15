"""解散广播之后的等待期里，会话仍可路由：成员回的「已断开」、Leader 自己那条 DISBAND 的回声照常记录。"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from acps_sdk.aip.aip_group_leader import (
    GroupLeader,
    GroupLeaderMqClient,
    GroupLeaderSession,
    GroupLeaderState,
)
from acps_sdk.aip.aip_group_model import (
    GroupMemberStatus,
    GroupMgmtCommand,
    GroupMgmtCommandType,
    GroupMgmtResult,
)

LEADER = "leader-aic"
PARTNER = "partner-aic"


def _leader_with_session(acl: bool = False):
    leader = GroupLeader(LEADER, {"host": "mq.example", "port": 5671, "vhost": "acps"})
    if acl:
        leader._group_acl_client = MagicMock()
        leader._group_acl_client.remove_member = AsyncMock()
        leader._group_acl_client.delete_group = AsyncMock()
    client = GroupLeaderMqClient(leader_aic=LEADER)
    client._state = GroupLeaderState.READY
    client.cleanup_group_resources = AsyncMock()
    session = GroupLeaderSession("session-1", "group-1", client)
    leader.group_sessions["session-1"] = session
    return leader, session, client


def _echoes():
    disband = GroupMgmtCommand(
        id="mgmt-1", sentAt="2026-01-01T00:00:00Z", senderRole="leader", senderId=LEADER, dataItems=[],
        command=GroupMgmtCommandType.DISBAND, mentions="all", groupId="group-1", sessionId="session-1",
    )
    left = GroupMgmtResult(
        id="mgmt-r-1", sentAt="2026-01-01T00:00:01Z", senderRole="partner", senderId=PARTNER, dataItems=[],
        status=GroupMemberStatus(connected=False, muted=False), groupId="group-1", sessionId="session-1",
    )
    return disband, left


def test_messages_during_disband_grace_period_are_routed(caplog) -> None:
    leader, session, client = _leader_with_session(acl=True)
    disband, left = _echoes()

    async def broadcast() -> None:
        # 广播之后、等待期内到达的两条：自己那条 DISBAND 的回声、成员回的「已断开」
        await leader._handle_group_message(disband)
        await leader._handle_group_message(left)

    client.broadcast_group_shutdown = broadcast

    with caplog.at_level(logging.WARNING):
        asyncio.run(leader.dissolve_group_session("session-1"))

    assert "Cannot route message" not in caplog.text
    assert session.message_history == [disband, left]
    assert "session-1" not in leader.group_sessions
    assert client.state == GroupLeaderState.DISCONNECTED
    # 解散中不逐个删成员 ACL，整组删一次
    leader._group_acl_client.remove_member.assert_not_awaited()
    leader._group_acl_client.delete_group.assert_awaited_once_with(leader_aic=LEADER, group_id="group-1")


def test_session_is_removed_even_if_broadcast_raises() -> None:
    leader, session, client = _leader_with_session()
    client.broadcast_group_shutdown = AsyncMock(side_effect=RuntimeError("channel gone"))

    try:
        asyncio.run(leader.dissolve_group_session("session-1"))
    except RuntimeError:
        pass

    assert "session-1" not in leader.group_sessions


def test_concurrent_dissolve_runs_teardown_once() -> None:
    leader, session, client = _leader_with_session()
    calls: list[str] = []

    async def broadcast() -> None:
        calls.append("broadcast")
        await asyncio.sleep(0.05)

    client.broadcast_group_shutdown = broadcast

    async def main() -> None:
        await asyncio.gather(
            leader.dissolve_group_session("session-1"),
            leader.dissolve_group_session("session-1"),
        )

    asyncio.run(main())

    assert calls == ["broadcast"]
    assert client.cleanup_group_resources.await_count == 1
    assert "session-1" not in leader.group_sessions
