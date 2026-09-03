from __future__ import annotations

import json
import logging
import ssl
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Optional

import httpx

from acps_sdk.aip.access_status import error_info_from_rpc_response, status_from_rpc_response
from acps_sdk.amp.models import AccessBody, AccessParticipant, AccessRequest, AccessResponse

from .aip_base_model import TaskCommand, TaskCommandType, TaskResult, TextDataItem
from .aip_identity import (
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_peer_aic_from_httpx_response,
)
from .aip_rpc_model import RpcRequest, RpcRequestParams, RpcResponse

if TYPE_CHECKING:
    from acps_sdk.amp.access_emitter import AccessEmitter
    from acps_sdk.amp.trace_context import TraceContext

logger = logging.getLogger(__name__)


class AipRpcClient:
    """
    A client for interacting with an AIP-compliant Partner over RPC.
    Supports both HTTP and HTTPS with mTLS.
    opt-in AccessEmitter 注入时，在 _send_request 前后发射 caller client span。
    """

    def __init__(
        self,
        partner_url: str,
        leader_id: str,
        ssl_context: Optional[ssl.SSLContext] = None,
        transport: Optional[httpx.AsyncTransport] = None,
        *,
        access_emitter: AccessEmitter | None = None,
        callee_aic: str = "",
        caller_service: str = "",
        callee_service: str = "",
        trace_context_provider: Callable[[], TraceContext | None] | None = None,
        expected_partner_aic: str | None = None,
        identity_binding_enabled: bool = True,
    ):
        """
        初始化AIP RPC客户端

        Args:
            partner_url: Partner的RPC端点URL
            leader_id: Leader Agent的ID
            ssl_context: 可选的SSL上下文，用于mTLS连接
            access_emitter: 可选 Access 发射器（opt-in caller 埋点）
            callee_aic: 被调方 AIC（AccessBody.callee）
            caller_service: 主调服务名
            callee_service: 被调服务名
            trace_context_provider: 返回当前 trace 上下文；无则每次 RPC 新生成 trace_id
        """
        self.partner_url = partner_url
        self.leader_id = leader_id
        self._access_emitter = access_emitter
        self._callee_aic = callee_aic
        self._caller_service = caller_service
        self._callee_service = callee_service
        self._trace_context_provider = trace_context_provider
        self._expected_partner_aic = expected_partner_aic
        self._identity_binding_enabled = identity_binding_enabled

        if self._identity_binding_enabled and not self._expected_partner_aic:
            raise ValueError(
                "expected_partner_aic is required when identity_binding_enabled=True"
            )
        if not self._identity_binding_enabled:
            logger.warning(
                "AIP identity binding disabled for RPC client partner_url=%s",
                self.partner_url,
            )

        kwargs: dict = {}
        if transport is not None:
            kwargs["transport"] = transport
        elif ssl_context is not None:
            kwargs["verify"] = ssl_context

        self.http_client = httpx.AsyncClient(**kwargs)

    async def _send_request(self, command: TaskCommand) -> RpcResponse:
        """
        Sends a task command to the Partner and returns the response.
        """
        from acps_sdk.amp.trace_context import (
            TRACEPARENT_HEADER,
            TraceContext,
            format_traceparent,
            new_span_id,
            new_trace_id,
        )

        request_id = str(uuid.uuid4())
        rpc_request = RpcRequest(
            id=request_id,
            params=RpcRequestParams(command=command),
        )
        request_payload = rpc_request.model_dump(exclude_none=True)
        request_bytes = len(json.dumps(request_payload).encode("utf-8"))
        method = str(command.command.value if hasattr(command.command, "value") else command.command)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        trace_id: str | None = None
        span_id: str | None = None
        parent_span_id = ""

        if self._access_emitter is not None:
            parent_ctx = self._trace_context_provider() if self._trace_context_provider else None
            trace_id = parent_ctx.trace_id if parent_ctx else new_trace_id()
            span_id = new_span_id()
            headers[TRACEPARENT_HEADER] = format_traceparent(
                TraceContext(trace_id=trace_id, span_id=span_id)
            )

        t0 = monotonic()
        response: httpx.Response | None = None
        rpc_response: RpcResponse | None = None
        status_code = 500
        response_bytes = 0
        error_info = None
        remote_aic: str | None = None

        try:
            if self._identity_binding_enabled:
                assert_sender_matches_expected(command, self.leader_id)
            response = await self.http_client.post(
                self.partner_url,
                json=request_payload,
                headers=headers,
                timeout=30.0,
            )
            if self._identity_binding_enabled:
                remote_aic = extract_peer_aic_from_httpx_response(response)
                remote_aic = assert_aic_matches_expected(
                    remote_aic,
                    self._expected_partner_aic,
                    actual_label="TLS server AIC",
                    expected_label="expected_partner_aic",
                )
            response.raise_for_status()
            response_bytes = len(response.content)
            rpc_response = RpcResponse.model_validate(response.json())
            status_code = status_from_rpc_response(rpc_response)
            error_info = error_info_from_rpc_response(rpc_response)

            if rpc_response.error:
                raise Exception(
                    f"RPC Error: {rpc_response.error.code} - {rpc_response.error.message}"
                )

            if rpc_response.id != request_id:
                raise Exception("RPC Error: Response ID does not match request ID.")

            if self._identity_binding_enabled and rpc_response.result is not None:
                assert_sender_matches_peer(rpc_response.result, remote_aic)

            return rpc_response

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            response_bytes = len(e.response.content) if e.response.content else 0
            from acps_sdk.amp.models import ErrorInfo

            error_info = ErrorInfo(code=status_code, message=e.response.text[:500])
            raise Exception(
                f"HTTP Error: {e.response.status_code} - {e.response.text}"
            ) from e
        except Exception:
            if error_info is None:
                from acps_sdk.amp.models import ErrorInfo

                error_info = ErrorInfo(code=status_code, message="rpc failed")
            raise
        finally:
            if self._access_emitter is not None and span_id is not None and trace_id is not None:
                duration_ms = int((monotonic() - t0) * 1000)
                request_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                if "Content-Type" in headers:
                    request_headers["content-type"] = headers["Content-Type"]

                body = AccessBody(
                    request=AccessRequest(
                        method=method,
                        url=self.partner_url,
                        route="/rpc",
                        headers=request_headers,
                        bodySizeBytes=request_bytes,
                    ),
                    response=AccessResponse(statusCode=status_code, bodySizeBytes=response_bytes),
                    caller=AccessParticipant(aic=self.leader_id, serviceName=self._caller_service),
                    callee=AccessParticipant(aic=self._callee_aic, serviceName=self._callee_service),
                    error=error_info,
                    durationMs=float(duration_ms),
                )
                try:
                    await self._access_emitter.emit(
                        body,
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent_span_id,
                        correlation_id=command.sessionId,
                    )
                except Exception:
                    pass

    def _create_task_command(
        self,
        command: TaskCommandType,
        task_id: str,
        session_id: str,
        text_content: str | None = None,
    ) -> TaskCommand:
        """
        Helper to create a new TaskCommand object.
        """
        data_items = []
        if text_content:
            data_items.append(TextDataItem(text=text_content))

        return TaskCommand(
            id=f"cmd-{uuid.uuid4()}",
            sentAt=datetime.now(timezone.utc).isoformat(),
            senderRole="leader",
            senderId=self.leader_id,
            dataItems=data_items if data_items else None,
            sessionId=session_id,
            command=command,
            taskId=task_id,
        )

    async def start_task(
        self, session_id: str, user_input: str, task_id: Optional[str] = None
    ) -> TaskResult:
        """
        Starts a new task with the Partner.
        """
        if not task_id:
            task_id = f"task-{uuid.uuid4()}"
        command = self._create_task_command(
            command=TaskCommandType.Start,
            task_id=task_id,
            session_id=session_id,
            text_content=user_input,
        )

        response = await self._send_request(command)
        if isinstance(response.result, TaskResult):
            return response.result
        raise TypeError(f"Expected TaskResult, got {type(response.result)}")

    async def continue_task(
        self, task_id: str, session_id: str, user_input: str
    ) -> TaskResult:
        """
        Continues a task that is in a waiting state.
        """
        command = self._create_task_command(
            command=TaskCommandType.Continue,
            task_id=task_id,
            session_id=session_id,
            text_content=user_input,
        )
        response = await self._send_request(command)
        if isinstance(response.result, TaskResult):
            return response.result
        raise TypeError(f"Expected TaskResult, got {type(response.result)}")

    async def complete_task(self, task_id: str, session_id: str) -> TaskResult:
        """
        Marks a task as completed.
        """
        command = self._create_task_command(
            command=TaskCommandType.Complete,
            task_id=task_id,
            session_id=session_id,
        )
        response = await self._send_request(command)
        if isinstance(response.result, TaskResult):
            return response.result
        raise TypeError(f"Expected TaskResult, got {type(response.result)}")

    async def cancel_task(self, task_id: str, session_id: str) -> TaskResult:
        """
        Cancels a task that is in a non-terminal state.
        """
        command = self._create_task_command(
            command=TaskCommandType.Cancel,
            task_id=task_id,
            session_id=session_id,
        )
        response = await self._send_request(command)
        if isinstance(response.result, TaskResult):
            return response.result
        raise TypeError(f"Expected TaskResult, got {type(response.result)}")

    async def get_task(self, task_id: str, session_id: str) -> TaskResult:
        """
        Retrieves the current state of a task.
        """
        command = self._create_task_command(
            command=TaskCommandType.Get,
            task_id=task_id,
            session_id=session_id,
        )
        response = await self._send_request(command)
        if isinstance(response.result, TaskResult):
            return response.result
        raise TypeError(f"Expected TaskResult, got {type(response.result)}")

    async def close(self):
        """
        Closes the underlying HTTP client.
        """
        await self.http_client.aclose()
