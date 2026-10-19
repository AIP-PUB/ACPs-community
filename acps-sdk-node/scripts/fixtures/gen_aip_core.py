"""AIP 核心（非群组）跨语言对照样例。

覆盖：
- models.json            —— 各 pydantic 模型 `model_dump(exclude_none=True, mode="json")` 的紧凑 JSON（含键顺序）
- data_item_union.json   —— 无 `type` 的 DataItem 在 pydantic smart-mode union 下解析成哪一类
- sse.json               —— format_sse(build_stream_response / build_stream_error_response) 的输出
- identity.json          —— AIC 规范化、身份断言、证书身份提取、身份错误 → JSON-RPC 错误映射
- access_status.json     —— RpcResponse → Access statusCode / ErrorInfo
- rpc_scenarios.json     —— handle_rpc_request + DefaultHandlers + TaskManager 的逐步行为
- stream_scenarios.json  —— handle_stream_request 的 HTTP 错误 / SSE 帧
- notification_scenarios.json —— 通知路由（set/get/delete/start）、回调分发、回调接收
- client_requests.json   —— 各客户端发出的请求体（固定 UUID / 时间）

UUID 与时间戳通过 patch 固定为递增序列（Node 测试用同样的序列 mock `randomUUID` / `utcNowIso`）。

运行：`uv run --project ../acps-sdk --python 3.12 python -I scripts/fixtures/gen_aip_core.py`
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from _common import write_fixture  # noqa: E402

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

from acps_sdk.aip import aip_notification_client, aip_rpc_client, aip_rpc_server, aip_stream_client  # noqa: E402
from acps_sdk.aip.access_status import (  # noqa: E402
    error_info_from_rpc_response,
    status_from_rpc_error,
    status_from_rpc_response,
)
from acps_sdk.aip.aip_base_model import (  # noqa: E402
    DataItem,
    FileDataItem,
    GetCommandParams,
    Message,
    Product,
    StartCommandParams,
    StructuredDataItem,
    TaskCommand,
    TaskResult,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_identity import (  # noqa: E402
    AipIdentityError,
    InvalidPeerCertificateError,
    PeerAicMissingError,
    SenderIdentityMismatchError,
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_acps_uri_san,
    extract_common_name,
    extract_peer_identity,
    identity_error_to_jsonrpc,
    normalize_aic,
    normalize_and_validate_aic,
    normalize_and_validate_expected_aic,
)
from acps_sdk.aip.aip_notification_client import AipNotificationClient, NotificationReceiver  # noqa: E402
from acps_sdk.aip.aip_notification_model import (  # noqa: E402
    NotificationConfig,
    NotificationDeleteRequest,
    NotificationDeleteResponse,
    NotificationDeleteResult,
    NotificationGetRequest,
    NotificationGetResponse,
    NotificationIdParams,
    NotificationRequest,
    NotificationResponse,
    NotificationStartParams,
    NotificationStartRequest,
    NotificationStartRequestParams,
)
from acps_sdk.aip.aip_notification_server import NotificationService, add_aip_notification_router  # noqa: E402
from acps_sdk.aip.aip_rpc_client import AipRpcClient  # noqa: E402
from acps_sdk.aip.aip_rpc_model import (  # noqa: E402
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    RpcRequest,
    RpcRequestParams,
    RpcResponse,
)
from acps_sdk.aip.aip_rpc_server import CommandHandlers, TaskManager, handle_rpc_request  # noqa: E402
from acps_sdk.aip.aip_stream_client import AipStreamClient  # noqa: E402
from acps_sdk.aip.aip_stream_model import (  # noqa: E402
    ProductChunkEvent,
    ReStreamCommandParams,
    StreamEventData,
    StreamRequest,
    StreamRequestParams,
    StreamResponse,
    TaskStatusUpdateEvent,
)
from acps_sdk.aip.aip_stream_server import (  # noqa: E402
    BufferedStreamEvent,
    StreamHandlers,
    StreamHub,
    TaskStreamChannel,
    build_stream_error_response,
    build_stream_response,
    format_sse,
    handle_stream_request,
)

LEADER = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"
NOW = "2026-06-24T00:00:00Z"
LATER = "2026-06-24T00:00:05Z"
UNICODE_TEXT = '中文 😀 "quote" \\ /   \x00 \x1f tab\tnl\n'


def compact(value: Any) -> str:
    """与 JS `JSON.stringify` 等价的紧凑 JSON（不转义非 ASCII）。"""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# ===========================================================================
# 固定 UUID / 时间
# ===========================================================================


class _Sequence:
    def __init__(self) -> None:
        self.uuid_counter = 0
        self.time_counter = 0

    def next_uuid(self) -> uuid.UUID:
        self.uuid_counter += 1
        return uuid.UUID(int=self.uuid_counter)

    def next_time(self) -> str:
        self.time_counter += 1
        n = self.time_counter
        return f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}.{n:06d}+00:00"


@contextmanager
def deterministic() -> Iterator[_Sequence]:
    seq = _Sequence()

    class _FakeNow:
        def __init__(self, text: str) -> None:
            self._text = text

        def isoformat(self) -> str:
            return self._text

    class _FakeDatetime:
        @staticmethod
        def now(tz: Any = None) -> _FakeNow:
            return _FakeNow(seq.next_time())

    with ExitStack() as stack:
        stack.enter_context(patch("uuid.uuid4", side_effect=seq.next_uuid))
        for module in (aip_rpc_server, aip_rpc_client, aip_stream_client, aip_notification_client):
            stack.enter_context(patch.object(module, "datetime", _FakeDatetime))
        yield seq


# ===========================================================================
# 公共输入
# ===========================================================================

STATUS_MIN = {"state": "working", "stateChangedAt": NOW}
STATUS_FULL = {
    "dataItems": [{"type": "text", "text": "progress", "metadata": {"k": None}}],
    "stateChangedAt": NOW,
    "state": "awaiting-input",
}
CMD_MIN = {
    "type": "task-command",
    "id": "cmd-1",
    "sentAt": NOW,
    "senderRole": "leader",
    "senderId": LEADER,
    "command": "start",
}
CMD_FULL = {
    "taskId": "task-1",
    "commandParams": {"a": None, "nested": {"b": [1, None, "x"]}, "lastCommandSentAt": NOW},
    "command": "continue",
    "sessionId": "sess-1",
    "groupId": "group-1",
    "dataItems": [
        {"text": UNICODE_TEXT},
        {"type": "file", "name": "a.txt", "mimeType": "text/plain", "uri": "https://x/a.txt", "bytes": "YWJj"},
        {"type": "data", "data": {"k": None, "list": [True, False, None]}, "metadata": {}},
    ],
    "mentions": ["p1", "p2"],
    "senderId": LEADER,
    "senderRole": "leader",
    "sentAt": NOW,
    "id": "cmd-2",
    "type": "task-command",
}
RESULT_MIN = {
    "type": "task-result",
    "id": "res-1",
    "sentAt": NOW,
    "senderRole": "partner",
    "senderId": PARTNER,
    "taskId": "task-1",
    "status": STATUS_MIN,
}
PRODUCT_FULL = {
    "dataItems": [{"type": "text", "text": "answer"}, {"data": {"score": 3}}],
    "description": "desc",
    "name": "Result",
    "id": "prod-1",
}
RESULT_FULL = {
    "statusHistory": [STATUS_MIN, STATUS_FULL],
    "commandHistory": [CMD_MIN, CMD_FULL],
    "products": [PRODUCT_FULL, {"id": "prod-2", "dataItems": []}],
    "status": STATUS_FULL,
    "taskId": "task-1",
    "sessionId": "sess-1",
    "groupId": None,
    "dataItems": None,
    "mentions": "all",
    "senderId": PARTNER,
    "senderRole": "partner",
    "sentAt": NOW,
    "id": "res-2",
}
STATUS_EVENT = {
    "type": "task-status-update",
    "id": "ev-1",
    "sentAt": NOW,
    "senderRole": "partner",
    "senderId": PARTNER,
    "taskId": "task-1",
    "status": STATUS_MIN,
}
CHUNK_EVENT = {
    "lastChunk": False,
    "append": True,
    "product": {"id": "prod-1", "dataItems": [{"text": "chunk"}]},
    "taskId": "task-1",
    "senderId": PARTNER,
    "senderRole": "partner",
    "sentAt": NOW,
    "id": "ev-2",
    "type": "product-chunk",
}
CONFIG_FULL = {"taskId": "task-1", "token": "tok", "url": "https://leader/cb", "id": "cfg-1"}


def without(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in keys}


def merged(data: dict[str, Any], **updates: Any) -> dict[str, Any]:
    return {**data, **updates}


# ===========================================================================
# models.json
# ===========================================================================

MODELS: dict[str, Any] = {
    "TextDataItem": TextDataItem,
    "FileDataItem": FileDataItem,
    "StructuredDataItem": StructuredDataItem,
    "DataItem": TypeAdapter(DataItem),
    "TaskStatus": TaskStatus,
    "Product": Product,
    "GetCommandParams": GetCommandParams,
    "StartCommandParams": StartCommandParams,
    "Message": Message,
    "TaskCommand": TaskCommand,
    "TaskResult": TaskResult,
    "JSONRPCRequest": JSONRPCRequest,
    "JSONRPCError": JSONRPCError,
    "JSONRPCResponse": JSONRPCResponse,
    "RpcRequestParams": RpcRequestParams,
    "RpcRequest": RpcRequest,
    "RpcResponse": RpcResponse,
    "TaskStatusUpdateEvent": TaskStatusUpdateEvent,
    "ProductChunkEvent": ProductChunkEvent,
    "StreamRequestParams": StreamRequestParams,
    "StreamRequest": StreamRequest,
    "StreamEventData": StreamEventData,
    "StreamResponse": StreamResponse,
    "ReStreamCommandParams": ReStreamCommandParams,
    "NotificationConfig": NotificationConfig,
    "NotificationRequest": NotificationRequest,
    "NotificationResponse": NotificationResponse,
    "NotificationIdParams": NotificationIdParams,
    "NotificationDeleteRequest": NotificationDeleteRequest,
    "NotificationDeleteResult": NotificationDeleteResult,
    "NotificationDeleteResponse": NotificationDeleteResponse,
    "NotificationGetRequest": NotificationGetRequest,
    "NotificationGetResponse": NotificationGetResponse,
    "NotificationStartParams": NotificationStartParams,
    "NotificationStartRequestParams": NotificationStartRequestParams,
    "NotificationStartRequest": NotificationStartRequest,
}


def model_inputs() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = []
    add = cases.append

    # --- 数据项 ---
    add(("TextDataItem", {"text": "hi"}))
    add(("TextDataItem", {"text": UNICODE_TEXT, "metadata": {"a": None, "b": [1, {"c": None}]}, "type": "text"}))
    add(("TextDataItem", {"text": "hi", "metadata": None, "extra": 1}))
    add(("TextDataItem", {"type": "file", "text": "hi"}))
    add(("TextDataItem", {}))
    add(("TextDataItem", {"text": None}))
    add(("FileDataItem", {}))
    add(("FileDataItem", {"bytes": "YQ==", "uri": "u", "mimeType": "m", "name": "n", "metadata": {}}))
    add(("FileDataItem", {"name": None, "uri": None}))
    add(("FileDataItem", {"type": "text"}))
    add(("StructuredDataItem", {"data": {}}))
    add(("StructuredDataItem", {"data": {"x": None, "y": {"z": None}}, "metadata": {"m": 1}}))
    add(("StructuredDataItem", {"data": None}))
    add(("StructuredDataItem", {"data": [1, 2]}))
    add(("StructuredDataItem", {}))
    add(("DataItem", {"type": "text", "text": "t"}))
    add(("DataItem", {"type": "file"}))
    add(("DataItem", {"type": "data", "data": {}}))
    add(("DataItem", {"type": "unknown", "text": "t"}))
    add(("DataItem", {"type": None, "text": "t"}))
    add(("DataItem", {"type": "text"}))
    add(("DataItem", "not-an-object"))

    # --- 状态 / 产出物 / 命令参数 ---
    add(("TaskStatus", STATUS_MIN))
    add(("TaskStatus", STATUS_FULL))
    add(("TaskStatus", {"state": "bogus", "stateChangedAt": NOW}))
    add(("TaskStatus", {"state": "working"}))
    add(("TaskStatus", merged(STATUS_MIN, dataItems=None)))
    for state in ["accepted", "working", "awaiting-input", "awaiting-completion", "completed", "canceled", "failed", "rejected"]:
        add(("TaskStatus", {"stateChangedAt": NOW, "state": state}))
    add(("Product", PRODUCT_FULL))
    add(("Product", {"id": "p"}))
    add(("Product", {"id": "p", "dataItems": [{}], "name": None}))
    add(("GetCommandParams", {}))
    add(("GetCommandParams", {"lastStateChangedAt": NOW, "lastCommandSentAt": LATER}))
    add(("GetCommandParams", {"lastMessageSentAt": NOW}))
    add(("StartCommandParams", {}))
    add(("StartCommandParams", {"maxProductsBytes": 1024, "timeout": 30}))
    add(("StartCommandParams", {"timeout": 1.5}))
    add(("StartCommandParams", {"timeout": None}))

    # --- Message / TaskCommand / TaskResult ---
    add(("Message", without(CMD_MIN, "command")))
    add(("Message", merged(without(CMD_MIN, "command", "type"), mentions="all")))
    add(("Message", merged(without(CMD_MIN, "command"), type="custom-type", extra="ignored")))
    add(("Message", merged(without(CMD_MIN, "command"), senderRole="observer")))
    add(("Message", merged(without(CMD_MIN, "command"), mentions="some")))
    add(("Message", merged(without(CMD_MIN, "command"), mentions=[])))
    add(("Message", merged(without(CMD_MIN, "command"), mentions=[1])))
    add(("TaskCommand", CMD_MIN))
    add(("TaskCommand", CMD_FULL))
    add(("TaskCommand", without(CMD_MIN, "type")))
    add(("TaskCommand", merged(CMD_MIN, type="task-result")))
    add(("TaskCommand", merged(CMD_MIN, command="bogus")))
    add(("TaskCommand", without(CMD_MIN, "command")))
    add(("TaskCommand", without(CMD_MIN, "senderId")))
    add(("TaskCommand", merged(CMD_MIN, command="re-stream", commandParams={"lastEventSeq": 3})))
    add(("TaskCommand", merged(CMD_MIN, commandParams=None, taskId=None, sessionId=None, mentions=None)))
    add(("TaskCommand", merged(CMD_MIN, commandParams=[1])))
    for command in ["get", "start", "continue", "cancel", "complete", "re-stream"]:
        add(("TaskCommand", merged(CMD_MIN, command=command)))
    add(("TaskResult", RESULT_MIN))
    add(("TaskResult", RESULT_FULL))
    add(("TaskResult", without(RESULT_MIN, "type")))
    add(("TaskResult", without(RESULT_MIN, "taskId")))
    add(("TaskResult", without(RESULT_MIN, "status")))
    add(("TaskResult", merged(RESULT_MIN, type="task-command")))
    add(("TaskResult", merged(RESULT_MIN, commandHistory=[without(CMD_MIN, "type")])))

    # --- JSON-RPC ---
    add(("JSONRPCRequest", {"method": "x"}))
    add(("JSONRPCRequest", {"params": [1, None], "id": 7, "method": "x", "jsonrpc": "2.0"}))
    add(("JSONRPCRequest", {"params": {"a": None}, "id": "abc", "method": "x"}))
    add(("JSONRPCRequest", {"method": "x", "id": None, "params": None}))
    add(("JSONRPCRequest", {"method": "x", "jsonrpc": "1.0"}))
    add(("JSONRPCRequest", {"id": 1}))
    add(("JSONRPCRequest", {"method": "x", "id": 1.5}))
    add(("JSONRPCRequest", {"method": "x", "params": "str"}))
    add(("JSONRPCError", {"code": -32000, "message": "boom"}))
    for data in [None, 0, False, "", [], {}, {"a": None}, [None], "detail"]:
        add(("JSONRPCError", {"data": data, "message": "m", "code": 1}))
    add(("JSONRPCError", {"code": "x", "message": "m"}))
    add(("JSONRPCError", {"code": 1}))
    add(("JSONRPCResponse", {}))
    add(("JSONRPCResponse", {"error": {"code": 1, "message": "m"}, "result": None, "id": "r"}))
    add(("JSONRPCResponse", {"result": {"a": None}, "id": 3}))
    add(("JSONRPCResponse", {"result": 0}))
    add(("JSONRPCResponse", {"result": False}))
    add(("RpcRequestParams", {"command": CMD_MIN}))
    add(("RpcRequestParams", {}))
    add(("RpcRequest", {"params": {"command": CMD_FULL}, "id": "rpc-1"}))
    add(("RpcRequest", {"params": {"command": CMD_MIN}, "method": "rpc", "jsonrpc": "2.0", "id": 9}))
    add(("RpcRequest", {"params": {"command": CMD_MIN}, "method": "stream"}))
    add(("RpcRequest", {"method": "rpc"}))
    add(("RpcRequest", {"params": {"command": merged(CMD_MIN, command="nope")}}))
    add(("RpcResponse", {"id": "r", "result": RESULT_FULL}))
    add(("RpcResponse", {"id": "r", "error": {"code": -32001, "message": "Task not found", "data": {"taskId": "t"}}}))
    add(("RpcResponse", {"result": None, "error": None, "id": None}))
    add(("RpcResponse", {"result": {"not": "a task result"}}))

    # --- Stream ---
    add(("TaskStatusUpdateEvent", STATUS_EVENT))
    add(("TaskStatusUpdateEvent", without(STATUS_EVENT, "type")))
    add(("TaskStatusUpdateEvent", without(STATUS_EVENT, "status")))
    add(("ProductChunkEvent", CHUNK_EVENT))
    add(("ProductChunkEvent", without(CHUNK_EVENT, "type", "lastChunk")))
    add(("ProductChunkEvent", merged(CHUNK_EVENT, append=None)))
    add(("StreamRequestParams", {"message": CMD_MIN}))
    add(("StreamRequest", {"params": {"message": CMD_FULL}, "id": "s-1"}))
    add(("StreamRequest", {"params": {"message": CMD_MIN}, "method": "rpc"}))
    add(("StreamRequest", {"params": {"command": CMD_MIN}}))
    for payload in [RESULT_MIN, RESULT_FULL, CMD_MIN, CMD_FULL, STATUS_EVENT, CHUNK_EVENT]:
        add(("StreamEventData", {"eventData": payload, "eventSeq": 4}))
    add(("StreamEventData", {"eventSeq": 1, "eventData": without(RESULT_MIN, "type")}))
    add(("StreamEventData", {"eventSeq": 1, "eventData": merged(RESULT_MIN, type="message")}))
    add(("StreamEventData", {"eventSeq": 1, "eventData": merged(CMD_MIN, type="task-result")}))
    add(("StreamEventData", {"eventData": RESULT_MIN}))
    add(("StreamResponse", {"id": "s", "result": {"eventSeq": 2, "eventData": CHUNK_EVENT}}))
    add(("StreamResponse", {"id": 5, "error": {"code": -32002, "message": "Cannot re-stream"}}))
    add(("StreamResponse", {"error": None, "result": None}))
    add(("ReStreamCommandParams", {}))
    add(("ReStreamCommandParams", {"lastEventSeq": 7}))
    add(("ReStreamCommandParams", {"lastEventSeq": None}))

    # --- Notification ---
    add(("NotificationConfig", CONFIG_FULL))
    add(("NotificationConfig", without(CONFIG_FULL, "id")))
    add(("NotificationConfig", merged(CONFIG_FULL, id=None)))
    add(("NotificationConfig", without(CONFIG_FULL, "token")))
    add(("NotificationRequest", {"params": without(CONFIG_FULL, "id"), "id": "1"}))
    add(("NotificationRequest", {"params": CONFIG_FULL, "method": "notification/get"}))
    add(("NotificationResponse", {"id": "1", "result": CONFIG_FULL}))
    add(("NotificationResponse", {"id": "1", "result": None}))
    add(("NotificationIdParams", {"taskId": "t"}))
    add(("NotificationIdParams", {"notificationConfigId": "c", "taskId": "t"}))
    add(("NotificationIdParams", {"taskId": "t", "configId": "ignored"}))
    add(("NotificationIdParams", {}))
    add(("NotificationDeleteRequest", {"params": {"taskId": "t"}, "id": 2}))
    add(("NotificationDeleteRequest", {"params": {"taskId": "t"}, "method": "notification/set"}))
    add(("NotificationDeleteResult", {}))
    add(("NotificationDeleteResult", {"success": True}))
    add(("NotificationDeleteResult", {"success": False}))
    add(("NotificationDeleteResponse", {"id": "x", "result": {}}))
    add(("NotificationDeleteResponse", {"id": "x"}))
    add(("NotificationGetRequest", {"params": {"notificationConfigId": "c", "taskId": "t"}}))
    add(("NotificationGetResponse", {"id": "x", "result": [CONFIG_FULL, without(CONFIG_FULL, "id")]}))
    add(("NotificationGetResponse", {"id": "x", "result": []}))
    add(("NotificationStartParams", {"notificationConfigId": "c"}))
    add(("NotificationStartParams", {"notifyOnStates": ["completed", "failed"], "notificationConfigId": "c"}))
    add(("NotificationStartParams", {"notifyOnStates": ["bogus"], "notificationConfigId": "c"}))
    add(("NotificationStartParams", {"notifyOnStates": []}))
    add(("NotificationStartRequestParams", {"message": CMD_FULL}))
    add(("NotificationStartRequest", {"params": {"message": merged(CMD_MIN, commandParams={"notificationConfigId": "c"})}, "id": "4"}))
    add(("NotificationStartRequest", {"params": {"message": CMD_MIN}, "method": "notification/set"}))
    return cases


def _dump(model: Any, value: Any) -> Any:
    if isinstance(model, TypeAdapter):
        return model.dump_python(model.validate_python(value), exclude_none=True, mode="json")
    return model.model_validate(value).model_dump(exclude_none=True, mode="json")


def models_fixture() -> list[dict[str, Any]]:
    out = []
    for name, value in model_inputs():
        try:
            out.append({"model": name, "input": value, "dump": compact(_dump(MODELS[name], value))})
        except ValidationError:
            out.append({"model": name, "input": value, "error": True})
    return out


# ===========================================================================
# data_item_union.json
# ===========================================================================

UNION_FIELDS: dict[str, Any] = {
    "text": "t",
    "data": {"k": 1},
    "name": "n",
    "mimeType": "m",
    "uri": "u",
    "bytes": "b",
    "metadata": {"m": 1},
}


def data_item_union_fixture() -> list[dict[str, Any]]:
    adapter = TypeAdapter(DataItem)
    inputs: list[dict[str, Any]] = []
    keys = list(UNION_FIELDS)
    for size in range(len(keys) + 1):
        for combo in itertools.combinations(keys, size):
            inputs.append({key: UNION_FIELDS[key] for key in combo})
    # null 值的键是否计入 "fields set"
    for key in keys:
        for base in ({"text": "t"}, {"data": {"k": 1}}, {"name": "n"}, {}):
            if key in base:
                continue
            inputs.append({**base, key: None})
    # 类型不匹配时退回其它成员
    inputs += [
        {"text": 1},
        {"text": 1, "name": "n"},
        {"data": "x", "name": "n"},
        {"data": "x", "text": "t"},
        {"text": "t", "data": "x", "uri": "u"},
        {"text": "t", "extra": 1, "another": 2},
        {"text": "t", "name": "n", "uri": None},
        {"name": 1},
        {"metadata": "x"},
        {"metadata": None},
    ]
    out = []
    for value in inputs:
        try:
            dumped = adapter.dump_python(adapter.validate_python(value), exclude_none=True, mode="json")
            out.append({"input": value, "type": dumped["type"], "dump": compact(dumped)})
        except ValidationError:
            out.append({"input": value, "error": True})
    return out


# ===========================================================================
# sse.json
# ===========================================================================


def sse_fixture() -> dict[str, Any]:
    frames = []
    payloads = [("TaskResult", RESULT_FULL), ("TaskResult", RESULT_MIN), ("TaskCommand", CMD_FULL), ("TaskStatusUpdateEvent", STATUS_EVENT), ("ProductChunkEvent", CHUNK_EVENT)]
    models = {"TaskResult": TaskResult, "TaskCommand": TaskCommand, "TaskStatusUpdateEvent": TaskStatusUpdateEvent, "ProductChunkEvent": ProductChunkEvent}
    for rpc_id in ["stream-1", 7, None]:
        for seq, (name, payload) in enumerate(payloads, start=1):
            event = BufferedStreamEvent(event_seq=seq, payload=models[name].model_validate(payload))
            frames.append({"rpcId": rpc_id, "eventSeq": seq, "model": name, "payload": payload, "sse": format_sse(build_stream_response(rpc_id, event)).decode()})
    errors = []
    for rpc_id in ["stream-1", 0, None]:
        for data in [None, 0, False, "", {}, [], {"a": None}, "detail"]:
            response = build_stream_error_response(rpc_id, -32002, "Cannot re-stream: buffer overflow, please restart", data)
            errors.append({"rpcId": rpc_id, "code": -32002, "message": "Cannot re-stream: buffer overflow, please restart", "data": data, "sse": format_sse(response).decode()})
    errors.append({"rpcId": "x", "code": -32001, "message": UNICODE_TEXT, "data": None, "sse": format_sse(build_stream_error_response("x", -32001, UNICODE_TEXT)).decode()})
    return {"frames": frames, "errors": errors}


# ===========================================================================
# identity.json
# ===========================================================================


def _err(exc: BaseException) -> dict[str, Any]:
    name = type(exc).__name__
    return {"error": "Error" if name == "ValueError" else name, "message": str(exc)}


def _call(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"result": fn()}
    except (AipIdentityError, ValueError) as exc:
        return _err(exc)


def _cert_from_spec(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """把中立的证书描述转成 Python `ssl.getpeercert()` 结构。"""
    if spec is None:
        return None
    cert: dict[str, Any] = {}
    if spec.get("cns") is not None:
        cert["subject"] = tuple(((("commonName", cn),),) for cn in spec["cns"])
        cert["subject"] = tuple(rdn for (rdn,) in cert["subject"])
        if spec.get("org"):
            cert["subject"] = ((("organizationName", spec["org"]),),) + cert["subject"]
    if spec.get("sans") is not None:
        cert["subjectAltName"] = tuple((kind, value) for kind, value in spec["sans"])
    return cert


CERT_SPECS: list[dict[str, Any] | None] = [
    None,
    {},
    {"cns": [LEADER]},
    {"cns": [LEADER], "sans": [["URI", f"acps://{LEADER}"]]},
    {"cns": [LEADER.lower()], "sans": [["URI", f"acps://{LEADER}"]]},
    {"cns": [f"  {LEADER}  "], "sans": [["URI", f"ACPS://{LEADER.lower()}"]]},
    {"cns": [LEADER], "sans": [["URI", f"acps://{OTHER}"]]},
    {"cns": [LEADER], "sans": [["URI", f"acps://{LEADER}"], ["URI", f"acps://{OTHER}"]]},
    {"cns": [LEADER], "sans": [["URI", f"acps://{LEADER}"], ["URI", f"acps://{LEADER.lower()}"]]},
    {"cns": [LEADER], "sans": [["DNS", "partner.local"], ["IP Address", "127.0.0.1"], ["URI", "https://example.com"]]},
    {"cns": [LEADER], "sans": [["DNS", "partner.local"], ["URI", f"acps://{LEADER}"]]},
    {"cns": [LEADER], "sans": [["URI", "acps://"]]},
    {"cns": [LEADER], "sans": [["URI", "acps://not-an-aic"]]},
    {"cns": ["not-an-aic"]},
    {"cns": [f"{LEADER[:10]} {LEADER[10:]}"]},
    {"cns": [""]},
    {"sans": [["URI", f"acps://{LEADER}"]]},
    {"cns": [LEADER, OTHER]},
    {"cns": [LEADER], "org": "ACPs"},
]


def identity_fixture() -> dict[str, Any]:
    normalize_inputs = [None, "", "   ", LEADER, f"  {LEADER.lower()} \n", "\x1cabc\x1c", "\x85abc", "﻿abc", "　abc　", "a b", f"{LEADER[:10]} {LEADER[10:]}"]
    normalize = [{"input": value, "result": normalize_aic(value)} for value in normalize_inputs]

    validate_inputs = [None, "", "  ", LEADER, LEADER.lower(), f"\t{LEADER}\n", "bad-aic", f"{LEADER[:10]} {LEADER[10:]}", f"\x1c{LEADER}", f"﻿{LEADER}"]
    validate_sender = []
    validate_expected = []
    for value in validate_inputs:
        validate_sender.append({"input": value, **_call(lambda: normalize_and_validate_aic(value))})
        validate_sender.append({"input": value, "fieldName": "groupLeaderId", **_call(lambda: normalize_and_validate_aic(value, field_name="groupLeaderId"))})
        validate_expected.append({"input": value, **_call(lambda: normalize_and_validate_expected_aic(value))})
        validate_expected.append({"input": value, "fieldName": "local_aic", **_call(lambda: normalize_and_validate_expected_aic(value, field_name="local_aic"))})

    senders: list[Any] = [LEADER, LEADER.lower(), OTHER, "bad", "", None, 123, {"senderId": LEADER}, {"senderId": None}, {}, {"senderId": OTHER}, {"senderId": f" {LEADER} "}]
    peers = [LEADER, LEADER.lower(), None, "", OTHER, "bad"]
    match_peer = []
    for sender in senders:
        for peer in peers:
            match_peer.append({"sender": sender, "peer": peer, **_call(lambda: assert_sender_matches_peer(sender, peer))})
    match_expected = []
    for sender in senders:
        for expected in [LEADER, None, "bad", OTHER]:
            match_expected.append({"sender": sender, "expected": expected, **_call(lambda: assert_sender_matches_expected(sender, expected))})
    aic_matches = []
    for actual in [LEADER, LEADER.lower(), None, "", "bad", OTHER]:
        for expected in [LEADER, None, "bad"]:
            aic_matches.append({"actual": actual, "expected": expected, **_call(lambda: assert_aic_matches_expected(actual, expected))})
            aic_matches.append(
                {
                    "actual": actual,
                    "expected": expected,
                    "actualLabel": "TLS server AIC",
                    "expectedLabel": "expected_partner_aic",
                    **_call(lambda: assert_aic_matches_expected(actual, expected, actual_label="TLS server AIC", expected_label="expected_partner_aic")),
                }
            )

    certs = []
    for spec in CERT_SPECS:
        cert = _cert_from_spec(spec)
        entry: dict[str, Any] = {"spec": spec, "commonName": extract_common_name(cert)}
        entry["san"] = _call(lambda: extract_acps_uri_san(cert))
        try:
            identity = extract_peer_identity(cert)
            entry["identity"] = {"result": None if identity is None else {"aic": identity.aic, "commonName": identity.common_name, "sanAic": identity.san_aic}}
        except AipIdentityError as exc:
            entry["identity"] = _err(exc)
        certs.append(entry)

    errors = []
    for cls in [AipIdentityError, PeerAicMissingError, SenderIdentityMismatchError, InvalidPeerCertificateError]:
        for message in ["boom", "", UNICODE_TEXT]:
            errors.append({"error": cls.__name__, "message": message, "jsonrpc": compact(identity_error_to_jsonrpc(cls(message)).model_dump(exclude_none=True, mode="json"))})
    return {
        "normalize": normalize,
        "normalizeAndValidate": validate_sender,
        "normalizeAndValidateExpected": validate_expected,
        "assertSenderMatchesPeer": match_peer,
        "assertSenderMatchesExpected": match_expected,
        "assertAicMatchesExpected": aic_matches,
        "certificates": certs,
        "errorToJsonRpc": errors,
    }


# ===========================================================================
# access_status.json
# ===========================================================================


def access_status_fixture() -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = [{"id": "r", "result": merged(RESULT_MIN, status={"state": state, "stateChangedAt": NOW})} for state in ["accepted", "working", "awaiting-input", "awaiting-completion", "completed", "canceled", "failed", "rejected"]]
    for code in [-32001, -32602, -32700, -32603, -32000, -32008, -32009, 0, 404]:
        responses.append({"id": "r", "error": {"code": code, "message": f"err {code}"}})
    responses.append({"id": "r"})
    responses.append({"id": "r", "error": {"code": -32001, "message": "nf"}, "result": RESULT_MIN})
    out = []
    for value in responses:
        resp = RpcResponse.model_validate(value)
        info = error_info_from_rpc_response(resp)
        out.append(
            {
                "response": value,
                "status": status_from_rpc_response(resp),
                "errorStatus": status_from_rpc_error(resp.error),
                "errorInfo": None if info is None else info.model_dump(exclude_none=True, mode="json", by_alias=True),
            }
        )
    return out


# ===========================================================================
# rpc_scenarios.json
# ===========================================================================


def _rpc_body(command: dict[str, Any], rpc_id: Any = "rpc-1") -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": "rpc", "params": {"command": command}}
    if rpc_id is not None:
        body["id"] = rpc_id
    return body


def _cmd(command: str, task_id: str | None = "task-1", *, sender: str = LEADER, sent_at: str = NOW, cid: str = "cmd", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "task-command", "id": cid, "sentAt": sent_at, "senderRole": "leader", "senderId": sender, "command": command, "sessionId": "sess-1"}
    if task_id is not None:
        out["taskId"] = task_id
    out.update(extra)
    return out


async def _raise_handler(command: Any, task: Any) -> Any:
    raise ValueError("boom")


async def _echo_handler(command: Any, task: Any) -> Any:
    return TaskResult.model_validate(merged(RESULT_MIN, taskId=command.taskId or "none", senderId=PARTNER))


async def _forged_handler(command: Any, task: Any) -> Any:
    return TaskResult.model_validate(merged(RESULT_MIN, taskId=command.taskId or "none", senderId=OTHER))


NAMED_HANDLERS = {"raise": _raise_handler, "echo": _echo_handler, "forged": _forged_handler}


def _handlers(spec: dict[str, str] | None) -> CommandHandlers:
    spec = spec or {}
    mapping = {"onStart": "on_start", "onGet": "on_get", "onCancel": "on_cancel", "onComplete": "on_complete", "onContinue": "on_continue", "onMessage": "on_message"}
    return CommandHandlers(**{mapping[key]: NAMED_HANDLERS[value] for key, value in spec.items()})


def _strip_parse_error(dumped: dict[str, Any]) -> dict[str, Any]:
    error = dumped.get("error")
    if isinstance(error, dict) and error.get("code") == -32700:
        error.pop("data", None)
        dumped["parseErrorDataOmitted"] = True
    return dumped


RPC_SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "default handlers lifecycle without identity binding",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", cid="c1", dataItems=[{"type": "text", "text": "hello"}]))},
            {"rpc": _rpc_body(_cmd("start", cid="c2"))},
            {"rpc": _rpc_body(_cmd("get", cid="c3"))},
            {"rpc": _rpc_body(_cmd("continue", cid="c4", dataItems=[{"type": "text", "text": "more"}]))},
            {"rpc": _rpc_body(_cmd("complete", cid="c5"))},
            {"snapshot": "task-1"},
            {"rpc": _rpc_body(_cmd("cancel", cid="c6"))},
            {"rpc": _rpc_body(_cmd("cancel", cid="c7"))},
            {"rpc": _rpc_body(_cmd("complete", cid="c8"))},
            {"snapshot": "task-1"},
        ],
    },
    {
        "name": "default handlers with local AIC backfill and identity binding",
        "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": LEADER},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", cid="c1"))},
            {"rpc": _rpc_body(_cmd("get", cid="c2"))},
            {"setState": ["task-1", "awaiting-input"]},
            {"rpc": _rpc_body(_cmd("continue", cid="c3", dataItems=[{"text": "   "}]))},
            {"rpc": _rpc_body(_cmd("continue", cid="c4", dataItems=[{"text": "answer"}]))},
            {"rpc": _rpc_body(_cmd("continue", cid="c5", dataItems=[{"type": "data", "data": {}}]))},
            {"setState": ["task-1", "awaiting-completion"]},
            {"rpc": _rpc_body(_cmd("complete", cid="c6"))},
            {"rpc": _rpc_body(_cmd("cancel", cid="c7"))},
            {"snapshot": "task-1"},
        ],
    },
    {
        "name": "local AIC backfill with identity binding disabled",
        "options": {"identityBindingEnabled": False, "localAic": PARTNER},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", cid="c1"))},
            {"rpc": _rpc_body(_cmd("get", cid="c2"))},
        ],
    },
    {
        "name": "get incremental filtering",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", cid="c1", sent_at="2026-06-24T00:00:01Z"))},
            {"rpc": _rpc_body(_cmd("continue", cid="c2", sent_at="2026-06-24T00:00:02Z"))},
            {"setState": ["task-1", "working"]},
            {"setState": ["task-1", "awaiting-input"]},
            {"rpc": _rpc_body(_cmd("continue", cid="c3", sent_at="2026-06-24T00:00:03Z"))},
            {"rpc": _rpc_body(_cmd("get", cid="c4", sent_at="2026-06-24T00:00:04Z", commandParams={"lastCommandSentAt": "2026-06-24T00:00:01Z"}))},
            {"rpc": _rpc_body(_cmd("get", cid="c5", commandParams={"lastStateChangedAt": "2026-01-01T00:00:03.000003+00:00"}))},
            {"rpc": _rpc_body(_cmd("get", cid="c6", commandParams={"lastCommandSentAt": "2026-06-24T00:00:02Z", "lastStateChangedAt": "2026-01-01T00:00:01.000001+00:00"}))},
            {"rpc": _rpc_body(_cmd("get", cid="c7", commandParams={"lastCommandSentAt": "9999"}))},
            {"rpc": _rpc_body(_cmd("get", cid="c8", commandParams={"lastCommandSentAt": 5}))},
            {"rpc": _rpc_body(_cmd("get", cid="c9", commandParams={"lastCommandSentAt": ""}))},
            {"snapshot": "task-1"},
        ],
    },
    {
        "name": "error responses",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"rpc": _rpc_body(_cmd("get", "missing"))},
            {"rpc": _rpc_body(_cmd("cancel", "missing"))},
            {"rpc": _rpc_body(_cmd("complete", "missing"))},
            {"rpc": _rpc_body(_cmd("continue", "missing"))},
            {"rpc": _rpc_body(_cmd("get", None))},
            {"rpc": _rpc_body(_cmd("cancel", None), rpc_id=None)},
            {"rpc": _rpc_body(_cmd("re-stream", "missing"))},
            {"rpc": {"jsonrpc": "2.0", "method": "rpc", "id": "bad"}},
            {"rpc": [1, 2]},
            {"rpc": _rpc_body(merged(_cmd("start"), command="bogus"))},
            {"rpc": _rpc_body(_cmd("start", None))},
        ],
    },
    {
        "name": "identity binding failures",
        "options": {"identityBindingEnabled": True, "localAic": PARTNER},
        "steps": [
            {"rpc": _rpc_body(_cmd("start")), "peerAic": None},
            {"rpc": _rpc_body(_cmd("start")), "peerAic": OTHER},
            {"rpc": _rpc_body(_cmd("start", sender="bad-aic")), "peerAic": LEADER},
            {"rpc": _rpc_body(_cmd("start", sender=LEADER.lower())), "peerAic": f" {LEADER} "},
            {"rpc": _rpc_body(_cmd("get", None)), "peerAic": OTHER},
            {"rpc": _rpc_body(_cmd("get", None)), "peerAic": LEADER},
        ],
    },
    {
        "name": "identity binding requires local AIC",
        "options": {"identityBindingEnabled": True, "localAic": None, "peerAic": LEADER},
        "steps": [
            {"rpc": _rpc_body(_cmd("start"))},
            {"rpc": {"bad": True}},
        ],
    },
    {
        "name": "custom handlers",
        "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": LEADER, "handlers": {"onStart": "echo", "onGet": "raise", "onCancel": "forged", "onMessage": "echo"}},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", "task-x"))},
            {"rpc": _rpc_body(_cmd("get", "task-x"))},
            {"rpc": _rpc_body(_cmd("start", "task-y"), rpc_id=12)},
            {"setDefaultTask": "task-z"},
            {"rpc": _rpc_body(_cmd("get", "task-z"))},
            {"snapshot": "task-z"},
            {"rpc": _rpc_body(_cmd("cancel", "task-z"))},
            {"rpc": _rpc_body(_cmd("re-stream", "task-z"))},
            {"rpc": _rpc_body(_cmd("re-stream", "nope"))},
        ],
    },
    {
        "name": "custom start handler raising for a new task",
        "options": {"identityBindingEnabled": False, "handlers": {"onStart": "raise"}},
        "steps": [{"rpc": _rpc_body(_cmd("start", "fresh"))}],
    },
    {
        "name": "products size limit",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"rpc": _rpc_body(_cmd("start", "p-1"))},
            {"setProducts": ["p-1", [{"id": "prod", "dataItems": [{"text": "中文"}, {"type": "file", "bytes": "YWJj"}, {"data": {"x": 1}}]}], None]},
            {"snapshot": "p-1"},
            {"setProducts": ["p-1", [{"id": "prod", "dataItems": [{"text": "中文"}, {"type": "file", "bytes": "YWJj"}]}], 9]},
            {"snapshot": "p-1"},
            {"setProducts": ["p-1", [{"id": "prod", "dataItems": [{"text": "中文"}, {"type": "file", "bytes": "YWJj"}]}], 8]},
            {"snapshot": "p-1"},
            {"setProducts": ["missing", [], 1]},
        ],
    },
]


class _FakeRequest:
    def __init__(self, body: Any, peer_aic: str | None) -> None:
        self._body = body
        self.state = SimpleNamespace(peer_aic=peer_aic, peer_identity=None, peer_identity_error=None)

    async def json(self) -> Any:
        return self._body


async def _run_rpc_scenario(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    options = scenario["options"]
    handlers = _handlers(options.get("handlers"))
    outputs: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        if "rpc" in step:
            peer = step.get("peerAic", options.get("peerAic"))
            try:
                response = await handle_rpc_request(
                    _FakeRequest(step["rpc"], peer),  # type: ignore[arg-type]
                    handlers,
                    local_aic=options.get("localAic"),
                    identity_binding_enabled=options.get("identityBindingEnabled", True),
                )
                outputs.append({"response": compact(_strip_parse_error(response.model_dump(exclude_none=True, mode="json")))})
            except Exception as exc:  # noqa: BLE001
                outputs.append({"exception": type(exc).__name__, "message": str(exc)})
        elif "setState" in step:
            task_id, state = step["setState"]
            TaskManager.update_task_status(task_id, state)
            outputs.append({})
        elif "setDefaultTask" in step:
            TaskManager.create_task(TaskCommand.model_validate(_cmd("start", step["setDefaultTask"], cid="seed")))
            outputs.append({})
        elif "setProducts" in step:
            task_id, products, max_bytes = step["setProducts"]
            task = TaskManager.get_task(task_id)
            if task is not None and max_bytes is not None:
                object.__setattr__(task, "_aip_max_products_bytes", max_bytes)
            TaskManager.set_products(task_id, [Product.model_validate(p) for p in products])
            outputs.append({})
        elif "snapshot" in step:
            task = TaskManager.get_task(step["snapshot"])
            outputs.append({"snapshot": None if task is None else compact(task.model_dump(exclude_none=True, mode="json"))})
    return outputs


def rpc_scenarios_fixture() -> list[dict[str, Any]]:
    out = []
    for scenario in RPC_SCENARIOS:
        TaskManager._tasks.clear()
        with deterministic():
            outputs = asyncio.run(_run_rpc_scenario(scenario))
        TaskManager._tasks.clear()
        out.append({**scenario, "outputs": outputs})
    return out


# ===========================================================================
# stream_scenarios.json
# ===========================================================================


def _stream_body(command: dict[str, Any], rpc_id: Any = "stream-1") -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": "stream", "params": {"message": command}}
    if rpc_id is not None:
        body["id"] = rpc_id
    return body


def _result(task_id: str, state: str, *, sender: str = PARTNER, rid: str = "res") -> dict[str, Any]:
    return merged(RESULT_MIN, id=rid, taskId=task_id, senderId=sender, status={"state": state, "stateChangedAt": NOW})


STREAM_SCENARIOS: list[dict[str, Any]] = [
    {"name": "start replays buffered events and ends on terminal", "options": {"identityBindingEnabled": False}, "publish": [["task-1", _result("task-1", "working", rid="r1")], ["task-1", _result("task-1", "completed", rid="r2")]], "request": _stream_body(_cmd("start"))},
    {"name": "start publishes from on_stream_start", "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": LEADER}, "onStart": [["task-1", _result("task-1", "completed")]], "request": _stream_body(_cmd("start"), rpc_id=3)},
    {"name": "start without task id", "options": {"identityBindingEnabled": False}, "request": _stream_body(_cmd("start", None))},
    {"name": "unsupported command", "options": {"identityBindingEnabled": False}, "request": _stream_body(_cmd("get"))},
    {"name": "parse error", "options": {"identityBindingEnabled": False}, "request": {"invalid": "body"}},
    {"name": "re-stream not found", "options": {"identityBindingEnabled": False}, "request": _stream_body(_cmd("re-stream", "nope"))},
    {"name": "re-stream without task id", "options": {"identityBindingEnabled": False}, "request": _stream_body(_cmd("re-stream", None))},
    {"name": "re-stream without task id recovered by on_re_stream", "options": {"identityBindingEnabled": False}, "reStreamChannel": True, "request": _stream_body(_cmd("re-stream", None))},
    {"name": "re-stream recovered channel not registered in hub", "options": {"identityBindingEnabled": False}, "reStreamChannel": True, "request": _stream_body(_cmd("re-stream", "lost"))},
    {
        "name": "re-stream resumes after lastEventSeq",
        "options": {"identityBindingEnabled": False},
        "publish": [["task-1", _result("task-1", "working", rid="r1")], ["task-1", STATUS_EVENT], ["task-1", CHUNK_EVENT], ["task-1", _result("task-1", "completed", rid="r4")]],
        "request": _stream_body(_cmd("re-stream", commandParams={"lastEventSeq": 2}), rpc_id=None),
    },
    {
        "name": "re-stream without lastEventSeq replays all",
        "options": {"identityBindingEnabled": False},
        "publish": [["task-1", _result("task-1", "working", rid="r1")], ["task-1", _result("task-1", "failed", rid="r2")]],
        "request": _stream_body(_cmd("re-stream", commandParams={})),
    },
    {
        "name": "re-stream buffer overflow",
        "options": {"identityBindingEnabled": False},
        "maxBuffer": 2,
        "publish": [["task-1", _result("task-1", "working", rid=f"r{i}")] for i in range(3)] + [["task-1", _result("task-1", "completed", rid="r3")]],
        "request": _stream_body(_cmd("re-stream", commandParams={"lastEventSeq": 1})),
    },
    {
        "name": "re-stream exactly at buffer edge",
        "options": {"identityBindingEnabled": False},
        "maxBuffer": 2,
        "publish": [["task-1", _result("task-1", "working", rid=f"r{i}")] for i in range(3)] + [["task-1", _result("task-1", "completed", rid="r3")]],
        "request": _stream_body(_cmd("re-stream", commandParams={"lastEventSeq": 2})),
    },
    {"name": "identity missing peer", "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": None}, "request": _stream_body(_cmd("start"))},
    {"name": "identity sender mismatch", "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": OTHER}, "request": _stream_body(_cmd("start"))},
    {"name": "identity invalid sender", "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": LEADER}, "request": _stream_body(_cmd("start", sender="bad"))},
    {"name": "identity requires local AIC", "options": {"identityBindingEnabled": True, "localAic": None, "peerAic": LEADER}, "request": _stream_body(_cmd("start"))},
    {
        "name": "outbound event sender mismatch",
        "options": {"identityBindingEnabled": True, "localAic": PARTNER, "peerAic": LEADER},
        "publish": [["task-1", _result("task-1", "working", rid="ok")], ["task-1", _result("task-1", "completed", sender=OTHER, rid="forged")]],
        "request": _stream_body(_cmd("re-stream")),
    },
]


async def _run_stream_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    options = scenario["options"]
    hub = StreamHub(max_buffer=scenario.get("maxBuffer", 200))
    models = {"task-result": TaskResult, "task-command": TaskCommand, "task-status-update": TaskStatusUpdateEvent, "product-chunk": ProductChunkEvent}
    for task_id, payload in scenario.get("publish", []):
        model = models[payload["type"]].model_validate(payload)
        if isinstance(model, TaskResult):
            await hub.publish_task_result(task_id, model)
        else:
            await hub.get_or_create_channel(task_id).publish(model)

    async def on_start(command: Any) -> None:
        for task_id, payload in scenario.get("onStart", []):
            await hub.publish_task_result(task_id, TaskResult.model_validate(payload))

    async def on_re_stream(command: Any) -> TaskStreamChannel | None:
        return TaskStreamChannel() if scenario.get("reStreamChannel") else None

    try:
        response = await handle_stream_request(
            _FakeRequest(scenario["request"], options.get("peerAic")),  # type: ignore[arg-type]
            hub,
            StreamHandlers(on_stream_start=on_start, on_re_stream=on_re_stream),
            local_aic=options.get("localAic"),
            identity_binding_enabled=options.get("identityBindingEnabled", True),
        )
    except HTTPException as exc:
        detail = dict(exc.detail)
        if detail.get("code") == -32700:
            detail.pop("data", None)
            return {"status": exc.status_code, "detail": detail, "parseErrorDataOmitted": True}
        return {"status": exc.status_code, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"exception": type(exc).__name__, "message": str(exc)}

    frames: list[str] = []
    try:
        async for chunk in response.body_iterator:
            frames.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    except AipIdentityError as exc:
        return {"status": response.status_code, "mediaType": response.media_type, "frames": frames, "streamError": type(exc).__name__, "streamErrorMessage": str(exc)}
    return {"status": response.status_code, "mediaType": response.media_type, "frames": frames}


def stream_scenarios_fixture() -> list[dict[str, Any]]:
    return [{**scenario, "outcome": asyncio.run(_run_stream_scenario(scenario))} for scenario in STREAM_SCENARIOS]


# ===========================================================================
# notification_scenarios.json
# ===========================================================================


def _set(task_id: str = "task-1", *, url: str = "https://leader/cb", cid: str | None = None, rid: Any = "1") -> dict[str, Any]:
    params: dict[str, Any] = {"url": url, "token": "tok", "taskId": task_id}
    if cid is not None:
        params = {"id": cid, **params}
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": "notification/set", "params": params}
    if rid is not None:
        body["id"] = rid
    return body


def _idreq(method: str, task_id: str = "task-1", cid: str | None = None, rid: Any = "2") -> dict[str, Any]:
    params: dict[str, Any] = {"taskId": task_id}
    if cid is not None:
        params["notificationConfigId"] = cid
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": f"notification/{method}", "params": params}
    if rid is not None:
        body["id"] = rid
    return body


def _start(task_id: str | None = "task-1", params: Any = None, *, sender: str = LEADER, rid: Any = "3") -> dict[str, Any]:
    command = _cmd("start", task_id, sender=sender)
    if params is not None:
        command["commandParams"] = params
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": "notification/start", "params": {"message": command}}
    if rid is not None:
        body["id"] = rid
    return body


CFG1 = "notification-00000000-0000-0000-0000-000000000001"

NOTIFICATION_SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "crud without identity binding",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"method": "set", "body": _set()},
            {"method": "set", "body": _set(url="https://leader/cb2", rid=None)},
            {"method": "set", "body": _set(cid="fixed", rid=5)},
            {"method": "set", "body": _set(cid="fixed", url="https://leader/updated")},
            {"method": "get", "body": _idreq("get")},
            {"method": "get", "body": _idreq("get", cid="fixed")},
            {"method": "get", "body": _idreq("get", cid="missing")},
            {"method": "get", "body": _idreq("get", task_id="other", rid=None)},
            {"method": "delete", "body": _idreq("delete", cid="fixed")},
            {"method": "delete", "body": _idreq("delete", cid="fixed")},
            {"method": "delete", "body": _idreq("delete")},
            {"method": "delete", "body": _idreq("delete")},
            {"method": "get", "body": _idreq("get")},
        ],
    },
    {
        "name": "parse errors",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"method": "set", "body": {"bad": "data"}},
            {"method": "set", "body": {**_set(), "method": "notification/get"}},
            {"method": "delete", "body": {"params": {}}},
            {"method": "get", "body": []},
            {"method": "start", "body": {"params": {"message": {"command": "start"}}}},
        ],
    },
    {
        "name": "start validation",
        "options": {"identityBindingEnabled": False},
        "steps": [
            {"method": "set", "body": _set()},
            {"method": "start", "body": _start(None, {"notificationConfigId": CFG1})},
            {"method": "start", "body": _start("task-1", None)},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": ""})},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": "missing"})},
            {"method": "start", "body": _start("task-2", {"notificationConfigId": CFG1})},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1})},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1}, rid=None)},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1, "notifyOnStates": ["completed", "failed"]}, rid=7)},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1, "notifyOnStates": ["completed", "bogus"]})},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1, "notifyOnStates": []})},
            {"matches": ["task-1", "working"]},
            {"matches": ["task-1", "completed"]},
            {"matches": ["task-2", "completed"]},
        ],
    },
    {
        "name": "owner scoping with identity binding",
        "options": {"identityBindingEnabled": True, "localAic": PARTNER},
        "steps": [
            {"method": "set", "body": _set(), "peerAic": None},
            {"method": "set", "body": _set(), "peerAic": "bad-aic"},
            {"method": "set", "body": _set(), "peerAic": LEADER.lower()},
            {"method": "get", "body": _idreq("get"), "peerAic": LEADER},
            {"method": "get", "body": _idreq("get", cid=CFG1), "peerAic": OTHER},
            {"method": "delete", "body": _idreq("delete", cid=CFG1), "peerAic": OTHER},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1}, sender=OTHER), "peerAic": LEADER},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1}, sender=OTHER), "peerAic": OTHER},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1}), "peerAic": LEADER},
            {"method": "start", "body": _start("task-1", {"notificationConfigId": CFG1}), "peerAic": None},
            {"matches": ["task-1", "completed"]},
            {"method": "delete", "body": _idreq("delete", cid=CFG1), "peerAic": LEADER},
        ],
    },
]


def _match_view(subs: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "taskId": sub.task_id,
            "configId": sub.config_id,
            "ownerAic": sub.owner_aic,
            "notifyOnStates": None if sub.notify_on_states is None else [s.value for s in sub.notify_on_states],
        }
        for sub in subs
    ]


def _notification_app(service: NotificationService) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_peer(request: Request, call_next: Any) -> Any:
        request.state.peer_aic = request.headers.get("x-peer-aic")
        request.state.peer_identity = None
        request.state.peer_identity_error = None
        return await call_next(request)

    add_aip_notification_router(app, service)
    return app


def _response_json(response: httpx.Response) -> Any:
    body = response.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("code") == -32700:
        detail.pop("data", None)
        return {"body": body, "parseErrorDataOmitted": True}
    return {"body": body}


def notification_scenarios_fixture() -> list[dict[str, Any]]:
    out = []
    for scenario in NOTIFICATION_SCENARIOS:
        options = scenario["options"]
        outputs: list[dict[str, Any]] = []
        with deterministic():
            service = NotificationService(
                local_aic=options.get("localAic"),
                identity_binding_enabled=options["identityBindingEnabled"],
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
            with TestClient(_notification_app(service)) as client:
                for step in scenario["steps"]:
                    if "matches" in step:
                        task_id, state = step["matches"]
                        outputs.append({"matches": _match_view(service.registry.matches(task_id, state))})
                        continue
                    headers = {}
                    peer = step.get("peerAic", options.get("peerAic"))
                    if peer is not None:
                        headers["x-peer-aic"] = peer
                    response = client.post(f"/notification/{step['method']}", json=step["body"], headers=headers)
                    outputs.append({"status": response.status_code, **_response_json(response)})
        out.append({**scenario, "outputs": outputs})
    return out


def dispatch_fixture() -> list[dict[str, Any]]:
    """NotificationDispatcher 发出的回调请求（按订阅顺序）。"""
    cases = []
    specs = [
        {"name": "two subscriptions with state filter", "states": [None, ["completed"]], "result": _result("task-1", "completed")},
        {"name": "state filter excludes working", "states": [["completed"]], "result": _result("task-1", "working")},
        {"name": "full task result body", "states": [None], "result": merged(RESULT_FULL, taskId="task-1")},
    ]
    for spec in specs:
        captured: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            captured.append({"url": str(request.url), "token": request.headers.get("X-ACPs-AIP-Notification-Token"), "contentType": request.headers.get("content-type"), "body": request.content.decode()})
            return httpx.Response(204)

        service = NotificationService(identity_binding_enabled=False, transport=httpx.MockTransport(_handler))
        configs = []
        for index, states in enumerate(spec["states"]):
            cfg = service.store.set(NotificationConfig(id=f"cfg-{index}", url=f"https://leader/cb/{index}", token=f"tok-{index}", taskId="task-1"))
            configs.append(cfg)
            from acps_sdk.aip.aip_notification_server import NotificationSubscription

            service.registry.add(NotificationSubscription(task_id="task-1", config_id=cfg.id, notify_on_states=None if states is None else [s for s in states]))  # type: ignore[arg-type]
        asyncio.run(service.dispatch(TaskResult.model_validate(spec["result"])))
        cases.append({"name": spec["name"], "states": spec["states"], "result": spec["result"], "requests": captured})
    return cases


def receiver_fixture() -> list[dict[str, Any]]:
    cases = []
    body_ok = compact(_result("task-1", "completed"))
    body_forged = compact(_result("task-1", "completed", sender=OTHER))
    specs = [
        {"name": "valid token", "binding": False, "body": body_ok, "token": "tok"},
        {"name": "legacy header casing", "binding": False, "body": body_ok, "tokenHeader": "X-ACPS-AIP-Notification-Token", "token": "tok"},
        {"name": "wrong token", "binding": False, "body": body_ok, "token": "nope"},
        {"name": "missing token", "binding": False, "body": body_ok},
        {"name": "empty token", "binding": False, "body": body_ok, "token": ""},
        {"name": "malformed body", "binding": False, "body": '{"not": "a task result"}', "token": "tok"},
        {"name": "invalid json", "binding": False, "body": "{", "token": "tok"},
        {"name": "binding ok", "binding": True, "body": body_ok, "token": "tok", "peerAic": PARTNER},
        {"name": "binding missing peer", "binding": True, "body": body_ok, "token": "tok"},
        {"name": "binding forged sender", "binding": True, "body": body_forged, "token": "tok", "peerAic": PARTNER},
        {"name": "binding checked before token", "binding": True, "body": body_forged, "token": "nope", "peerAic": PARTNER},
    ]
    for spec in specs:
        received: list[str] = []

        async def _handler(task_result: TaskResult) -> None:
            received.append(task_result.taskId)

        receiver = NotificationReceiver(token="tok", handler=_handler, identity_binding_enabled=spec["binding"])
        app = FastAPI()

        @app.middleware("http")
        async def _inject_peer(request: Request, call_next: Any) -> Any:
            request.state.peer_aic = request.headers.get("x-peer-aic")
            request.state.peer_identity = None
            request.state.peer_identity_error = None
            return await call_next(request)

        receiver.mount(app, "/callback")
        headers = {"Content-Type": "application/json"}
        if "token" in spec:
            headers[spec.get("tokenHeader", "X-ACPs-AIP-Notification-Token")] = spec["token"]
        if "peerAic" in spec:
            headers["x-peer-aic"] = spec["peerAic"]
        with TestClient(app) as client:
            response = client.post("/callback", content=spec["body"].encode(), headers=headers)
        body = response.json()
        entry: dict[str, Any] = {**spec, "status": response.status_code}
        if isinstance(body.get("detail"), str) and body["detail"].startswith("Parse error:"):
            entry["parseError"] = True
        else:
            entry["responseBody"] = body
        entry["handled"] = len(received)
        cases.append(entry)
    return cases


# ===========================================================================
# client_requests.json
# ===========================================================================


class _Recorder:
    def __init__(self, responder: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "method": request.method, "contentType": request.headers.get("content-type"), "accept": request.headers.get("accept"), "body": compact(body)})
        return httpx.Response(200, json=self._responder(body))


def client_requests_fixture() -> dict[str, Any]:
    out: dict[str, Any] = {}

    # --- AipRpcClient ---
    def rpc_responder(body: dict[str, Any]) -> dict[str, Any]:
        command = body["params"]["command"]
        return {"jsonrpc": "2.0", "id": body["id"], "result": _result(command["taskId"], "working", sender="agent")}

    with deterministic():
        recorder = _Recorder(rpc_responder)
        client = AipRpcClient(partner_url="http://partner/rpc", leader_id="leader-1", transport=httpx.MockTransport(recorder), identity_binding_enabled=False)

        async def _rpc() -> list[Any]:
            results = [
                await client.start_task("sess-1", "hello"),
                await client.start_task("sess-1", "", task_id="t-fixed"),
                await client.continue_task("t-fixed", "sess-1", "more"),
                await client.complete_task("t-fixed", "sess-1"),
                await client.cancel_task("t-fixed", "sess-1"),
                await client.get_task("t-fixed", "sess-1"),
            ]
            await client.close()
            return [compact(r.model_dump(exclude_none=True, mode="json")) for r in results]

        results = asyncio.run(_rpc())
    out["rpc"] = {"requests": recorder.requests, "results": results}

    # --- AipStreamClient ---
    sse = "".join(
        format_sse(StreamResponse(id="s", result=StreamEventData(eventSeq=i, eventData=TaskResult.model_validate(_result("t-s", state, sender="agent"))))).decode()
        for i, state in [(1, "working"), (2, "completed")]
    )

    def _stream_handler(request: httpx.Request) -> httpx.Response:
        stream_recorder.requests.append({"url": str(request.url), "method": request.method, "contentType": request.headers.get("content-type"), "accept": request.headers.get("accept"), "body": compact(json.loads(request.content))})
        if request.url.path.endswith("/rpc"):
            body = json.loads(request.content)
            return httpx.Response(200, json=rpc_responder(body))
        return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})

    with deterministic():
        stream_recorder = _Recorder(lambda body: {})
        stream_client = AipStreamClient(partner_stream_url="http://partner/stream", partner_rpc_url="http://partner/rpc", leader_id="leader-1", transport=httpx.MockTransport(_stream_handler), identity_binding_enabled=False)

        async def _stream() -> list[Any]:
            collected = []
            async for event in stream_client.start_stream("sess-1", text_content="hi"):
                collected.append(compact(event.model_dump(exclude_none=True, mode="json")))
            async for event in stream_client.start_stream("sess-1", task_id="t-s"):
                collected.append(compact(event.model_dump(exclude_none=True, mode="json")))
            async for event in stream_client.re_stream("t-s", "sess-1", last_event_seq=1):
                collected.append(compact(event.model_dump(exclude_none=True, mode="json")))
            async for event in stream_client.re_stream("t-s", "sess-1"):
                collected.append(compact(event.model_dump(exclude_none=True, mode="json")))
            collected.append(compact((await stream_client.complete("t-s", "sess-1")).model_dump(exclude_none=True, mode="json")))
            collected.append(compact((await stream_client.cancel("t-s", "sess-1")).model_dump(exclude_none=True, mode="json")))
            collected.append(stream_client.last_event_seq)
            await stream_client.close()
            return collected

        stream_results = asyncio.run(_stream())
    out["stream"] = {"requests": stream_recorder.requests, "results": stream_results}

    # --- AipNotificationClient ---
    def notif_responder(body: dict[str, Any]) -> dict[str, Any]:
        method = body["method"]
        if method == "notification/set":
            params = body["params"]
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"id": params.get("id") or "cfg-new", **{k: v for k, v in params.items() if k != "id"}}}
        if method == "notification/get":
            return {"jsonrpc": "2.0", "id": body["id"], "result": [CONFIG_FULL]}
        if method == "notification/delete":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"success": True}}
        return {"jsonrpc": "2.0", "id": body["id"], "result": True}

    with deterministic():
        notif_recorder = _Recorder(notif_responder)
        notif_client = AipNotificationClient(partner_url="http://partner/", leader_id="leader-1", transport=httpx.MockTransport(notif_recorder), identity_binding_enabled=False)

        async def _notif() -> list[Any]:
            results: list[Any] = [
                compact((await notif_client.set_config("task-1", "https://leader/cb", "tok")).model_dump(exclude_none=True, mode="json")),
                compact((await notif_client.set_config("task-1", "https://leader/cb", "tok", config_id="cfg-9")).model_dump(exclude_none=True, mode="json")),
                await notif_client.delete_config("task-1"),
                await notif_client.delete_config("task-1", "cfg-9"),
                [compact(c.model_dump(exclude_none=True, mode="json")) for c in await notif_client.get_configs("task-1")],
                [compact(c.model_dump(exclude_none=True, mode="json")) for c in await notif_client.get_configs("task-1", "cfg-1")],
                await notif_client.start_notification("task-1", "cfg-1", "sess-1"),
                await notif_client.start_notification("task-1", "cfg-1", "sess-1", notify_on_states=["completed", "failed"]),
                await notif_client.start_notification("task-1", "cfg-1", "sess-1", notify_on_states=[]),
            ]
            await notif_client.close()
            return results

        notif_results = asyncio.run(_notif())
    out["notification"] = {"requests": notif_recorder.requests, "results": notif_results}
    return out


def main() -> None:
    write_fixture("aip", "models", models_fixture())
    write_fixture("aip", "data_item_union", data_item_union_fixture())
    write_fixture("aip", "sse", sse_fixture())
    write_fixture("aip", "identity", identity_fixture())
    write_fixture("aip", "access_status", access_status_fixture())
    write_fixture("aip", "rpc_scenarios", rpc_scenarios_fixture())
    write_fixture("aip", "stream_scenarios", stream_scenarios_fixture())
    write_fixture("aip", "notification_scenarios", notification_scenarios_fixture())
    write_fixture("aip", "notification_dispatch", dispatch_fixture())
    write_fixture("aip", "notification_receiver", receiver_fixture())
    write_fixture("aip", "client_requests", client_requests_fixture())


if __name__ == "__main__":
    main()
