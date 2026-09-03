"""
AIP v2 流式传输客户端（Leader 侧）

提供：
- AipStreamClient：
  - start_stream / re_stream         — 向 Partner /stream 端点建立 SSE 连接，yield StreamResponse
  - stream_with_reconnect            — 带断线重连的高级接口
  - complete / cancel                — 通过 RPC 端点发送命令
  - last_event_seq (property)        — 最后收到的事件序号（用于断线续传）
  - close                            — 释放内部 HTTP 客户端
- StreamProtocolError：Partner 推送 error 帧时抛出（不触发重连）
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

from .aip_base_model import TaskCommand, TaskCommandType, TaskResult, TextDataItem
from .aip_identity import (
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_peer_aic_from_httpx_response,
)
from .aip_rpc_client import AipRpcClient
from .aip_stream_model import StreamRequest, StreamRequestParams, StreamResponse

logger = logging.getLogger(__name__)


class StreamProtocolError(Exception):
    """Partner 推送了带 error 字段的 StreamResponse 帧。

    此错误代表服务端明确告知不可续传（如 buffer 已过期），
    不应触发 re-stream 重连，应直接向上传播。
    """

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"StreamProtocolError {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AipStreamClient:
    """Leader 侧流式传输客户端。

    与 Partner 的 /stream 端点建立 SSE 连接，解析事件并 yield。
    complete / cancel 操作走 RPC 端点（与 AipRpcClient 共享逻辑）。

    transport 参数仅用于测试注入；生产代码传 None 使用真实 HTTP。
    """

    def __init__(
        self,
        partner_stream_url: str,
        partner_rpc_url: str,
        leader_id: str,
        *,
        ssl_context: Optional[ssl.SSLContext] = None,
        transport: Optional[httpx.AsyncTransport] = None,
        expected_partner_aic: str | None = None,
        identity_binding_enabled: bool = True,
    ) -> None:
        self.partner_stream_url = partner_stream_url
        self.partner_rpc_url = partner_rpc_url
        self.leader_id = leader_id
        self._expected_partner_aic = expected_partner_aic
        self._identity_binding_enabled = identity_binding_enabled

        if self._identity_binding_enabled and not self._expected_partner_aic:
            raise ValueError(
                "expected_partner_aic is required when identity_binding_enabled=True"
            )
        if not self._identity_binding_enabled:
            logger.warning(
                "AIP identity binding disabled for Stream client partner_stream_url=%s",
                self.partner_stream_url,
            )

        kwargs: dict = {}
        if transport is not None:
            kwargs["transport"] = transport
        elif ssl_context is not None:
            kwargs["verify"] = ssl_context

        # SSE 长连接客户端（设较长 timeout）
        self._stream_client = httpx.AsyncClient(timeout=None, **kwargs)
        # RPC 短连接客户端（复用 transport）
        self._rpc_client = AipRpcClient(
            partner_url=partner_rpc_url,
            leader_id=leader_id,
            ssl_context=ssl_context,
            transport=transport,
            expected_partner_aic=expected_partner_aic,
            identity_binding_enabled=identity_binding_enabled,
        )

        # 最后成功接收的 SSE 事件序号（断线续传用）
        self._last_event_seq: Optional[int] = None

    @property
    def last_event_seq(self) -> Optional[int]:
        """最后成功接收的 SSE 事件序号。可传入 re_stream 用于断线续传。"""
        return self._last_event_seq

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _make_command(
        self,
        command_type: str,
        session_id: str,
        task_id: Optional[str] = None,
        text_content: Optional[str] = None,
        command_params: Optional[dict] = None,
    ) -> TaskCommand:
        """构建 TaskCommand。task_id=None 时自动生成 UUID。"""
        if task_id is None:
            task_id = f"task-{uuid.uuid4()}"

        data_items = []
        if text_content:
            data_items.append(TextDataItem(text=text_content))

        return TaskCommand(
            id=f"cmd-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_id,
            sessionId=session_id,
            command=TaskCommandType(command_type),
            taskId=task_id,
            dataItems=data_items if data_items else None,
            commandParams=command_params,
        )

    def _parse_sse_line(self, line: str) -> Optional[StreamResponse]:
        """解析单条 SSE 行，非 'data: ' 前缀的行返回 None（保留供单行快速路径使用）。"""
        if not line.startswith("data: "):
            return None
        json_str = line[len("data: "):]
        try:
            return StreamResponse.model_validate_json(json_str)
        except Exception:
            return None

    def _emit_accumulated(
        self, data_parts: list[str]
    ) -> Optional[StreamResponse]:
        """将积累的多个 data: 值拼接后解析为 StreamResponse。

        按 SSE 规范：多个 data: 字段的值以 LF（\n）拼接。
        解析失败时返回 None（容错，不抛出）。
        """
        if not data_parts:
            return None
        json_str = "\n".join(data_parts)
        try:
            return StreamResponse.model_validate_json(json_str)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 流式操作
    # ------------------------------------------------------------------

    def open_stream(self, command: "TaskCommand") -> AsyncIterator[StreamResponse]:
        """通用流入口：向 Partner 发送任意 TaskCommand 并返回 SSE 事件迭代器。

        与 start_stream / re_stream 不同，此方法接受完整构造好的 TaskCommand，
        适用于需要精确控制命令参数的场景。
        """
        return self._stream(command)

    def start_stream(
        self,
        session_id: str,
        task_id: Optional[str] = None,
        text_content: Optional[str] = None,
    ) -> AsyncIterator[StreamResponse]:
        """向 Partner 发起 stream/start，返回 async 迭代器（SSE 事件流）。"""
        command = self._make_command(
            command_type="start",
            session_id=session_id,
            task_id=task_id,
            text_content=text_content,
        )
        return self._stream(command, last_event_seq=None)

    def re_stream(
        self,
        task_id: str,
        session_id: str,
        last_event_seq: Optional[int] = None,
    ) -> AsyncIterator[StreamResponse]:
        """向 Partner 发起 re-stream 重连，从 last_event_seq 之后续传。"""
        command = self._make_command(
            command_type="re-stream",
            session_id=session_id,
            task_id=task_id,
            command_params={"lastEventSeq": last_event_seq} if last_event_seq is not None else None,
        )
        return self._stream(command, last_event_seq=last_event_seq)

    async def _stream(
        self, command: TaskCommand, last_event_seq: Optional[int] = None
    ) -> AsyncIterator[StreamResponse]:
        """内部：建立 SSE 连接并解析事件流，自动更新 _last_event_seq。

        SSE 规范支持：
        - 单行 data: <json>
        - 多行 data:，每行值以 LF 拼接，空行（event boundary）触发解析
        - comment 行（以 : 开头）、event:/id:/retry: 行均被忽略
        """
        request_id = str(uuid.uuid4())
        stream_req = StreamRequest(
            id=request_id,
            params=StreamRequestParams(message=command),
        )
        body = stream_req.model_dump_json(exclude_none=True)
        remote_aic: str | None = None

        if self._identity_binding_enabled:
            assert_sender_matches_expected(command, self.leader_id)

        async with self._stream_client.stream(
            "POST",
            self.partner_stream_url,
            content=body.encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        ) as response:
            if self._identity_binding_enabled:
                remote_aic = extract_peer_aic_from_httpx_response(response)
                remote_aic = assert_aic_matches_expected(
                    remote_aic,
                    self._expected_partner_aic,
                    actual_label="TLS server AIC",
                    expected_label="expected_partner_aic",
                )
            response.raise_for_status()
            data_parts: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    # data: 后紧跟一个可选空格（规范要求去掉第一个空格）
                    value = line[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    data_parts.append(value)
                elif line == "":
                    # 空行 = 事件边界，发出积累的数据
                    if data_parts:
                        parsed = self._emit_accumulated(data_parts)
                        data_parts = []
                        if parsed is not None:
                            if self._identity_binding_enabled and parsed.result is not None:
                                assert_sender_matches_peer(parsed.result.eventData, remote_aic)
                            if parsed.result is not None and parsed.result.eventSeq is not None:
                                self._last_event_seq = parsed.result.eventSeq
                            yield parsed
                # 其他行（comment、event:、id:、retry:）忽略

            # 流结束后若有未发出的数据（无末尾空行的情况），也尝试发出
            if data_parts:
                parsed = self._emit_accumulated(data_parts)
                if parsed is not None:
                    if self._identity_binding_enabled and parsed.result is not None:
                        assert_sender_matches_peer(parsed.result.eventData, remote_aic)
                    if parsed.result is not None and parsed.result.eventSeq is not None:
                        self._last_event_seq = parsed.result.eventSeq
                    yield parsed

    async def stream_with_reconnect(
        self,
        session_id: str,
        user_input: str,
        task_id: Optional[str] = None,
        *,
        max_reconnects: int = 3,
        backoff_s: float = 1.0,
    ) -> AsyncIterator[StreamResponse]:
        """带断线重连的高级流式接口。

        首次调用 start_stream；若连接中断（网络/协议错误），
        自动用 re_stream(last_event_seq) 从中断处续传。

        若 Partner 返回 error 帧（StreamProtocolError），直接抛出，不重连。

        Args:
            session_id:      会话 ID
            user_input:      用户输入文本
            task_id:         任务 ID（None 则自动生成）
            max_reconnects:  最多重连次数（默认 3）
            backoff_s:       每次重连的退避步长（秒，实际等待 backoff_s * 重连次数）
        """
        if task_id is None:
            task_id = f"task-{uuid.uuid4()}"

        reconnect_count = 0
        gen = self.start_stream(session_id=session_id, task_id=task_id, text_content=user_input)

        while True:
            try:
                async for event in gen:
                    if event.error is not None:
                        raise StreamProtocolError(
                            code=event.error.code,
                            message=event.error.message,
                            data=event.error.data if hasattr(event.error, "data") else None,
                        )
                    yield event
                break  # 流正常结束，退出重连循环
            except StreamProtocolError:
                raise  # 服务端明确 error，不重连
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
                if reconnect_count >= max_reconnects:
                    raise
                reconnect_count += 1
                await asyncio.sleep(backoff_s * reconnect_count)
                gen = self.re_stream(
                    task_id=task_id,
                    session_id=session_id,
                    last_event_seq=self._last_event_seq,
                )

    # ------------------------------------------------------------------
    # RPC 操作（complete / cancel）
    # ------------------------------------------------------------------

    async def complete(self, task_id: str, session_id: str) -> TaskResult:
        """发送 complete 命令到 RPC 端点。"""
        return await self._rpc_client.complete_task(task_id=task_id, session_id=session_id)

    async def cancel(self, task_id: str, session_id: str) -> TaskResult:
        """发送 cancel 命令到 RPC 端点。"""
        return await self._rpc_client.cancel_task(task_id=task_id, session_id=session_id)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """释放内部 HTTP 客户端资源。"""
        await self._stream_client.aclose()
        await self._rpc_client.close()
