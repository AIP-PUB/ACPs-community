"""Node 端：启动 agent.mts 子进程，用 stdin/stdout 上的 JSON 行驱动 Node SDK。

NodeSide 与 interop_py.PySide 提供同一组方法，场景代码对两种语言一视同仁。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from interop_common import LEADER_AIC, PARTNER_AIC, Pki, TlsFiles

AGENT = Path(__file__).resolve().parent / "agent.mts"


class NodeAgent:
    """一个 Node agent 子进程。"""

    def __init__(self, label: str, *, debug: bool) -> None:
        self.label = label
        self.debug = debug
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self._event_signal = asyncio.Event()
        self._readers: list[asyncio.Task[None]] = []
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        env = dict(os.environ)
        if self.debug:
            env["ACPS_INTEROP_DEBUG"] = "1"
        self._proc = await asyncio.create_subprocess_exec(
            "node",
            str(AGENT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,
        )
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        await self.wait_event(lambda e: e["event"] == "agent.ready", timeout=20)

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.stderr_tail.append(f"[non-json stdout] {line!r}")
                continue
            if "id" in message and message["id"] in self._pending:
                future = self._pending.pop(message["id"])
                if not future.done():
                    future.set_result(message)
            elif "event" in message:
                self.events.append(message)
                self._event_signal.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(f"node agent {self.label} exited"))

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            self.stderr_tail = (self.stderr_tail + [text])[-200:]
            if self.debug:
                print(f"      [{self.label} stderr] {text}", file=sys.stderr, flush=True)

    async def call(self, op: str, timeout: float = 30.0, **params: Any) -> dict[str, Any]:
        """发送请求并返回 `{"ok": bool, "result"|"error": ...}`。"""
        assert self._proc and self._proc.stdin
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = json.dumps({"id": request_id, "op": op, "params": params}, ensure_ascii=False)
        self._proc.stdin.write(line.encode("utf-8") + b"\n")
        await self._proc.stdin.drain()
        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return {"ok": False, "error": {"name": "HarnessTimeout", "message": f"{op} timed out"}}
        if message.get("ok"):
            return {"ok": True, "result": message.get("result")}
        return {"ok": False, "error": message.get("error")}

    async def must(self, op: str, timeout: float = 30.0, **params: Any) -> Any:
        outcome = await self.call(op, timeout=timeout, **params)
        if not outcome["ok"]:
            raise RuntimeError(f"node {op} failed: {outcome['error']}")
        return outcome["result"]

    async def wait_event(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10.0, *, since: int = 0
    ) -> dict[str, Any]:
        """等待第 since 条之后满足条件的事件。"""
        deadline = asyncio.get_running_loop().time() + timeout
        seen = since
        while True:
            for event in self.events[seen:]:
                if predicate(event):
                    return event
            seen = len(self.events)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"node agent {self.label}: event not seen within {timeout}s; "
                    f"stderr tail: {self.stderr_tail[-5:]}"
                )
            self._event_signal.clear()
            try:
                await asyncio.wait_for(self._event_signal.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        for reader in self._readers:
            reader.cancel()
        self._proc = None


def _tls(identity: TlsFiles | None, pki: Pki) -> dict[str, str]:
    return identity.spec() if identity is not None else {"ca": str(pki.ca)}


class NodeSide:
    """Node SDK 一侧（Partner 服务端 + Leader 客户端 + 群组两端）。"""

    lang = "node"

    def __init__(self, pki: Pki, broker: dict[str, Any] | None, *, debug: bool) -> None:
        self.pki = pki
        self.broker = broker
        self.agent = NodeAgent("node", debug=debug)
        self._partner_urls: dict[str, str] | None = None
        self.group_messages: list[dict[str, Any]] = []

    async def start(self) -> None:
        await self.agent.start()

    async def stop(self) -> None:
        await self.agent.stop()

    # ---- Partner -----------------------------------------------------------

    async def serve_partner(self) -> dict[str, str]:
        if self._partner_urls is None:
            self._partner_urls = await self.agent.must(
                "partner.serve",
                aic=PARTNER_AIC,
                tls=self.pki.partner.spec(),
                callbackTls=self.pki.partner.spec(),
            )
        return self._partner_urls

    async def partner_notify(self, task_id: str, session_id: str, state: str) -> dict[str, Any]:
        return await self.agent.call("partner.notify", taskId=task_id, sessionId=session_id, state=state)

    # ---- Leader ------------------------------------------------------------

    def _client(self, identity: TlsFiles | None, leader_id: str, expected: str) -> dict[str, Any]:
        return {"tls": _tls(identity, self.pki), "leaderId": leader_id, "expectedPartnerAic": expected}

    async def rpc(
        self,
        url: str,
        method: str,
        task_id: str,
        session_id: str,
        *,
        text: str | None = None,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
    ) -> dict[str, Any]:
        return await self.agent.call(
            "rpc.call",
            url=url,
            method=method,
            taskId=task_id,
            sessionId=session_id,
            text=text,
            **self._client(identity, leader_id, expected),
        )

    async def stream_collect(
        self,
        stream_url: str,
        rpc_url: str,
        mode: str,
        task_id: str,
        session_id: str,
        *,
        text: str | None = None,
        last_event_seq: int | None = None,
        stop_after: int | None = None,
        complete_on_awaiting: bool = False,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
    ) -> dict[str, Any]:
        return await self.agent.call(
            "stream.collect",
            timeout=30,
            streamUrl=stream_url,
            rpcUrl=rpc_url,
            mode=mode,
            taskId=task_id,
            sessionId=session_id,
            text=text,
            lastEventSeq=last_event_seq,
            stopAfter=stop_after,
            completeOnAwaitingCompletion=complete_on_awaiting,
            **self._client(identity, leader_id, expected),
        )

    async def receiver_start(self, token: str) -> str:
        result = await self.agent.must("notif.receiver.start", tls=self.pki.leader.spec(), token=token)
        return result["baseUrl"]

    async def received_callbacks(self) -> list[dict[str, Any]]:
        return await self.agent.must("notif.received")

    async def notif(
        self,
        partner_url: str,
        method: str,
        task_id: str,
        *,
        identity: TlsFiles | None,
        leader_id: str = LEADER_AIC,
        expected: str = PARTNER_AIC,
        callback_url: str | None = None,
        token: str | None = None,
        config_id: str | None = None,
        session_id: str | None = None,
        notify_on_states: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.agent.call(
            "notif.call",
            partnerUrl=partner_url,
            method=method,
            taskId=task_id,
            callbackUrl=callback_url,
            token=token,
            configId=config_id,
            sessionId=session_id,
            notifyOnStates=notify_on_states,
            **self._client(identity, leader_id, expected),
        )

    async def raw_post(
        self,
        url: str,
        body: Any,
        *,
        identity: TlsFiles | None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.agent.call(
            "raw.post", url=url, body=body, headers=headers or {}, tls=_tls(identity, self.pki)
        )

    # ---- 群组 --------------------------------------------------------------

    def _broker(self) -> dict[str, Any]:
        assert self.broker is not None
        return {
            "host": self.broker["host"],
            "port": self.broker["port"],
            "vhost": self.broker["vhost"],
            "password": self.broker["password"],
        }

    async def group_leader_open(self, group_id: str) -> dict[str, Any]:
        return await self.agent.must("group.leader.open", aic=LEADER_AIC, groupId=group_id, **self._broker())

    async def group_leader(self, method: str, timeout: float = 30.0, **kwargs: Any) -> dict[str, Any]:
        return await self.agent.call("group.leader.call", timeout=timeout, method=method, **kwargs)

    def mark(self) -> int:
        """当前事件位置；wait_* 的 since 参数只看这之后的事件。"""
        return len(self.agent.events)

    def leader_messages(self) -> list[dict[str, Any]]:
        return [e["data"] for e in self.agent.events if e["event"] == "group.leader.message"]

    async def wait_leader_message(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10.0, *, since: int = 0
    ) -> dict[str, Any]:
        event = await self.agent.wait_event(
            lambda e: e["event"] == "group.leader.message" and predicate(e["data"]),
            timeout,
            since=since,
        )
        return event["data"]

    async def group_partner_open(self) -> dict[str, Any]:
        return await self.agent.must("group.partner.open", aic=PARTNER_AIC, **self._broker())

    async def group_partner(self, method: str, timeout: float = 30.0, **kwargs: Any) -> dict[str, Any]:
        return await self.agent.call("group.partner.call", timeout=timeout, method=method, **kwargs)

    async def wait_partner_event(
        self,
        kind: str,
        predicate: Callable[[dict[str, Any]], bool] = lambda _d: True,
        timeout: float = 10.0,
        *,
        since: int = 0,
    ) -> dict[str, Any]:
        event = await self.agent.wait_event(
            lambda e: e["event"] == f"group.partner.{kind}" and predicate(e["data"]),
            timeout,
            since=since,
        )
        return event["data"]
