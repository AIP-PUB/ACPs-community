"""用 Python 版 acps_sdk.aip 群组模式生成跨语言对照样例（test/fixtures/python/aip_group/*.json）。

确定性：uuid4 / datetime.now / secrets.token_urlsafe / os.getpid / trace id 全部替换为常量，
AMQP 与 HTTP 交互由内存假对象记录。Node 测试以同样的常量重放并逐项比对。

运行：cd acps-sdk-node && uv run --project ../acps-sdk --python 3.12 python -I scripts/fixtures/gen_aip_group.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime as _real_datetime
from datetime import timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import write_fixture  # noqa: E402

import httpx  # noqa: E402

from acps_sdk.aip import aip_group_leader as leader_mod  # noqa: E402
from acps_sdk.aip import aip_group_model as model_mod  # noqa: E402
from acps_sdk.aip import aip_group_partner as partner_mod  # noqa: E402
from acps_sdk.aip import aip_group_runtime as runtime_mod  # noqa: E402
from acps_sdk.aip.aip_base_model import (  # noqa: E402
    Product,
    StructuredDataItem,
    TaskCommandType,
    TaskState,
    TextDataItem,
)
from acps_sdk.aip.aip_group_auth import GroupAclClient, GroupAclError  # noqa: E402
from acps_sdk.aip.aip_group_identity import assert_direct_group_request_identity  # noqa: E402
from acps_sdk.aip.aip_group_model import (  # noqa: E402
    ACSObject,
    AMQPConfig,
    GroupInfo,
    GroupInvitationError,
    GroupInvitationErrorData,
    GroupMemberStatus,
    GroupMgmtCommand,
    GroupMgmtCommandType,
    GroupMgmtResult,
    InboxGroupInvitation,
    InboxGroupInvitationError,
    RabbitMQRequest,
    RabbitMQRequestParams,
    RabbitMQResponse,
    RabbitMQServerConfig,
)
from acps_sdk.amp.trace_context import TraceContext  # noqa: E402

PKG = "aip_group"

LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"

FIXED_UUID = "12345678-1234-4678-9234-567812345678"
FIXED_NOW = _real_datetime(2026, 10, 19, 8, 1, 12, 160000, tzinfo=timezone.utc)
FIXED_TOKEN = "fixed-invitation-token"
FIXED_PID = 4242
FIXED_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
FIXED_SPAN_ID = "b7ad6b7169203331"


class _FixedDatetime(_real_datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


_GLOBAL_PATCH_TARGETS: list[tuple[Any, str, Any]] = []


def _patch(target: Any, name: str, value: Any) -> None:
    """记录原值后替换；main() 结束时统一恢复（统一入口会在同一进程里依次运行所有生成脚本）。"""
    _GLOBAL_PATCH_TARGETS.append((target, name, getattr(target, name)))
    setattr(target, name, value)


def _restore_patches() -> None:
    while _GLOBAL_PATCH_TARGETS:
        target, name, original = _GLOBAL_PATCH_TARGETS.pop()
        setattr(target, name, original)


def _install_determinism() -> None:
    _patch(uuid, "uuid4", lambda: uuid.UUID(FIXED_UUID))
    for module in (leader_mod, partner_mod, runtime_mod, model_mod):
        _patch(module, "datetime", _FixedDatetime)
    _patch(runtime_mod.secrets, "token_urlsafe", lambda _n=32: FIXED_TOKEN)
    _patch(partner_mod.os, "getpid", lambda: FIXED_PID)
    for module in (leader_mod, partner_mod):
        _patch(module, "new_trace_id", lambda: FIXED_TRACE_ID)
        _patch(module, "new_span_id", lambda: FIXED_SPAN_ID)
    _patch(leader_mod.aio_pika, "connect_robust", leader_mod.aio_pika.connect_robust)
    _patch(leader_mod.aio_pika, "connect", leader_mod.aio_pika.connect)
    _patch(leader_mod.httpx, "AsyncClient", leader_mod.httpx.AsyncClient)


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(exclude_none=True)


def _error_text(fn) -> str | None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


# =============================================================================
# AMQP 假对象（记录调用参数）
# =============================================================================


class Recorder:
    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []

    def add(self, op: str, **fields: Any) -> None:
        self.ops.append({"op": op, **fields})


def _message_record(message: Any, *, exchange: str, routing_key: str) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "routing_key": routing_key,
        "body": message.body.decode("utf-8"),
        "body_size": len(message.body),
        "content_type": message.content_type,
        "delivery_mode": int(message.delivery_mode),
        "headers": dict(message.headers or {}),
        "user_id": message.user_id,
        "priority": message.priority,
        "message_id": message.message_id,
    }


class FakeExchange:
    def __init__(self, recorder: Recorder, name: str) -> None:
        self.recorder = recorder
        self.name = name
        self.published: list[dict[str, Any]] = []

    async def publish(self, message: Any, routing_key: str, **_: Any) -> None:
        record = _message_record(message, exchange=self.name, routing_key=routing_key)
        self.published.append(record)
        self.recorder.add("publish", **record)


class FakeQueue:
    def __init__(self, recorder: Recorder, name: str) -> None:
        self.recorder = recorder
        self.name = name
        self.consumers: list[Any] = []

    async def bind(self, exchange: Any, routing_key: str | None = None, **kwargs: Any) -> None:
        self.recorder.add(
            "bind",
            queue=self.name,
            exchange=getattr(exchange, "name", exchange),
            routing_key=routing_key,
        )

    async def consume(self, callback: Any, **_: Any) -> str:
        self.consumers.append(callback)
        self.recorder.add("consume", queue=self.name)
        return f"ctag-{self.name}"

    async def cancel(self, consumer_tag: str) -> None:
        self.recorder.add("cancel", queue=self.name, consumer_tag=consumer_tag)

    async def delete(self, if_unused: bool = True, if_empty: bool = True) -> None:
        self.recorder.add("queue_object_delete", queue=self.name, if_unused=if_unused, if_empty=if_empty)


class FakeChannel:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.is_closed = False
        self.exchanges: dict[str, FakeExchange] = {}
        self.queues: dict[str, FakeQueue] = {}

    async def declare_exchange(
        self,
        name: str,
        type: Any = "direct",
        durable: bool = False,
        auto_delete: bool = False,
        internal: bool = False,
        passive: bool = False,
        arguments: Any = None,
        **_: Any,
    ) -> FakeExchange:
        self.recorder.add(
            "declare_exchange",
            name=name,
            type=getattr(type, "value", type),
            durable=durable,
            auto_delete=auto_delete,
            passive=passive,
        )
        exchange = self.exchanges.get(name) or FakeExchange(self.recorder, name)
        self.exchanges[name] = exchange
        return exchange

    async def get_exchange(self, name: str, *, ensure: bool = True) -> FakeExchange:
        self.recorder.add("get_exchange", name=name, ensure=ensure)
        exchange = self.exchanges.get(name) or FakeExchange(self.recorder, name)
        self.exchanges[name] = exchange
        return exchange

    async def declare_queue(
        self,
        name: str | None = None,
        *,
        durable: bool = False,
        exclusive: bool = False,
        passive: bool = False,
        auto_delete: bool = False,
        arguments: Any = None,
        **_: Any,
    ) -> FakeQueue:
        self.recorder.add(
            "declare_queue",
            name=name,
            durable=durable,
            exclusive=exclusive,
            auto_delete=auto_delete,
            arguments=arguments,
        )
        queue = FakeQueue(self.recorder, name or "")
        self.queues[queue.name] = queue
        return queue

    async def queue_delete(self, queue_name: str, if_unused: bool = False, if_empty: bool = False, **_: Any) -> None:
        self.recorder.add("queue_delete", name=queue_name, if_unused=if_unused, if_empty=if_empty)

    async def exchange_delete(self, exchange_name: str, if_unused: bool = False, **_: Any) -> None:
        self.recorder.add("exchange_delete", name=exchange_name, if_unused=if_unused)

    async def close(self) -> None:
        self.is_closed = True
        self.recorder.add("channel_close")


class FakeConnection:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.is_closed = False
        self.channels: list[FakeChannel] = []

    async def channel(self) -> FakeChannel:
        channel = FakeChannel(self.recorder)
        self.channels.append(channel)
        self.recorder.add("open_channel")
        return channel

    async def close(self) -> None:
        self.is_closed = True
        self.recorder.add("connection_close")


class FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def emit(self, body: Any, **kwargs: Any) -> str:
        self.calls.append(
            {"body": body.model_dump(mode="json", by_alias=True, exclude_none=True), "options": kwargs}
        )
        return "log-id"


class FakeIncoming:
    def __init__(self, body: bytes, *, user_id: str | None, headers: dict | None = None) -> None:
        self.body = body
        self.user_id = user_id
        self.headers = headers
        self.routing_key = ""
        self.redelivered = False

    def process(self):
        class _Ctx:
            async def __aenter__(self_inner):
                return self

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


# =============================================================================
# 样例
# =============================================================================


def _group_info() -> GroupInfo:
    return GroupInfo(
        groupId="group-session-1",
        leader=ACSObject(aic=LEADER_AIC),
        partners=[ACSObject(aic=PARTNER_AIC)],
    )


def _amqp_config() -> AMQPConfig:
    return AMQPConfig(exchange=f"group_{LEADER_AIC}_group-session-1", exchangeType="fanout", routingKey="")


def gen_models() -> None:
    group = _group_info().model_dump()
    amqp = _amqp_config().model_dump()

    mgmt_command = GroupMgmtCommand(
        id="mgmt-cmd-1",
        sentAt="2026-04-15T00:00:00+00:00",
        senderRole="leader",
        senderId=LEADER_AIC,
        dataItems=[],
        command=GroupMgmtCommandType.MUTE,
        mentions=[PARTNER_AIC],
        groupId="group-session-1",
        sessionId="session-1",
    )
    mgmt_result = GroupMgmtResult(
        id="mgmt-result-1",
        sentAt="2026-04-15T00:00:00+00:00",
        senderRole="partner",
        senderId=PARTNER_AIC,
        dataItems=[],
        status=GroupMemberStatus(connected=True, muted=False),
        groupId="group-session-1",
    )
    invitation = InboxGroupInvitation(
        id="invite-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="leader",
        senderId=LEADER_AIC,
        groupId="group-session-1",
        protocol="rabbitmq:4.2",
        expiresAt="2026-06-24T00:05:00Z",
        invitationToken="token-1",
        group=_group_info(),
        amqp=_amqp_config(),
    )
    legacy_invitation_input = {
        "type": "group-invitation",
        "id": "invite-legacy",
        "sentAt": "2026-06-24T00:00:00Z",
        "protocol": "rabbitmq:4.2",
        "expiresAt": "2026-06-24T00:05:00Z",
        "invitationToken": "token-1",
        "group": group,
        "amqp": amqp,
    }
    legacy_error_input = {
        "type": "group-invitation-error",
        "id": "invite-err-legacy",
        "sentAt": "2026-06-24T00:00:01Z",
        "groupId": "group-session-1",
        "partnerAic": PARTNER_AIC,
        "invitationToken": "token-1",
        "error": {
            "code": -32020,
            "message": "Invitation rejected by partner policy",
            "data": {"errorType": "INVITATION_REJECTED"},
        },
    }
    defaults_invitation_input = {
        "protocol": "rabbitmq:4.2",
        "expiresAt": "2026-06-24T00:05:00Z",
        "invitationToken": "token-1",
        "group": group,
        "amqp": amqp,
    }
    invitation_error = InboxGroupInvitationError(
        id="invite-err-1",
        sentAt="2026-06-24T00:00:00Z",
        senderRole="partner",
        senderId=PARTNER_AIC,
        groupId="group-session-1",
        invitationToken="token-1",
        error=GroupInvitationError(
            code=-32021,
            message="Connection failed",
            data=GroupInvitationErrorData(errorType="CONNECTION_FAILED", details={"host": "mq", "port": 5671}),
        ),
    )
    request = RabbitMQRequest(
        id="req-1",
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=_group_info(),
            server=RabbitMQServerConfig(host="mq.acps.example.com", port=5671, vhost="acps"),
            amqp=_amqp_config(),
        ),
    )
    request_with_token = RabbitMQRequest(
        id=7,
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=_group_info(),
            server=RabbitMQServerConfig(host="localhost", port=5672, vhost="/", accessToken="guest:guest"),
            amqp=_amqp_config(),
        ),
    )
    response_ok = RabbitMQResponse.model_validate(
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {
                "connectionName": f"agent-{PARTNER_AIC}",
                "vhost": "acps",
                "nodeName": "unknown",
                "queueName": f"group_{LEADER_AIC}_group-session-1_{PARTNER_AIC}",
                "processId": None,
            },
        }
    )
    response_error = RabbitMQResponse.model_validate(
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "error": {
                "code": -32021,
                "message": "Connection failed",
                "data": {"errorType": "CONNECTION_FAILED", "details": {"host": "h", "port": 1, "reason": None}},
            },
        }
    )

    invalid_cases = {
        "invitation_sender_mismatch": {
            **legacy_invitation_input,
            "senderRole": "leader",
            "senderId": PARTNER_AIC,
            "groupId": "group-session-1",
        },
        "invitation_group_mismatch": {**legacy_invitation_input, "senderId": LEADER_AIC, "groupId": "group-x"},
        "invitation_sender_role_partner": {**legacy_invitation_input, "senderRole": "partner"},
        "invitation_error_partner_mismatch": {**legacy_error_input, "senderId": PARTNER_AIC, "partnerAic": LEADER_AIC},
    }
    invalid = {}
    for name, payload in invalid_cases.items():
        model = InboxGroupInvitationError if payload.get("type") == "group-invitation-error" else InboxGroupInvitation
        invalid[name] = {"model": model.__name__, "input": payload, "error": _error_text(lambda: model.model_validate(payload))}

    mention_cases = []
    for mentions in (None, [], "all", [PARTNER_AIC], [OTHER_AIC], [PARTNER_AIC, OTHER_AIC]):
        cmd = GroupMgmtCommand(
            id="m",
            sentAt="t",
            senderRole="leader",
            senderId=LEADER_AIC,
            command=GroupMgmtCommandType.GET_STATUS,
            groupId="g",
            mentions=mentions,
        )
        mention_cases.append(
            {
                "mentions": mentions,
                "is_mentioned": cmd.is_mentioned(PARTNER_AIC),
                "must_respond": cmd.must_respond(PARTNER_AIC),
            }
        )

    write_fixture(
        PKG,
        "models",
        {
            "group_mgmt_command": _dump(mgmt_command),
            "group_mgmt_result": _dump(mgmt_result),
            "inbox_invitation": _dump(invitation),
            "legacy_invitation": {
                "input": legacy_invitation_input,
                "output": _dump(InboxGroupInvitation.model_validate(dict(legacy_invitation_input))),
            },
            "defaults_invitation": {
                "input": defaults_invitation_input,
                "output": _dump(InboxGroupInvitation.model_validate(dict(defaults_invitation_input))),
            },
            "legacy_invitation_error": {
                "input": legacy_error_input,
                "output": _dump(InboxGroupInvitationError.model_validate(dict(legacy_error_input))),
            },
            "invitation_error": _dump(invitation_error),
            "rabbitmq_request": _dump(request),
            "rabbitmq_request_with_token": _dump(request_with_token),
            "rabbitmq_response_ok": _dump(response_ok),
            "rabbitmq_response_error": _dump(response_error),
            "invalid": invalid,
            "mentions": mention_cases,
            "direct_identity": {
                "ok": assert_direct_group_request_identity(_dump(request), peer_aic=LEADER_AIC.lower()),
                "mismatch": _error_text(
                    lambda: assert_direct_group_request_identity(_dump(request), peer_aic=OTHER_AIC)
                ),
                "missing_leader": _error_text(
                    lambda: assert_direct_group_request_identity({"params": None}, peer_aic=OTHER_AIC)
                ),
            },
        },
    )


def gen_runtime() -> None:
    rt = runtime_mod
    normalize_inputs = [
        "group-sess_b50a1a6a8fb24024",
        "  group with spaces  ",
        "--leading--and--trailing--",
        "中文-group",
        "___",
        "a" * 130 + "-b",
        "x" * 127 + "_y",
    ]
    normalize = []
    for value in normalize_inputs:
        try:
            normalize.append({"input": value, "output": rt.normalize_group_id(value)})
        except ValueError as exc:
            normalize.append({"input": value, "error": str(exc)})

    plain_cases = [
        {"host": "localhost", "port": 5672, "vhost": "/", "username": "guest", "password": "guest", "connection_name": f"agent-{LEADER_AIC}"},
        {"host": "mq.example.com", "port": 5673, "vhost": "acps", "username": "u@x", "password": "p:w/d!*'()~ é", "connection_name": None},
        {"host": "mq", "port": 1, "vhost": " /team/a b ", "username": "a", "password": "b", "connection_name": "name with space&x=1"},
        {"host": "mq", "port": 1, "vhost": "", "username": "a", "password": "b", "connection_name": ""},
    ]
    plain = [{"input": c, "output": rt.build_plain_connection_url(**c)} for c in plain_cases]
    external_cases = [
        {"host": "mq.acps.example.com", "port": 5671, "vhost": "acps", "connection_name": f"agent-{PARTNER_AIC}"},
        {"host": "mq", "port": 5671, "vhost": "/", "connection_name": "x/y"},
        {"host": "mq", "port": 5671, "vhost": "//double", "connection_name": "名"},
    ]
    external = [{"input": c, "output": rt.build_external_connection_url(**c)} for c in external_cases]

    endpoint_cases = [
        ("amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}", PARTNER_AIC),
        ("amqp://MQ.Example.COM/?inbox=inbox_x", None),
        ("amqps://mq.example.com?inbox=a%20b&inbox=second", None),
        ("amqps://user:pw@[::1]:5999/v%2Fhost%20x?inbox=q", None),
        ("amqps://mq:0/acps?inbox=q", None),
        ("amqps://mq/acps;params?inbox=q#frag", None),
        ("amqps://mq/acps?inbox=", None),
        ("amqps://mq/acps", None),
        ("http://mq/acps?inbox=q", None),
        ("amqps:///acps?inbox=q", None),
        ("amqps://mq:99999/acps?inbox=q", None),
        ("amqps://mq:abc/acps?inbox=q", None),
        ("amqps://mq/acps?inbox=inbox_{AIC}", None),
        ("amqps://mq/acps?inbox=inbox_{AIC}", "bad-aic"),
        ("  AMQPS://mq/%E4%B8%AD?inbox=%E4%B8%AD+x  ", None),
    ]
    endpoints = []
    for url, aic in endpoint_cases:
        try:
            ep = rt.parse_amqp_endpoint_url(url, aic=aic)
            endpoints.append(
                {"url": url, "aic": aic, "output": {"url": ep.url, "host": ep.host, "port": ep.port, "vhost": ep.vhost, "inbox": ep.inbox}}
            )
        except ValueError as exc:
            endpoints.append({"url": url, "aic": aic, "error": str(exc)})

    acs_data = {
        "endPoints": [
            "not-a-dict",
            {"transport": None, "url": "amqps://none"},
            {"transport": "amqp", "url": "   "},
            {"transport": "amqp", "url": "  amqps://mq/acps?inbox=inbox_{AIC}  "},
            {"transport": "JSONRPC", "url": "https://partner.example.com/group/rpc"},
        ]
    }
    get_endpoint = {
        "acs_data": acs_data,
        "amqp": rt.get_endpoint_url(acs_data, "AMQP", aic=PARTNER_AIC),
        "jsonrpc": rt.get_endpoint_url(acs_data, "jsonrpc"),
        "missing": rt.get_endpoint_url(acs_data, "HTTP"),
        "not_dict": rt.get_endpoint_url(None, "AMQP"),
        "no_list": rt.get_endpoint_url({"endPoints": {}}, "AMQP"),
    }

    datetime_inputs = [
        "2026-06-24",
        "20260624",
        "2026-06-24T00:00:00",
        "2026-06-24T00:00:00Z",
        " 2026-06-24 08:30:00+08:00 ",
        "2026-06-24T00:00:00.1234567+00:00",
        "2026-06-24T00:00:00,5Z",
        "2026-06-24T07",
        "2026-06-24T0730",
        "2026-06-24T073015",
        "2026-06-24T00:00:00+0530",
        "2026-06-24T00:00:00+05",
        "2026-06-24T00:00:00-05:30:15",
        "2026-W26-3",
        "2026W263",
        "2026-W26",
        "2020-W53-1",
        "2026-W53",
        "2026-06-24X12:00",
        "2026-06-24T24:00:00",
        "2026-02-29",
        "2024-02-29T12:00:00Z",
        "garbage",
        "2026-6-24",
        "2026-06-24T1",
        "2026-06-24T00:00:00.",
        "2026-06-24T00:00:00Z+00:00",
        "2026-06-24T00:00:00z",
    ]
    datetimes = []
    for value in datetime_inputs:
        try:
            datetimes.append({"input": value, "output": rt.parse_utc_datetime(value).isoformat()})
        except ValueError as exc:
            datetimes.append({"input": value, "error": str(exc)})

    names = {
        "exchange": rt.build_group_exchange_name(LEADER_AIC, "group-session-1"),
        "queue": rt.build_group_queue_name(LEADER_AIC, "group-session-1", PARTNER_AIC),
        "inbox": rt.build_inbox_queue_name(PARTNER_AIC),
        "invalid_aic": _error_text(lambda: rt.build_inbox_queue_name("leader-aic")),
        "invalid_group": _error_text(lambda: rt.build_group_exchange_name(LEADER_AIC, "bad_group")),
        "placeholder_missing_aic": _error_text(lambda: rt.replace_aic_placeholder("x{AIC}y{AIC}", None)),
        "placeholder_replaced": rt.replace_aic_placeholder("x{AIC}y{AIC}", LEADER_AIC),
    }

    write_fixture(
        PKG,
        "runtime",
        {
            "constants": {
                "INBOX_EXCHANGE_NAME": rt.INBOX_EXCHANGE_NAME,
                "INBOX_QUEUE_EXPIRES_MS": rt.INBOX_QUEUE_EXPIRES_MS,
                "INBOX_MESSAGE_TTL_MS": rt.INBOX_MESSAGE_TTL_MS,
                "DEFAULT_INVITATION_TIMEOUT_SECONDS": rt.DEFAULT_INVITATION_TIMEOUT_SECONDS,
                "INVITATION_CLOCK_SKEW_SECONDS": rt.INVITATION_CLOCK_SKEW_SECONDS,
            },
            "names": names,
            "normalize_group_id": normalize,
            "plain_urls": plain,
            "external_urls": external,
            "endpoints": endpoints,
            "get_endpoint_url": get_endpoint,
            "parse_utc_datetime": datetimes,
            "invitation_expiry_300": rt.calculate_invitation_expiry(300),
            "now": FIXED_NOW.isoformat(),
            "expired": {
                "2026-10-19T08:00:42.160000+00:00": rt.invitation_is_expired("2026-10-19T08:00:42.160000+00:00"),
                "2026-10-19T08:00:42.161000+00:00": rt.invitation_is_expired("2026-10-19T08:00:42.161000+00:00"),
                "2026-10-19T08:01:12.160000+00:00": rt.invitation_is_expired("2026-10-19T08:01:12.160000+00:00"),
            },
        },
    )


async def gen_published() -> None:
    result: dict[str, Any] = {}

    # ---------------- Leader：建群拓扑 + 各类发布 ----------------
    recorder = Recorder()
    connection = FakeConnection(recorder)
    leader = leader_mod.GroupLeaderMqClient(
        leader_aic=LEADER_AIC,
        rabbitmq_host="mq.acps.example.com",
        rabbitmq_port=5671,
        rabbitmq_vhost="acps",
        rabbitmq_user=None,
        rabbitmq_password=None,
        connection=connection,
        connection_owner=False,
    )
    await leader.connect()
    group_id = await leader.create_group(group_id="group-session-1", partner_acs_list=[ACSObject(aic=OTHER_AIC)])
    result["leader_create_group"] = {"group_id": group_id, "ops": list(recorder.ops)}

    recorder.ops.clear()
    await leader.send_task_command(
        TaskCommandType.Start,
        "task-1",
        "session-1",
        text_content="请处理：Ünïcödé ✓",
        data_items=[StructuredDataItem(data={"k": [1, 2.5, None, True], "nested": {"x": None}})],
        mentions=[PARTNER_AIC],
    )
    await leader.start_task("session-1", "start text", mentions="all")
    await leader.continue_task("task-1", "session-1", "more")
    await leader.complete_task("task-1", "session-1")
    await leader.cancel_task("task-1", "session-1", reason="stop")
    await leader.send_mgmt_command(GroupMgmtCommandType.GET_STATUS, mentions="all", session_id="session-1")
    await leader.get_partner_status(PARTNER_AIC)
    await leader.mute_partner(PARTNER_AIC, session_id="session-1")
    await leader.unmute_partner(PARTNER_AIC)
    await leader.request_partner_leave(PARTNER_AIC, session_id="session-1")
    result["leader_publishes"] = [op for op in recorder.ops if op["op"] == "publish"]

    recorder.ops.clear()
    invitation = await leader.invite_via_inbox(
        ACSObject(aic=PARTNER_AIC),
        partner_inbox=f"inbox_{PARTNER_AIC}",
        invitation_token="token-abc",
        expires_at="2026-10-19T08:06:12.160000+00:00",
    )
    await leader.invite_via_inbox(ACSObject(aic=PARTNER_AIC), partner_inbox=f"inbox_{PARTNER_AIC}")
    result["leader_invite_via_inbox"] = {"invitation": _dump(invitation), "ops": list(recorder.ops)}

    recorder.ops.clear()
    await leader.delete_partner_queue(PARTNER_AIC)
    await leader.cleanup_group_resources()
    result["leader_cleanup"] = list(recorder.ops)

    # ---------------- Leader：connect URL ----------------
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect_robust(url: str, **kwargs: Any) -> FakeConnection:
        connect_calls.append({"url": url, "ssl_context": "ssl_context" in kwargs})
        return FakeConnection(Recorder())

    leader_mod.aio_pika.connect_robust = fake_connect_robust  # type: ignore[assignment]
    await leader_mod.GroupLeaderMqClient(
        leader_aic=LEADER_AIC, rabbitmq_host="mq.example.com", rabbitmq_port=5672, rabbitmq_vhost="/"
    ).connect()
    await leader_mod.GroupLeaderMqClient(
        leader_aic=LEADER_AIC,
        rabbitmq_host="mq.acps.example.com",
        rabbitmq_port=5671,
        rabbitmq_vhost="acps",
        rabbitmq_user=None,
        rabbitmq_password=None,
        ssl_context=object(),  # type: ignore[arg-type]
    ).connect()
    result["leader_connect_urls"] = connect_calls
    result["leader_external_without_ssl"] = await _async_error_text(
        leader_mod.GroupLeaderMqClient(leader_aic=LEADER_AIC, rabbitmq_user="", rabbitmq_password=None).connect()
    )

    # ---------------- GroupLeader：共享连接 + inbox 拓扑 ----------------
    gl_recorder = Recorder()
    gl_connect_calls: list[dict[str, Any]] = []

    async def fake_connect_robust_gl(url: str, **kwargs: Any) -> FakeConnection:
        gl_connect_calls.append({"url": url, "ssl_context": "ssl_context" in kwargs})
        return FakeConnection(gl_recorder)

    leader_mod.aio_pika.connect_robust = fake_connect_robust_gl  # type: ignore[assignment]
    group_leader = leader_mod.GroupLeader(
        LEADER_AIC, {"host": "mq.acps.example.com", "port": 5671, "vhost": "acps", "user": "u", "password": "p"}
    )
    session = await group_leader.create_group_session("sess_b50a1a6a", [ACSObject(aic=PARTNER_AIC)])
    result["group_leader_create_session"] = {
        "connect_calls": gl_connect_calls,
        "group_id": session.group_id,
        "ops": list(gl_recorder.ops),
        "runtime": group_leader.get_group_runtime("sess_b50a1a6a"),
    }
    if group_leader._cleanup_task:
        group_leader._cleanup_task.cancel()

    # ---------------- Partner：直连 RPC 加入 ----------------
    p_recorder = Recorder()
    p_connect_calls: list[dict[str, Any]] = []

    async def fake_partner_connect_robust(url: str, **kwargs: Any) -> FakeConnection:
        p_connect_calls.append({"url": url, "ssl_context": "ssl_context" in kwargs, "robust": True})
        return FakeConnection(p_recorder)

    async def fake_partner_connect(url: str, **kwargs: Any) -> FakeConnection:
        p_connect_calls.append({"url": url, "ssl_context": "ssl_context" in kwargs, "robust": False})
        return FakeConnection(p_recorder)

    partner_mod.aio_pika.connect_robust = fake_partner_connect_robust  # type: ignore[assignment]
    partner_mod.aio_pika.connect = fake_partner_connect  # type: ignore[assignment]

    partner = partner_mod.GroupPartnerMqClient(partner_aic=PARTNER_AIC)
    request = RabbitMQRequest(
        id="req-1",
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=_group_info(),
            server=RabbitMQServerConfig(host="mq.acps.example.com", port=5672, vhost="acps", accessToken="user:pa:ss"),
            amqp=_amqp_config(),
        ),
    )
    response = await partner.join_group(request)
    result["partner_join_group"] = {
        "response": _dump(response),
        "connect_calls": list(p_connect_calls),
        "ops": list(p_recorder.ops),
        "user": partner.rabbitmq_user,
        "password": partner.rabbitmq_password,
    }

    p_recorder.ops.clear()
    await partner.send_status_result(connected=True, muted=True, session_id="session-1")
    await partner.accept_task("task-1", "session-1")
    await partner.request_input("task-1", "session-1", "need more ✓")
    await partner.submit_for_completion(
        "task-1",
        "session-1",
        [Product(id="p-1", name="report", dataItems=[TextDataItem(text="done"), StructuredDataItem(data={"score": 0.5})])],
    )
    await partner.fail_task("task-1", "session-1", "boom")
    result["partner_publishes"] = [op for op in p_recorder.ops if op["op"] == "publish"]

    p_recorder.ops.clear()
    await partner.leave_group(session_id="session-1")
    result["partner_leave_group"] = list(p_recorder.ops)

    # 加入失败（server 无 accessToken 且无 ssl_context）
    failing = partner_mod.GroupPartnerMqClient(partner_aic=PARTNER_AIC)
    fail_request = RabbitMQRequest(
        id=99,
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=_group_info(),
            server=RabbitMQServerConfig(host="mq", port=5671, vhost="acps"),
            amqp=_amqp_config(),
        ),
    )
    result["partner_join_failed"] = _dump(await failing.join_group(fail_request))

    rejecting = partner_mod.GroupPartnerMqClient(partner_aic=PARTNER_AIC)

    async def reject(_inv: Any) -> bool:
        return False

    rejecting.set_invitation_handler(reject)
    result["partner_join_rejected"] = _dump(await rejecting.join_group(fail_request))
    # 请求没有 id：Python 的 exclude_none 省略响应 id；Node 按 JSON-RPC 输出 "id": null（比对时去掉）
    no_id_request = RabbitMQRequest(params=fail_request.params)
    result["partner_join_rejected_no_id"] = _dump(await rejecting.join_group(no_id_request))

    # ---------------- Partner：inbox 拓扑 + 邀请错误 ----------------
    p_recorder.ops.clear()
    p_connect_calls.clear()
    inbox_partner = partner_mod.GroupPartnerMqClient(
        partner_aic=PARTNER_AIC,
        rabbitmq_host="mq.acps.example.com",
        rabbitmq_port=5671,
        rabbitmq_vhost="acps",
        rabbitmq_user=None,
        rabbitmq_password=None,
        ssl_context=object(),  # type: ignore[arg-type]
        robust_connection=False,
    )
    await inbox_partner.ensure_inbox_queue()
    inbox_invitation = InboxGroupInvitation(
        id="invite-1",
        sentAt="2026-10-19T08:01:12.160000+00:00",
        senderRole="leader",
        senderId=LEADER_AIC,
        groupId="group-session-1",
        protocol="rabbitmq:4.2",
        expiresAt="2026-10-19T08:06:12.160000+00:00",
        invitationToken="token-abc",
        group=_group_info(),
        amqp=_amqp_config(),
    )
    await inbox_partner.send_invitation_error(
        inbox_invitation,
        GroupInvitationError(
            code=-32021,
            message="Connection failed",
            data=GroupInvitationErrorData(errorType="CONNECTION_FAILED", details="reason ✓"),
        ),
    )
    result["partner_inbox"] = {"connect_calls": list(p_connect_calls), "ops": list(p_recorder.ops)}

    # ---------------- Emitter：send / receive body ----------------
    emitter = FakeEmitter()
    e_recorder = Recorder()
    e_leader = leader_mod.GroupLeaderMqClient(
        leader_aic=LEADER_AIC,
        rabbitmq_vhost="acps",
        connection=FakeConnection(e_recorder),
        connection_owner=False,
        message_emitter=emitter,  # type: ignore[arg-type]
        trace_context_provider=lambda: TraceContext(trace_id="a" * 32, span_id="c" * 16),
    )
    await e_leader.connect()
    await e_leader.create_group(group_id="group-session-1")
    await e_leader.send_task_command(TaskCommandType.Get, "task-1", "session-1", text_content="状态？")
    leader_send = {
        "publish": [op for op in e_recorder.ops if op["op"] == "publish"][-1],
        "emits": list(emitter.calls),
    }

    emitter.calls.clear()
    e_leader.set_message_handler(lambda _m: _noop())
    e_leader._leader_queue.consumers.clear()  # type: ignore[union-attr]
    await e_leader.start_consuming()
    await asyncio.sleep(0)
    on_message = e_leader._leader_queue.consumers[0]  # type: ignore[union-attr]
    incoming_body = json.dumps(
        {
            "type": "task-result",
            "id": "res-1",
            "sentAt": "2026-10-19T08:01:12.160000+00:00",
            "senderRole": "partner",
            "senderId": PARTNER_AIC,
            "groupId": "group-session-1",
            "sessionId": "session-1",
            "taskId": "task-1",
            "status": {"state": "working", "stateChangedAt": "2026-10-19T08:01:12.160000+00:00"},
        },
        ensure_ascii=False,
    ).encode()
    await on_message(
        FakeIncoming(
            incoming_body,
            user_id=PARTNER_AIC,
            headers={"traceparent": f"00-{'d' * 32}-{'e' * 16}-01"},
        )
    )
    leader_receive = {
        "incoming_body": incoming_body.decode(),
        "emits": [
            {**call, "body": {k: v for k, v in call["body"].items() if k != "settlement"}} for call in emitter.calls
        ],
    }

    p_emitter = FakeEmitter()
    pe_recorder = Recorder()
    e_partner = partner_mod.GroupPartnerMqClient(
        partner_aic=PARTNER_AIC, message_emitter=p_emitter, identity_binding_enabled=True  # type: ignore[arg-type]
    )
    e_partner._exchange = FakeExchange(pe_recorder, "group.test")  # type: ignore[assignment]
    e_partner._exchange_name = "group.test"
    e_partner._group_id = "group-session-1"
    e_partner.rabbitmq_vhost = "acps"
    await e_partner.complete_task("task-1", "session-1")
    partner_send = {"publish": pe_recorder.ops[-1], "emits": list(p_emitter.calls)}

    result["emitter"] = {"leader_send": leader_send, "leader_receive": leader_receive, "partner_send": partner_send}

    write_fixture(PKG, "published", result)


async def _noop() -> None:
    return None


async def _async_error_text(coro) -> str | None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


async def gen_rpc_and_acl() -> None:
    captured: list[dict[str, Any]] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "content_type": request.headers.get("content-type"),
                "body": request.content.decode("utf-8"),
            }
        )
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        kwargs.pop("cert", None)
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    leader_mod.httpx.AsyncClient = patched_client  # type: ignore[assignment]

    rpc: dict[str, Any] = {}
    recorder = Recorder()
    client = leader_mod.GroupLeaderMqClient(
        leader_aic=LEADER_AIC,
        rabbitmq_host="mq.acps.example.com",
        rabbitmq_port=5672,
        rabbitmq_vhost="acps",
        rabbitmq_user="guest",
        rabbitmq_password="guest",
        connection=FakeConnection(recorder),
        connection_owner=False,
    )
    await client.connect()
    await client.create_group(group_id="group-session-1", partner_acs_list=[ACSObject(aic=OTHER_AIC)])

    ok_response = {
        "jsonrpc": "2.0",
        "id": FIXED_UUID,
        "result": {
            "connectionName": f"agent-{PARTNER_AIC}",
            "vhost": "acps",
            "nodeName": "unknown",
            "queueName": f"group_{LEADER_AIC}_group-session-1_{PARTNER_AIC}",
            "processId": "pid-1",
        },
    }
    responses.append(httpx.Response(200, json=ok_response))
    info = await client.invite_partner(ACSObject(aic=PARTNER_AIC), "https://partner.example.com/group/rpc")
    rpc["ok"] = {"request": captured[-1], "response": ok_response, "partner": {k: v for k, v in info.to_dict().items() if k != "joined_at"}}

    responses.append(
        httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "error": {"code": -32020, "message": "nope"}})
    )
    rpc["refused"] = await _async_error_text(
        client.invite_partner(ACSObject(aic=OTHER_AIC), "https://partner.example.com/group/rpc")
    )
    responses.append(httpx.Response(200, json={"jsonrpc": "2.0", "id": "x"}))
    rpc["empty"] = await _async_error_text(
        client.invite_partner(ACSObject(aic=OTHER_AIC), "https://partner.example.com/group/rpc")
    )
    responses.append(httpx.Response(404, text="missing"))
    rpc["not_found"] = await _async_error_text(
        client.invite_partner(ACSObject(aic=OTHER_AIC), "https://partner.example.com/group/rpc")
    )
    responses.append(httpx.Response(200, text="not json"))
    not_json = await _async_error_text(
        client.invite_partner(ACSObject(aic=OTHER_AIC), "https://partner.example.com/group/rpc")
    )
    rpc["not_json_prefix"] = (not_json or "").split(":")[0]
    rpc["captured_count"] = len(captured)
    leader_mod.httpx.AsyncClient = original_client  # type: ignore[assignment]

    acl_captured: list[dict[str, Any]] = []
    acl_responses: list[httpx.Response] = []

    def acl_handler(request: httpx.Request) -> httpx.Response:
        acl_captured.append({"method": request.method, "url": str(request.url)})
        return acl_responses.pop(0) if acl_responses else httpx.Response(204)

    acl = GroupAclClient(base_url="https://auth.example.com/api/")
    acl._client = httpx.AsyncClient(base_url=acl.base_url, transport=httpx.MockTransport(acl_handler))
    await acl.add_member(leader_aic=LEADER_AIC, group_id="group-1", member_aic=PARTNER_AIC)
    await acl.remove_member(leader_aic=LEADER_AIC, group_id="group-1", member_aic=PARTNER_AIC)
    await acl.delete_group(leader_aic=LEADER_AIC, group_id="group-1")
    await acl.close_member_connection(leader_aic=LEADER_AIC, group_id="group-1", member_aic=PARTNER_AIC)
    await acl.add_member(leader_aic="a b/c!*'()~", group_id="中", member_aic="x")
    acl_responses.append(httpx.Response(404, text="  group not found \n"))
    errors: dict[str, str | None] = {}
    try:
        await acl.delete_group(leader_aic=LEADER_AIC, group_id="group-1")
    except GroupAclError as exc:
        errors["with_body"] = str(exc)
    acl_responses.append(httpx.Response(503, text=""))
    try:
        await acl.remove_member(leader_aic=LEADER_AIC, group_id="group-1", member_aic=PARTNER_AIC)
    except GroupAclError as exc:
        errors["without_body"] = str(exc)
    await acl.aclose()

    write_fixture(PKG, "http", {"rpc": rpc, "acl": {"base_url": acl.base_url, "requests": acl_captured, "errors": errors}})


def main() -> None:
    _install_determinism()
    try:
        gen_models()
        gen_runtime()
        asyncio.run(gen_published())
        asyncio.run(gen_rpc_and_acl())
    finally:
        _restore_patches()


if __name__ == "__main__":
    main()
