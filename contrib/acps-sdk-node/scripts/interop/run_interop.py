"""ACPs SDK 跨语言互通测试：官方 Python SDK（../../acps-sdk）↔ Node SDK（本包 dist/）。

两边互为对端跑同一套场景（真实 mTLS、真实 RabbitMQ），逐项断言线上数据与身份绑定行为，
最后打印「场景 × 方向」矩阵；有任何 FAIL 时退出码为 1。用法见同目录 README.md。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

# 以 `python -I` 运行时脚本目录不在 sys.path 上；不在仓库里留下 __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

import aio_pika  # noqa: E402

from acps_sdk.aip.aip_base_model import TaskCommand, TaskResult, TaskStatus, TextDataItem  # noqa: E402
from acps_sdk.aip.aip_group_runtime import build_plain_connection_url  # noqa: E402
from interop_common import (  # noqa: E402
    INPUT_PROMPT,
    LEADER_AIC,
    NEED_INPUT_MARK,
    OTHER_AIC,
    PARTNER_AIC,
    Checker,
    CheckFailed,
    Pki,
    ScenarioResult,
    create_pki,
    expected_products,
    rpc_error_code,
)
from interop_node import NodeSide  # noqa: E402
from interop_py import PySide  # noqa: E402

LANG_LABEL = {"python": "Py", "node": "Node"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self, *, verbose: bool) -> None:
        self.verbose = verbose
        self.results: list[ScenarioResult] = []

    @contextlib.asynccontextmanager
    async def scenario(self, name: str, direction: str) -> AsyncIterator[Checker]:
        result = ScenarioResult(scenario=name, direction=direction)
        self.results.append(result)
        print(f"  -> {name}", flush=True)
        checker = Checker(result, verbose=self.verbose)
        try:
            yield checker
        except CheckFailed as exc:
            result.status = "FAIL"
            result.note = str(exc)
        except Exception as exc:  # noqa: BLE001
            result.status = "FAIL"
            result.note = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        else:
            if any(not passed for _, passed, _ in result.checks):
                result.status = "FAIL"
                result.note = next(f"{n}: {d}" for n, passed, d in result.checks if not passed)
            elif result.xfails:
                result.status = "XFAIL"
                result.note = "; ".join(sorted({reason for _, reason in result.xfails}))
        count = f"{len(result.checks)} checks" + (f", {len(result.xfails)} xfail" if result.xfails else "")
        print(f"     {result.status} ({count}){' :: ' + result.note[:300] if result.note else ''}", flush=True)

    def skip(self, name: str, direction: str, note: str) -> None:
        self.results.append(ScenarioResult(scenario=name, direction=direction, status="SKIP", note=note))
        print(f"  -> {name}\n     SKIP :: {note}", flush=True)


class Run:
    """一个方向（Leader 语言 × Partner 语言）上的场景上下文。"""

    def __init__(
        self,
        runner: Runner,
        pki: Pki,
        broker: dict[str, Any] | None,
        leader: Any,
        partner: Any,
    ) -> None:
        self.runner = runner
        self.pki = pki
        self.broker = broker
        self.leader = leader
        self.partner = partner
        self.direction = f"{LANG_LABEL[leader.lang]} Leader → {LANG_LABEL[partner.lang]} Partner"
        self.tag = f"{leader.lang[:2]}2{partner.lang[:2]}"

    def uid(self, prefix: str) -> str:
        return f"{prefix}-{self.tag}-{uuid.uuid4().hex[:8]}"

    def scenario(self, name: str):  # type: ignore[no-untyped-def]
        return self.runner.scenario(name, self.direction)


def must(ck: Checker, label: str, outcome: dict[str, Any]) -> Any:
    ck.check(f"{label} succeeds", bool(outcome.get("ok")), outcome.get("error"))
    return outcome["result"]


def fails_with(ck: Checker, label: str, outcome: dict[str, Any]) -> dict[str, Any]:
    ck.check(f"{label} is rejected", not outcome.get("ok"), outcome.get("result"))
    return outcome.get("error") or {}


def rpc_body(task_id: str, session_id: str, command: str, sender_id: str, text: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "task-command",
        "id": f"cmd-{uuid.uuid4()}",
        "sentAt": _now(),
        "senderRole": "leader",
        "senderId": sender_id,
        "sessionId": session_id,
        "command": command,
        "taskId": task_id,
    }
    if text:
        message["dataItems"] = [{"type": "text", "text": text}]
    return message


# ---------------------------------------------------------------------------
# 场景：Direct RPC
# ---------------------------------------------------------------------------


async def flow_rpc(rn: Run) -> None:
    leader, partner, pki = rn.leader, rn.partner, rn.pki
    urls = await partner.serve_partner()
    rpc_url = f"{urls['baseUrl']}/rpc"
    permissive_url = f"{urls['permissiveBaseUrl']}/rpc"
    L = pki.leader

    async with rn.scenario("direct-rpc: start→get→continue→complete, cancel") as ck:
        sid = rn.uid("sess")
        t1 = rn.uid("rpc-input")
        text1 = f"请处理 {NEED_INPUT_MARK} ✓"
        res = must(ck, "start", await leader.rpc(rpc_url, "start", t1, sid, text=text1, identity=L))
        ck.equal("start: senderId = Partner cert AIC", res.get("senderId"), PARTNER_AIC)
        ck.equal("start: senderRole", res.get("senderRole"), "partner")
        ck.equal("start: type", res.get("type"), "task-result")
        ck.equal("start: taskId echoed", res.get("taskId"), t1)
        ck.equal("start: sessionId echoed", res.get("sessionId"), sid)
        ck.equal("start: state", res["status"]["state"], "awaiting-input")
        ck.equal("start: prompt dataItems", res["status"].get("dataItems"), [{"type": "text", "text": INPUT_PROMPT}])
        history = res.get("commandHistory") or []
        ck.equal("start: commandHistory[0].senderId = Leader AIC", [c.get("senderId") for c in history], [LEADER_AIC])
        ck.equal(
            "start: command text round-trips (non-ASCII)",
            history[0].get("dataItems") if history else None,
            [{"type": "text", "text": text1}],
        )

        res = must(ck, "get", await leader.rpc(rpc_url, "get", t1, sid, identity=L))
        ck.equal("get: state", res["status"]["state"], "awaiting-input")

        text2 = "补充信息 ✓"
        res = must(ck, "continue", await leader.rpc(rpc_url, "continue", t1, sid, text=text2, identity=L))
        ck.equal("continue: state", res["status"]["state"], "awaiting-completion")
        ck.equal("continue: products (text/data with nested null/file base64)", res.get("products"), expected_products(t1, text2))

        res = must(ck, "get after continue", await leader.rpc(rpc_url, "get", t1, sid, identity=L))
        ck.equal("get: products", res.get("products"), expected_products(t1, text2))

        res = must(ck, "complete", await leader.rpc(rpc_url, "complete", t1, sid, identity=L))
        ck.equal("complete: state", res["status"]["state"], "completed")
        ck.equal("complete: senderId", res.get("senderId"), PARTNER_AIC)

        res = must(ck, "get after complete", await leader.rpc(rpc_url, "get", t1, sid, identity=L))
        ck.equal("final: state", res["status"]["state"], "completed")
        ck.equal(
            "final: commandHistory",
            [c.get("command") for c in res.get("commandHistory") or []],
            ["start", "continue", "complete"],
        )
        ck.equal(
            "final: statusHistory",
            [s.get("state") for s in res.get("statusHistory") or []],
            ["awaiting-input", "awaiting-completion", "completed"],
        )

        t2 = rn.uid("rpc-cancel")
        res = must(ck, "start (cancel case)", await leader.rpc(rpc_url, "start", t2, sid, text="直接处理", identity=L))
        ck.equal("start: state", res["status"]["state"], "awaiting-completion")
        ck.equal("start: products", res.get("products"), expected_products(t2, "直接处理"))
        res = must(ck, "cancel", await leader.rpc(rpc_url, "cancel", t2, sid, identity=L))
        ck.equal("cancel: state", res["status"]["state"], "canceled")
        res = must(ck, "cancel again (idempotent)", await leader.rpc(rpc_url, "cancel", t2, sid, identity=L))
        ck.equal("cancel again: state", res["status"]["state"], "canceled")
        res = must(ck, "complete after cancel", await leader.rpc(rpc_url, "complete", t2, sid, identity=L))
        ck.equal("complete after cancel: state unchanged", res["status"]["state"], "canceled")

        error = fails_with(ck, "get unknown task", await leader.rpc(rpc_url, "get", rn.uid("missing"), sid, identity=L))
        ck.equal("get unknown task: JSON-RPC code", rpc_error_code(error), -32001)

    async with rn.scenario("direct-rpc: identity binding negatives") as ck:
        sid = rn.uid("sess")
        error = fails_with(
            ck,
            "SDK client with senderId ≠ its cert AIC",
            await leader.rpc(rpc_url, "start", rn.uid("forged"), sid, text="x", identity=L, leader_id=OTHER_AIC),
        )
        ck.equal("forged senderId: JSON-RPC code", rpc_error_code(error), -32009)

        body = {
            "jsonrpc": "2.0",
            "id": "rpc-forged-1",
            "method": "rpc",
            "params": {"command": rpc_body(rn.uid("forged"), sid, "start", OTHER_AIC, "x")},
        }
        raw = must(ck, "raw forged POST", await leader.raw_post(rpc_url, body, identity=L))
        ck.equal("raw forged: HTTP status", raw["status"], 200)
        ck.equal("raw forged: id echoed", raw["body"].get("id"), "rpc-forged-1")
        ck.equal(
            "raw forged: error code/message",
            {k: raw["body"].get("error", {}).get(k) for k in ("code", "message")},
            {"code": -32009, "message": "AuthorizationFailedError"},
        )
        ck.check("raw forged: no result", raw["body"].get("result") is None, raw["body"])

        error = fails_with(
            ck,
            "no client certificate (cert-optional listener)",
            await leader.rpc(permissive_url, "start", rn.uid("nocert"), sid, text="x", identity=None),
        )
        ck.equal("no client cert: JSON-RPC code", rpc_error_code(error), -32008)
        body["params"]["command"] = rpc_body(rn.uid("nocert"), sid, "start", LEADER_AIC, "x")
        raw = must(ck, "raw POST without client cert", await leader.raw_post(permissive_url, body, identity=None))
        ck.equal(
            "raw no-cert: error code/message",
            {k: raw["body"].get("error", {}).get(k) for k in ("code", "message")},
            {"code": -32008, "message": "AuthenticationRequiredError"},
        )

        error = fails_with(
            ck,
            "expectedPartnerAic ≠ server cert AIC",
            await leader.rpc(rpc_url, "start", rn.uid("exp"), sid, text="x", identity=L, expected=OTHER_AIC),
        )
        ck.check(
            "expected mismatch: SenderIdentityMismatchError on TLS server AIC",
            error.get("name") == "SenderIdentityMismatchError"
            and "TLS server AIC" in str(error.get("message"))
            and "expected_partner_aic" in str(error.get("message")),
            error,
        )

        outcome = await leader.rpc(
            permissive_url, "start", rn.uid("rogue"), sid, text="x", identity=pki.rogue_leader
        )
        rogue_error = outcome.get("error") or {}
        rogue_message = str(rogue_error.get("message", ""))
        if partner.lang == "node":
            # Node 服务端完成握手后按身份绑定拒绝：-32008 AuthenticationRequired
            rejected_for_identity = "-32008" in rogue_message
        else:
            # Python（CERT_OPTIONAL）在 TLS 握手阶段就拒绝未知 CA 的证书
            lowered = rogue_message.lower()
            rejected_for_identity = any(
                marker in lowered
                for marker in ("certificate", "ssl", "tls", "alert", "handshake", "econnreset", "socket hang up")
            )
        ck.check(
            "untrusted client cert (CN=Leader AIC, unknown CA) is not accepted as identity",
            not outcome.get("ok") and rejected_for_identity,
            outcome,
        )


# ---------------------------------------------------------------------------
# 场景：Streaming（SSE）
# ---------------------------------------------------------------------------


def _event_data(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f["result"]["eventData"] for f in frames]


async def flow_stream(rn: Run) -> None:
    leader, partner, pki = rn.leader, rn.partner, rn.pki
    urls = await partner.serve_partner()
    stream_url = f"{urls['baseUrl']}/stream"
    rpc_url = f"{urls['baseUrl']}/rpc"
    L = pki.leader

    async with rn.scenario("stream: start → drop → re-stream(lastEventSeq) → complete → replay") as ck:
        sid = rn.uid("sess")
        task_id = rn.uid("stream")
        text = "流式任务 ✓"
        first = must(
            ck,
            "stream start (stop after 2 events)",
            await leader.stream_collect(stream_url, rpc_url, "start", task_id, sid, text=text, stop_after=2, identity=L),
        )
        frames = first["events"]
        ck.equal("first connection: eventSeq", [f.get("result", {}).get("eventSeq") for f in frames], [1, 2])
        ck.check("frames: jsonrpc 2.0 and no error", all(f.get("jsonrpc") == "2.0" and f.get("error") is None for f in frames), frames)
        data = _event_data(frames)
        ck.equal("first connection: event types", [d.get("type") for d in data], ["task-result", "task-status-update"])
        ck.equal("first connection: states", [d["status"]["state"] for d in data], ["accepted", "working"])
        ck.check("all events: senderId = Partner AIC", all(d.get("senderId") == PARTNER_AIC for d in data), data)
        ck.equal("client lastEventSeq after drop", first["lastEventSeq"], 2)

        second = must(
            ck,
            "re-stream from lastEventSeq=2 (+ complete via RPC on awaiting-completion)",
            await leader.stream_collect(
                stream_url, rpc_url, "restream", task_id, sid, last_event_seq=2, complete_on_awaiting=True, identity=L
            ),
        )
        frames = second["events"]
        ck.equal("re-stream: eventSeq resumes at last+1", [f.get("result", {}).get("eventSeq") for f in frames], [3, 4, 5, 6])
        data = _event_data(frames)
        ck.equal(
            "re-stream: event types",
            [d.get("type") for d in data],
            ["product-chunk", "product-chunk", "task-result", "task-result"],
        )
        ck.equal(
            "chunks: append/lastChunk flags",
            [(d.get("append"), d.get("lastChunk")) for d in data[:2]],
            [(False, False), (True, True)],
        )
        ck.equal(
            "chunks: reassembled text",
            "".join(item["text"] for d in data[:2] for item in d["product"]["dataItems"]),
            f"已处理：{text}",
        )
        ck.equal("awaiting-completion event: state", data[2]["status"]["state"], "awaiting-completion")
        ck.equal("awaiting-completion event: products", data[2].get("products"), expected_products(task_id, text))
        ck.equal("terminal event: state", data[3]["status"]["state"], "completed")
        ck.check("all events: senderId = Partner AIC", all(d.get("senderId") == PARTNER_AIC for d in data), data)
        ck.equal("complete RPC result: state", (second.get("completeResult") or {}).get("status", {}).get("state"), "completed")
        ck.equal("client lastEventSeq", second["lastEventSeq"], 6)

        replay = must(
            ck,
            "re-stream after close from lastEventSeq=4",
            await leader.stream_collect(stream_url, rpc_url, "restream", task_id, sid, last_event_seq=4, identity=L),
        )
        ck.equal("replay: eventSeq", [f.get("result", {}).get("eventSeq") for f in replay["events"]], [5, 6])

    async with rn.scenario("stream: identity binding negatives") as ck:
        sid = rn.uid("sess")
        request = {
            "jsonrpc": "2.0",
            "id": "stream-forged-1",
            "method": "stream",
            "params": {"message": rpc_body(rn.uid("forged"), sid, "start", OTHER_AIC, "x")},
        }
        raw = must(ck, "raw forged stream POST", await leader.raw_post(stream_url, request, identity=L))
        ck.equal("forged: HTTP status", raw["status"], 403)
        ck.equal(
            "forged: detail code/message",
            {k: (raw["body"].get("detail") or {}).get(k) for k in ("code", "message")},
            {"code": -32009, "message": "AuthorizationFailedError"},
        )

        request["params"]["message"] = rpc_body(rn.uid("nocert"), sid, "start", LEADER_AIC, "x")
        raw = must(
            ck,
            "raw stream POST without client cert",
            await leader.raw_post(f"{urls['permissiveBaseUrl']}/stream", request, identity=None),
        )
        ck.equal("no client cert: HTTP status", raw["status"], 401)
        ck.equal("no client cert: detail code", (raw["body"].get("detail") or {}).get("code"), -32008)

        request["params"]["message"] = rpc_body(rn.uid("unknown"), sid, "re-stream", LEADER_AIC)
        raw = must(ck, "raw re-stream of unknown task", await leader.raw_post(stream_url, request, identity=L))
        ck.equal("unknown re-stream: HTTP status", raw["status"], 404)
        ck.equal("unknown re-stream: detail code", (raw["body"].get("detail") or {}).get("code"), -32001)

        error = fails_with(
            ck,
            "SDK stream client with senderId ≠ its cert AIC",
            await leader.stream_collect(
                stream_url, rpc_url, "start", rn.uid("forged"), sid, text="x", identity=L, leader_id=OTHER_AIC
            ),
        )
        ck.equal("SDK forged: HTTP status", error.get("status"), 403)

        error = fails_with(
            ck,
            "SDK stream client with expectedPartnerAic ≠ server cert AIC",
            await leader.stream_collect(
                stream_url, rpc_url, "start", rn.uid("exp"), sid, text="x", identity=L, expected=OTHER_AIC
            ),
        )
        ck.check("expected mismatch: SenderIdentityMismatchError", error.get("name") == "SenderIdentityMismatchError", error)


# ---------------------------------------------------------------------------
# 场景：Notification
# ---------------------------------------------------------------------------


async def _wait_callbacks(leader: Any, task_id: str, count: int, timeout: float = 8.0) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        received = [r for r in await leader.received_callbacks() if r.get("taskId") == task_id]
        if len(received) >= count or loop.time() > deadline:
            return received
        await asyncio.sleep(0.1)


def _task_result_body(task_id: str, session_id: str, sender_id: str, state: str) -> dict[str, Any]:
    return {
        "type": "task-result",
        "id": f"result-{uuid.uuid4()}",
        "sentAt": _now(),
        "senderRole": "partner",
        "senderId": sender_id,
        "sessionId": session_id,
        "taskId": task_id,
        "status": {"state": state, "stateChangedAt": _now()},
    }


async def flow_notification(rn: Run) -> None:
    leader, partner, pki = rn.leader, rn.partner, rn.pki
    urls = await partner.serve_partner()
    base = urls["baseUrl"]
    L = pki.leader
    token = uuid.uuid4().hex
    receiver_base = await leader.receiver_start(token)

    async with rn.scenario("notification: set/get/start → mTLS callbacks (notifyOnStates) → delete") as ck:
        sid = rn.uid("sess")
        task_id = rn.uid("notif")
        callback_url = f"{receiver_base}/callbacks/{task_id}"
        cfg = must(
            ck,
            "notification/set",
            await leader.notif(base, "set", task_id, identity=L, callback_url=callback_url, token=token),
        )
        ck.check("set: server-generated id", str(cfg.get("id", "")).startswith("notification-"), cfg)
        ck.equal(
            "set: echoed fields",
            {k: cfg.get(k) for k in ("url", "token", "taskId")},
            {"url": callback_url, "token": token, "taskId": task_id},
        )
        ck.equal("get (all)", must(ck, "notification/get", await leader.notif(base, "get", task_id, identity=L)), [cfg])
        ck.equal(
            "get (by id)",
            must(ck, "notification/get by id", await leader.notif(base, "get", task_id, identity=L, config_id=cfg["id"])),
            [cfg],
        )
        other_view = must(
            ck,
            "notification/get as another identity",
            await leader.notif(base, "get", task_id, identity=pki.other, leader_id=OTHER_AIC),
        )
        ck.equal("owner scoping: other identity sees nothing", other_view, [])

        started = must(
            ck,
            "notification/start",
            await leader.notif(
                base,
                "start",
                task_id,
                identity=L,
                config_id=cfg["id"],
                session_id=sid,
                notify_on_states=["working", "completed"],
            ),
        )
        ck.equal("start: result", started, True)

        for state in ("working", "awaiting-input", "completed"):
            must(ck, f"partner dispatch {state}", await partner.partner_notify(task_id, sid, state))
        received = await _wait_callbacks(leader, task_id, 2)
        await asyncio.sleep(0.5)
        received = [r for r in await leader.received_callbacks() if r.get("taskId") == task_id]
        ck.equal("callbacks received (filtered by notifyOnStates)", [r["status"]["state"] for r in received], ["working", "completed"])
        ck.check("callbacks: senderId = Partner AIC", all(r.get("senderId") == PARTNER_AIC for r in received), received)
        ck.check("callbacks: sessionId", all(r.get("sessionId") == sid for r in received), received)

        deleted = must(ck, "notification/delete", await leader.notif(base, "delete", task_id, identity=L, config_id=cfg["id"]))
        ck.equal("delete: result", deleted, True)
        ck.equal("get after delete", must(ck, "notification/get after delete", await leader.notif(base, "get", task_id, identity=L)), [])
        error = fails_with(ck, "delete again", await leader.notif(base, "delete", task_id, identity=L, config_id=cfg["id"]))
        ck.equal("delete again: HTTP status", error.get("status"), 404)

    async with rn.scenario("notification: identity binding negatives") as ck:
        sid = rn.uid("sess")
        task_id = rn.uid("notif-neg")
        error = fails_with(
            ck,
            "notification/start with senderId ≠ cert AIC",
            await leader.notif(
                base, "start", task_id, identity=L, leader_id=OTHER_AIC, config_id="cfg-x", session_id=sid
            ),
        )
        ck.equal("forged start: HTTP status", error.get("status"), 403)

        raw = must(
            ck,
            "raw notification/set without client cert",
            await leader.raw_post(
                f"{urls['permissiveBaseUrl']}/notification/set",
                {
                    "jsonrpc": "2.0",
                    "id": "n-1",
                    "method": "notification/set",
                    "params": {"url": "https://127.0.0.1:1/cb", "token": "t", "taskId": task_id},
                },
                identity=None,
            ),
        )
        ck.equal("no client cert: HTTP status", raw["status"], 401)
        ck.equal("no client cert: detail code", (raw["body"].get("detail") or {}).get("code"), -32008)

        callback_url = f"{receiver_base}/callbacks/{task_id}"
        headers = {"X-ACPs-AIP-Notification-Token": token}
        raw = must(
            ck,
            "callback with forged senderId (Partner cert)",
            await partner.raw_post(
                callback_url, _task_result_body(task_id, sid, OTHER_AIC, "completed"), identity=pki.partner, headers=headers
            ),
        )
        ck.equal("forged callback: HTTP status", raw["status"], 403)
        ck.equal("forged callback: detail code", (raw["body"].get("detail") or {}).get("code"), -32009)
        raw = must(
            ck,
            "callback with wrong token",
            await partner.raw_post(
                callback_url,
                _task_result_body(task_id, sid, PARTNER_AIC, "completed"),
                identity=pki.partner,
                headers={"X-ACPs-AIP-Notification-Token": "wrong"},
            ),
        )
        ck.equal("wrong token: HTTP status", raw["status"], 401)
        raw = must(
            ck,
            "callback with matching senderId and token (control)",
            await partner.raw_post(
                callback_url, _task_result_body(task_id, sid, PARTNER_AIC, "completed"), identity=pki.partner, headers=headers
            ),
        )
        ck.equal("control callback: HTTP status/body", (raw["status"], raw["body"]), (200, {"ok": True}))


# ---------------------------------------------------------------------------
# 场景：群组模式（RabbitMQ）
# ---------------------------------------------------------------------------


# Python SDK 的已知问题（与 Node 无关，Py Leader → Py Partner 同样复现，见 README）
KNOWN_PY_PARTNER_IN_CALLBACK_LEAVE = (
    "python GroupPartnerMqClient: LEAVE_GROUP/DISBAND received on the group queue runs the leave cleanup "
    "inside the aio-pika consumer task; connection.close() cancels that task, so _reset_group_state() and "
    "the disconnect handler never run (state stays 'leaving')"
)

GROUP_PHASES = [
    "group: inbox invite → join (AMQP user_id bound)",
    "group: TaskCommand ⇄ TaskResult (input/continue/complete, cancel, mentions)",
    "group: mgmt GET_STATUS / MUTE / UNMUTE (sessionId echo)",
    "group: forged senderId / user_id rejected",
    "group: leader-requested leave (LEAVE_GROUP → partner)",
    "group: partner-initiated leave (rejoin first)",
    "group: dissolve / DISBAND (rejoin first)",
]


async def _attacker_publish(broker: dict[str, Any], exchange: str, messages: list[tuple[dict[str, Any], str | None]]) -> None:
    """以 OTHER_AIC 身份（broker 认证用户）向群组 exchange 投递消息。"""
    url = build_plain_connection_url(
        host=broker["host"],
        port=broker["port"],
        vhost=broker["vhost"],
        username=OTHER_AIC,
        password=broker["password"],
        connection_name="interop-attacker",
    )
    connection = await aio_pika.connect(url)
    try:
        channel = await connection.channel()
        target = await channel.get_exchange(exchange, ensure=False)
        for body, user_id in messages:
            await target.publish(
                aio_pika.Message(
                    body=json.dumps(body, ensure_ascii=False).encode(),
                    content_type="application/json",
                    user_id=user_id,
                ),
                routing_key="",
            )
    finally:
        await connection.close()


async def _broker_rejects_spoofed_user_id(broker: dict[str, Any], exchange: str) -> tuple[bool, str]:
    url = build_plain_connection_url(
        host=broker["host"],
        port=broker["port"],
        vhost=broker["vhost"],
        username=OTHER_AIC,
        password=broker["password"],
        connection_name="interop-attacker-spoof",
    )
    connection = await aio_pika.connect(url)
    try:
        channel = await connection.channel()
        target = await channel.get_exchange(exchange, ensure=False)
        try:
            await target.publish(aio_pika.Message(body=b"{}", user_id=LEADER_AIC), routing_key="")
        except Exception as exc:  # noqa: BLE001
            # 只认 broker 以 PRECONDITION_FAILED（406）关闭通道且原因是 user_id 不匹配
            detail = f"{type(exc).__name__}: {exc}"
            rejected = "406" in detail or "PRECONDITION_FAILED" in detail.upper()
            return rejected and "user_id" in detail.lower(), detail
        return False, "publish with user_id ≠ authenticated user succeeded"
    finally:
        with contextlib.suppress(Exception):
            await connection.close()


def _is_result(task_id: str, state: str) -> Callable[[dict[str, Any]], bool]:
    return lambda d: d.get("type") == "task-result" and d.get("taskId") == task_id and d["status"]["state"] == state


def _is_status(session_id: str | None = None, connected: bool | None = None) -> Callable[[dict[str, Any]], bool]:
    def predicate(d: dict[str, Any]) -> bool:
        return (
            d.get("type") == "group-mgmt-result"
            and d.get("senderId") == PARTNER_AIC
            and (session_id is None or d.get("sessionId") == session_id)
            and (connected is None or d["status"]["connected"] is connected)
        )

    return predicate


async def _invite_and_join(ck: Checker, leader: Any, partner: Any, group_id: str, label: str) -> None:
    pm, lm = partner.mark(), leader.mark()
    invitation = must(ck, f"{label}: inviteViaInbox", await leader.group_leader("inviteViaInbox", partnerAic=PARTNER_AIC))
    received = await partner.wait_partner_event("invitation", since=pm, timeout=15)
    ck.equal(
        f"{label}: invitation as received by partner",
        {k: received.get(k) for k in ("type", "senderId", "groupId", "invitationToken", "amqp")},
        {k: invitation.get(k) for k in ("type", "senderId", "groupId", "invitationToken", "amqp")},
    )
    joined = await partner.wait_partner_event("joined", since=pm, timeout=15)
    ck.equal(
        f"{label}: partner joined",
        {k: joined.get(k) for k in ("ok", "groupId", "queueName", "state")},
        {"ok": True, "groupId": group_id, "queueName": f"group_{LEADER_AIC}_{group_id}_{PARTNER_AIC}", "state": "joined"},
    )
    status = await leader.wait_leader_message(_is_status(connected=True), since=lm, timeout=10)
    ck.equal(f"{label}: join notification groupId", status.get("groupId"), group_id)
    ck.equal(f"{label}: join notification muted", status["status"].get("muted"), False)


async def flow_group(rn: Run) -> None:
    leader, partner, broker = rn.leader, rn.partner, rn.broker
    if broker is None:
        for name in GROUP_PHASES:
            rn.runner.skip(name, rn.direction, "RabbitMQ not configured (set ACPS_INTEROP_RABBITMQ)")
        return

    group_id = rn.uid("grp")
    state: dict[str, Any] = {}
    phase_status: list[str] = []

    async def run_phase(index: int, body: Callable[[Checker], Awaitable[None]], requires: tuple[int, ...]) -> bool:
        name = GROUP_PHASES[index]
        blocked = [GROUP_PHASES[i] for i in requires if phase_status[i] != "PASS"]
        if blocked:
            rn.runner.skip(name, rn.direction, f"blocked by: {blocked[0]}")
            phase_status.append("SKIP")
            return False
        async with rn.scenario(name) as ck:
            await body(ck)
        phase_status.append(rn.runner.results[-1].status)
        return phase_status[-1] == "PASS"

    async def join(ck: Checker) -> None:
        opened = await leader.group_leader_open(group_id)
        ck.equal("leader: group created", opened.get("groupId"), group_id)
        state["exchange"] = opened["exchange"]
        ck.equal("leader: exchange name", opened["exchange"], f"group_{LEADER_AIC}_{group_id}")
        await partner.group_partner_open()
        await _invite_and_join(ck, leader, partner, group_id, "invite")

    async def tasks(ck: Checker) -> None:
        sid = rn.uid("gsess")
        state["sid"] = sid
        t1 = rn.uid("gtask")
        lm = leader.mark()
        text1 = f"群组任务 {NEED_INPUT_MARK} ✓"
        returned = must(
            ck, "startTask", await leader.group_leader("startTask", sessionId=sid, text=text1, taskId=t1, mentions=[PARTNER_AIC])
        )
        ck.equal("startTask returns taskId", returned, t1)
        accepted = await leader.wait_leader_message(_is_result(t1, "accepted"), since=lm)
        await leader.wait_leader_message(_is_result(t1, "working"), since=lm)
        awaiting_input = await leader.wait_leader_message(_is_result(t1, "awaiting-input"), since=lm)
        ck.equal("awaiting-input prompt", awaiting_input["status"].get("dataItems"), [{"type": "text", "text": INPUT_PROMPT}])
        ck.equal(
            "TaskResult envelope (senderId/senderRole/groupId/sessionId)",
            {k: accepted.get(k) for k in ("senderId", "senderRole", "groupId", "sessionId")},
            {"senderId": PARTNER_AIC, "senderRole": "partner", "groupId": group_id, "sessionId": sid},
        )
        own = [m for m in leader.leader_messages() if m.get("type") == "task-command" and m.get("taskId") == t1]
        ck.check(
            "leader also receives its own TaskCommand (user_id = own AIC passes binding)",
            any(m.get("senderId") == LEADER_AIC and m.get("command") == "start" for m in own),
            own,
        )

        lm = leader.mark()
        text2 = "补充 ✓"
        must(ck, "continueTask", await leader.group_leader("continueTask", taskId=t1, sessionId=sid, text=text2, mentions=[PARTNER_AIC]))
        awaiting = await leader.wait_leader_message(_is_result(t1, "awaiting-completion"), since=lm)
        ck.equal("awaiting-completion products", awaiting.get("products"), expected_products(t1, text2))
        lm = leader.mark()
        must(ck, "completeTask", await leader.group_leader("completeTask", taskId=t1, sessionId=sid, mentions=[PARTNER_AIC]))
        await leader.wait_leader_message(_is_result(t1, "completed"), since=lm)

        t2 = rn.uid("gtask-cancel")
        lm = leader.mark()
        must(ck, "startTask (cancel case)", await leader.group_leader("startTask", sessionId=sid, text="直接处理 ✓", taskId=t2, mentions=[PARTNER_AIC]))
        awaiting = await leader.wait_leader_message(_is_result(t2, "awaiting-completion"), since=lm)
        ck.equal("cancel case: products", awaiting.get("products"), expected_products(t2, "直接处理 ✓"))
        must(ck, "cancelTask", await leader.group_leader("cancelTask", taskId=t2, sessionId=sid, mentions=[PARTNER_AIC]))
        await leader.wait_leader_message(_is_result(t2, "canceled"), since=lm)

        t3 = rn.uid("gtask-other")
        state["t3"] = t3
        must(ck, "startTask mentioning only another AIC", await leader.group_leader("startTask", sessionId=sid, text="不是给你的", taskId=t3, mentions=[OTHER_AIC]))

        report = must(ck, "partner report", await partner.group_partner("report"))
        seen = [(c["taskId"], c["command"], c["isMentioned"], c["senderId"], c["groupId"]) for c in report["commands"] if c["taskId"] in (t1, t2)]
        ck.equal(
            "partner received commands",
            seen,
            [
                (t1, "start", True, LEADER_AIC, group_id),
                (t1, "continue", True, LEADER_AIC, group_id),
                (t1, "complete", True, LEADER_AIC, group_id),
                (t2, "start", True, LEADER_AIC, group_id),
                (t2, "cancel", True, LEADER_AIC, group_id),
            ],
        )
        ck.equal(
            "partner received command text (non-ASCII)",
            [c["text"] for c in report["commands"] if c["taskId"] == t1],
            [text1, text2, ""],
        )

    async def mgmt(ck: Checker) -> None:
        for method, command, muted in (
            ("getPartnerStatus", "get-status", False),
            ("mutePartner", "mute", True),
            ("unmutePartner", "unmute", False),
        ):
            session = rn.uid(method)
            pm, lm = partner.mark(), leader.mark()
            sent = must(ck, method, await leader.group_leader(method, partnerAic=PARTNER_AIC, sessionId=session))
            ck.equal(f"{method}: mentions", sent.get("mentions"), [PARTNER_AIC])
            reply = await leader.wait_leader_message(_is_status(session_id=session), since=lm)
            ck.equal(f"{method}: reply status", reply.get("status"), {"connected": True, "muted": muted})
            # Partner 的自定义 mgmt 回调在内置应答之后才执行，单独等它
            seen = await partner.wait_partner_event("mgmt", lambda d, s=session: d.get("sessionId") == s, since=pm)
            ck.equal(f"{method}: partner mgmt handler saw", (seen.get("command"), seen.get("senderId")), (command, LEADER_AIC))
        report = must(ck, "partner report", await partner.group_partner("report"))
        ck.equal("partner not muted at the end", report.get("isMuted"), False)
        t3 = state["t3"]
        ck.equal(
            "un-mentioned command delivered with isMentioned=false",
            [(c["command"], c["isMentioned"]) for c in report["commands"] if c["taskId"] == t3],
            [("start", False)],
        )
        ck.check(
            "no TaskResult for the un-mentioned task",
            not [m for m in leader.leader_messages() if m.get("type") == "task-result" and m.get("taskId") == t3],
            "",
        )

    async def forged(ck: Checker) -> None:
        sid = state.get("sid") or rn.uid("gsess")
        forged_cmd = TaskCommand(
            id=f"forged-cmd-{uuid.uuid4()}",
            sentAt=_now(),
            senderRole="leader",
            senderId=LEADER_AIC,
            command="start",
            taskId=rn.uid("forged-cmd"),
            groupId=group_id,
            sessionId=sid,
            mentions=[PARTNER_AIC],
            dataItems=[TextDataItem(text="forged")],
        ).model_dump(exclude_none=True)
        forged_result = TaskResult(
            id=f"forged-res-{uuid.uuid4()}",
            sentAt=_now(),
            senderRole="partner",
            senderId=PARTNER_AIC,
            taskId=rn.uid("forged-res"),
            groupId=group_id,
            sessionId=sid,
            status=TaskStatus(state="completed", stateChangedAt=_now()),
        ).model_dump(exclude_none=True)
        no_user_id = TaskCommand(
            id=f"forged-nouid-{uuid.uuid4()}",
            sentAt=_now(),
            senderRole="leader",
            senderId=OTHER_AIC,
            command="start",
            taskId=rn.uid("forged-nouid"),
            groupId=group_id,
            sessionId=sid,
            mentions=[PARTNER_AIC],
        ).model_dump(exclude_none=True)
        await _attacker_publish(
            broker,
            state["exchange"],
            [(forged_cmd, OTHER_AIC), (forged_result, OTHER_AIC), (no_user_id, None)],
        )
        rejected, detail = await _broker_rejects_spoofed_user_id(broker, state["exchange"])
        ck.check("broker rejects user_id ≠ authenticated user", rejected, detail)

        session = rn.uid("after-forge")
        lm = leader.mark()
        must(ck, "status round-trip after forged messages", await leader.group_leader("getPartnerStatus", partnerAic=PARTNER_AIC, sessionId=session))
        await leader.wait_leader_message(_is_status(session_id=session), since=lm)
        await asyncio.sleep(0.5)

        forged_ids = {forged_cmd["id"], forged_result["id"], no_user_id["id"]}
        forged_tasks = {forged_cmd["taskId"], forged_result["taskId"], no_user_id["taskId"]}
        report = must(ck, "partner report", await partner.group_partner("report"))
        ck.check(
            "partner dropped forged commands (senderId ≠ user_id, missing user_id)",
            not [c for c in report["commands"] if c["taskId"] in forged_tasks],
            report["commands"][-3:],
        )
        leaked = [m for m in leader.leader_messages() if m.get("id") in forged_ids or m.get("taskId") in forged_tasks]
        ck.check("leader dropped forged messages", not leaked, leaked)

    async def wait_local_disconnect(ck: Checker, since: int, label: str, *, via_group_queue: bool) -> None:
        """Partner 本地清理（disconnect 回调 + DISCONNECTED 状态）；放在线上检查之后，不阻断它们。"""
        known = KNOWN_PY_PARTNER_IN_CALLBACK_LEAVE if via_group_queue and partner.lang == "python" else None
        name = f"{label}: partner disconnect handler fired (groupId)"
        try:
            disconnected = await partner.wait_partner_event("disconnected", since=since, timeout=10)
            ck.equal(name, disconnected.get("groupId"), group_id, fatal=False, xfail=known)
        except TimeoutError as exc:
            ck.check(name, False, str(exc), fatal=False, xfail=known)
        report = must(ck, "partner report", await partner.group_partner("report"))
        ck.equal(f"{label}: partner local state", report.get("state"), "disconnected", fatal=False, xfail=known)

    async def fresh_partner(ck: Checker, label: str) -> None:
        # 每次退出后用新的 Partner 客户端（退出会关闭连接，收件箱消费者随之失效，两边 SDK 相同）
        await partner.group_partner("close", timeout=20)
        await partner.group_partner_open()
        await _invite_and_join(ck, leader, partner, group_id, label)

    async def leader_requested_leave(ck: Checker) -> None:
        session = rn.uid("leave")
        pm, lm = partner.mark(), leader.mark()
        must(ck, "requestPartnerLeave", await leader.group_leader("requestPartnerLeave", partnerAic=PARTNER_AIC, sessionId=session))
        mgmt_event = await partner.wait_partner_event("mgmt", lambda d: d.get("command") == "leave-group", since=pm)
        ck.equal("partner got LEAVE_GROUP with sessionId", (mgmt_event.get("senderId"), mgmt_event.get("sessionId")), (LEADER_AIC, session))
        status = await leader.wait_leader_message(_is_status(session_id=session, connected=False), since=lm)
        ck.equal("leader sees connected=false with echoed sessionId", status["status"]["connected"], False)
        await wait_local_disconnect(ck, pm, "after LEAVE_GROUP", via_group_queue=True)

    async def partner_initiated_leave(ck: Checker) -> None:
        await fresh_partner(ck, "rejoin")
        session = rn.uid("self-leave")
        pm, lm = partner.mark(), leader.mark()
        must(ck, "partner leaveGroup", await partner.group_partner("leave", sessionId=session))
        command = await leader.wait_leader_message(
            lambda d: d.get("type") == "group-mgmt-command" and d.get("senderId") == PARTNER_AIC and d.get("sessionId") == session,
            since=lm,
        )
        ck.equal(
            "leader receives partner's LEAVE_GROUP",
            {k: command.get(k) for k in ("command", "senderRole", "mentions", "groupId")},
            {"command": "leave-group", "senderRole": "partner", "mentions": [PARTNER_AIC], "groupId": group_id},
        )
        status = await leader.wait_leader_message(_is_status(session_id=session, connected=False), since=lm)
        ck.equal("leader sees connected=false", status["status"]["connected"], False)
        await wait_local_disconnect(ck, pm, "after leaveGroup", via_group_queue=False)

    async def dissolve(ck: Checker) -> None:
        await fresh_partner(ck, "rejoin")
        pm = partner.mark()
        dissolved = must(ck, "dissolveGroup", await leader.group_leader("dissolveGroup", timeout=40))
        ck.equal("leader state after dissolve", dissolved.get("state"), "DISCONNECTED")
        disband = await partner.wait_partner_event("mgmt", lambda d: d.get("command") == "disband", since=pm, timeout=15)
        ck.equal("partner got DISBAND", (disband.get("senderId"), disband.get("mentions")), (LEADER_AIC, "all"))
        await wait_local_disconnect(ck, pm, "after DISBAND", via_group_queue=True)

    # 退出类场景只依赖「加入」成功；各自先换一个新加入的 Partner
    phases = (
        (join, ()),
        (tasks, (0,)),
        (mgmt, (0, 1)),
        (forged, (0, 1)),
        (leader_requested_leave, (0,)),
        (partner_initiated_leave, (0,)),
        (dissolve, (0,)),
    )
    try:
        for index, (body, requires) in enumerate(phases):
            await run_phase(index, body, requires)
    finally:
        with contextlib.suppress(Exception):
            await partner.group_partner("close", timeout=20)
        with contextlib.suppress(Exception):
            await leader.group_leader("close", timeout=20)


FLOWS: dict[str, Callable[[Run], Awaitable[None]]] = {
    "rpc": flow_rpc,
    "stream": flow_stream,
    "notification": flow_notification,
    "group": flow_group,
}


# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------


def broker_config() -> tuple[dict[str, Any] | None, str]:
    address = os.environ.get("ACPS_INTEROP_RABBITMQ", "").strip()
    if not address:
        return None, "ACPS_INTEROP_RABBITMQ not set"
    host, _, port = address.rpartition(":")
    broker = {
        "host": host or "127.0.0.1",
        "port": int(port or 5672),
        "vhost": os.environ.get("ACPS_INTEROP_RABBITMQ_VHOST", "/"),
        "password": os.environ.get("ACPS_INTEROP_RABBITMQ_PASSWORD", "acps-interop"),
    }
    try:
        with socket.create_connection((broker["host"], broker["port"]), timeout=3):
            pass
    except OSError as exc:
        return None, f"RabbitMQ {address} unreachable: {exc}"
    container = os.environ.get("ACPS_INTEROP_RABBITMQ_CONTAINER", "").strip()
    if container:
        provision_users(container, broker)
    return broker, ""


def provision_users(container: str, broker: dict[str, Any]) -> None:
    """在 RabbitMQ 容器里建好以 AIC 命名的用户（PLAIN 认证下 user_id 必须等于认证用户名）。"""

    def ctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", container, "rabbitmqctl", *args], capture_output=True, text=True, check=False
        )

    existing = ctl("list_users", "--silent").stdout
    for aic in (LEADER_AIC, PARTNER_AIC, OTHER_AIC):
        if aic in existing.split():
            ctl("change_password", aic, broker["password"])
        else:
            ctl("add_user", aic, broker["password"])
        result = ctl("set_permissions", "-p", broker["vhost"], aic, ".*", ".*", ".*")
        if result.returncode != 0:
            raise RuntimeError(f"rabbitmqctl set_permissions failed: {result.stderr.strip()}")
    print(f"[interop] RabbitMQ users provisioned in container {container}", flush=True)


def check_dist(root: Path) -> None:
    dist = root / "dist" / "index.js"
    if not dist.exists():
        raise SystemExit("dist/ not found: run `npm run build` first (npm run interop does this)")
    newest_src = max(p.stat().st_mtime for p in (root / "src").rglob("*.ts"))
    if dist.stat().st_mtime < newest_src:
        print("[interop] WARNING: dist/ is older than src/ — run `npm run build`", flush=True)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def print_matrix(results: list[ScenarioResult], directions: list[str]) -> None:
    scenarios: list[str] = []
    for result in results:
        if result.scenario not in scenarios:
            scenarios.append(result.scenario)
    cell = {(r.scenario, r.direction): r for r in results}
    width = max(len(s) for s in scenarios) + 2
    col = max(24, *(len(d) + 2 for d in directions))
    print("\n" + "=" * (width + col * len(directions)))
    print("Scenario".ljust(width) + "".join(d.ljust(col) for d in directions))
    print("-" * (width + col * len(directions)))
    for scenario in scenarios:
        row = scenario.ljust(width)
        for direction in directions:
            result = cell.get((scenario, direction))
            text = "-" if result is None else f"{result.status} ({len(result.checks)})" if result.status != "SKIP" else "SKIP"
            if result is not None and result.xfails:
                text = f"{result.status} ({len(result.checks)}+{len(result.xfails)}x)"
            row += text.ljust(col)
        print(row)
    print("=" * (width + col * len(directions)))
    problems = [r for r in results if r.status != "PASS"]
    for result in problems:
        print(f"{result.status}: {result.scenario} [{result.direction}]\n    {result.note[:1500]}")
    total = sum(len(r.checks) for r in results)
    print(
        f"\n{sum(r.status == 'PASS' for r in results)} passed, {sum(r.status == 'FAIL' for r in results)} failed, "
        f"{sum(r.status == 'XFAIL' for r in results)} xfail (known issues), "
        f"{sum(r.status == 'SKIP' for r in results)} skipped; {total} individual checks"
    )


async def amain(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2]
    check_dist(root)
    selected = [s.strip() for s in args.only.split(",")] if args.only else list(FLOWS)
    unknown = [s for s in selected if s not in FLOWS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; choose from {list(FLOWS)}")
    pairs = [("python", "node"), ("node", "python")]
    if args.baseline:
        pairs += [("python", "python"), ("node", "node")]

    broker, broker_note = (None, "group scenario not selected")
    if "group" in selected:
        broker, broker_note = broker_config()
        if broker is None:
            print(f"[interop] group scenarios will be skipped: {broker_note}", flush=True)

    if not args.debug:
        # 伪造消息被拒绝时 aio-pika 会把回调里的身份异常交给事件循环打印整段 traceback，这里压成一行
        def compact_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            exc = context.get("exception")
            detail = f"{type(exc).__name__}: {exc}" if exc else ""
            print(f"      [py asyncio] {context.get('message')} {detail}"[:300], file=sys.stderr, flush=True)

        asyncio.get_running_loop().set_exception_handler(compact_exception_handler)

    runner = Runner(verbose=args.verbose)
    directions: list[str] = []
    with tempfile.TemporaryDirectory(prefix="acps-interop-") as tmp:
        pki = create_pki(Path(tmp))
        print(f"[interop] test PKI generated in {tmp} (deleted on exit)", flush=True)
        for leader_lang, partner_lang in pairs:
            sides: dict[str, Any] = {}
            for lang in {leader_lang, partner_lang}:
                sides[lang] = (PySide if lang == "python" else NodeSide)(pki, broker, debug=args.debug)
            run = Run(runner, pki, broker, sides[leader_lang], sides[partner_lang])
            directions.append(run.direction)
            print(f"\n=== {run.direction} ===", flush=True)
            try:
                for side in sides.values():
                    await side.start()
                for name in selected:
                    await FLOWS[name](run)
            finally:
                for side in sides.values():
                    with contextlib.suppress(Exception):
                        await side.stop()
    print_matrix(runner.results, directions)
    return 1 if any(r.status == "FAIL" for r in runner.results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ACPs SDK Python ↔ Node interop harness")
    parser.add_argument("--only", help="comma-separated scenarios: rpc,stream,notification,group")
    parser.add_argument("--baseline", action="store_true", help="also run Python↔Python and Node↔Node")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every check")
    parser.add_argument("--debug", action="store_true", help="show SDK logs (Node stderr, uvicorn warnings)")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
