"""AIP RPC 响应到 Access statusCode / ErrorInfo 的共享映射（caller/callee 同口径）。"""

from __future__ import annotations

from acps_sdk.aip.aip_base_model import TaskState
from acps_sdk.aip.aip_rpc_model import JSONRPCError, RpcResponse
from acps_sdk.amp.models import ErrorInfo

_RPC_ERROR_STATUS: dict[int, int] = {
    -32001: 404,
    -32602: 400,
    -32700: 400,
    -32603: 500,
}


def status_from_rpc_error(error: JSONRPCError | None) -> int:
    """JSON-RPC error → HTTP 风格 statusCode。"""
    if error is None:
        return 200
    return _RPC_ERROR_STATUS.get(error.code, 500)


def error_info_from_rpc_response(resp: RpcResponse) -> ErrorInfo | None:
    """从 RpcResponse 提取 ErrorInfo（含 Rejected 语义）。"""
    if resp.error is not None:
        return ErrorInfo(code=status_from_rpc_error(resp.error), message=resp.error.message)
    state = getattr(getattr(resp.result, "status", None), "state", None)
    if state == TaskState.Failed:
        return ErrorInfo(code=500, message="task failed")
    if state == TaskState.Rejected:
        return ErrorInfo(code="REJECTED", message="task rejected")
    return None


def status_from_rpc_response(resp: RpcResponse) -> int:
    """RpcResponse → statusCode（Rejected 映射为 200，靠 error.code 计错）。"""
    if resp.error is not None:
        return status_from_rpc_error(resp.error)
    state = getattr(getattr(resp.result, "status", None), "state", None)
    if state == TaskState.Failed:
        return 500
    if state == TaskState.Rejected:
        return 200
    return 200
