"""DISBAND / LEAVE_GROUP 的延迟退出不能在消费回调任务里关闭自己的通道。

aiormq 的 Channel 在 _on_deliver_frame 里用 create_task(consumer(message)) 运行消费回调，
close() 时经 FutureStore.reject_all() 取消这些任务。这里用 aiormq 自己的 FutureStore
模拟同样的任务归属与关闭语义，不需要 RabbitMQ。
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from aiormq.base import FutureStore

from acps_sdk.aip.aip_group_model import GroupMgmtCommand, GroupMgmtCommandType
from acps_sdk.aip.aip_group_partner import GroupPartnerMqClient, PartnerGroupState

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
GROUP_ID = "group-1"


class _Channel:
    def __init__(self) -> None:
        self.store = FutureStore(asyncio.get_running_loop())
        self.is_closed = False

    async def close(self) -> None:
        self.is_closed = True
        await self.store.reject_all(asyncio.CancelledError)


class _Exchange:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, message, routing_key: str = "") -> None:
        self.published.append(json.loads(message.body))


class _Message:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode()
        self.headers: dict = {}
        self.redelivered = False
        self.routing_key = ""

    @contextlib.asynccontextmanager
    async def process(self):
        yield


class _Queue:
    name = f"group_{LEADER_AIC}_{GROUP_ID}_{PARTNER_AIC}"

    def __init__(self, channel: _Channel) -> None:
        self.channel = channel
        self.callback = None

    async def consume(self, callback):
        self.callback = callback
        return "ctag-1"

    async def cancel(self, consumer_tag) -> None:
        return None

    async def delete(self, **kwargs) -> None:
        return None

    def deliver(self, message: _Message):
        return self.channel.store.create_task(self.callback(message))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, mentions",
    [
        (GroupMgmtCommandType.DISBAND, "all"),
        (GroupMgmtCommandType.LEAVE_GROUP, [PARTNER_AIC]),
    ],
)
async def test_leave_requested_by_leader_reaches_disconnected(command, mentions) -> None:
    client = GroupPartnerMqClient(partner_aic=PARTNER_AIC, identity_binding_enabled=False)
    channel = _Channel()
    queue = _Queue(channel)
    exchange = _Exchange()
    client._channel, client._queue, client._exchange = channel, queue, exchange
    client._group_id = GROUP_ID
    client._exchange_name = f"group_{LEADER_AIC}_{GROUP_ID}"
    client._state = PartnerGroupState.JOINED
    left: list[str] = []
    client.set_disconnect_handler(lambda _client, group_id: left.append(group_id))
    await client._start_consuming()

    mgmt = GroupMgmtCommand(
        id="mgmt-1",
        sentAt="2026-01-01T00:00:00Z",
        senderRole="leader",
        senderId=LEADER_AIC,
        dataItems=[],
        command=command,
        mentions=mentions,
        groupId=GROUP_ID,
        sessionId="session-1",
    )
    consumer_task = queue.deliver(_Message(mgmt.model_dump(exclude_none=True)))
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(consumer_task.task), 2)
    for _ in range(50):
        if client.state == PartnerGroupState.DISCONNECTED:
            break
        await asyncio.sleep(0.01)

    assert client.state == PartnerGroupState.DISCONNECTED
    assert left == [GROUP_ID]
    assert channel.is_closed
    statuses = [m["status"]["connected"] for m in exchange.published if m.get("type") == "group-mgmt-result"]
    assert statuses == [False]
