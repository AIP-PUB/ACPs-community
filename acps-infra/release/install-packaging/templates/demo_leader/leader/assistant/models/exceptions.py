"""
Leader Agent Platform - 业务异常定义

本模块定义所有业务异常，错误码采用 6 位整数格式：
- 前 3 位：HTTP 状态码
- 后 3 位：业务错误码
"""

from typing import Any


class LeaderError(Exception):
 """
    Leader 平台基础异常。

    所有业务异常都应继承此类。
 """

    def __init__(
        self,
        code: int,
        message: str,
        details: Any | None = None,
    ):
        self.code = code
        self.message = message
        self.details = details
        super.__init__(message)

    @property
    def http_status_code(self) -> int:
 """从错误码提取 HTTP 状态码（前 3 位）。"""
        return int(str(self.code)[:3])

    def to_dict(self) -> dict:
 """转换为 API 响应格式的字典。"""
        result = {
            "code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            result["data"] = self.details
        return result


# =============================================================================
# 400 系列：客户端错误
# =============================================================================


class BadRequestError(LeaderError):
 """请求体 JSON 不合法/缺少必填字段/字段类型明显不对。"""

    def __init__(
        self,
        message: str = "Invalid request body",
        details: Any | None = None,
    ):
        super.__init__(400001, message, details)


class ValidationError(LeaderError):
 """业务参数校验失败。"""

    def __init__(
        self,
        message: str = "Validation failed",
        details: Any | None = None,
    ):
        super.__init__(400002, message, details)


# =============================================================================
# 404 系列：资源不存在
# =============================================================================


class SessionNotFoundError(LeaderError):
 """指定的 Session 不存在或已被 TTL 清理。"""

    def __init__(
        self,
        session_id: str,
        message: str | None = None,
    ):
        msg = message or f"Session '{session_id}' not found or expired"
        super.__init__(404001, msg, {"session_id": session_id})


# =============================================================================
# 409 系列：冲突错误
# =============================================================================


class ModeMismatchError(LeaderError):
 """Session 已存在但 mode 与请求不一致。"""

    def __init__(
        self,
        session_id: str,
        expected_mode: str,
        actual_mode: str,
    ):
        message = f"Session mode mismatch: expected '{expected_mode}', got '{actual_mode}'"
        super.__init__(
            409001,
            message,
            {
                "session_id": session_id,
                "expected_mode": expected_mode,
                "actual_mode": actual_mode,
            },
        )


class ActiveTaskMismatchError(LeaderError):
 """传入的 activeTaskId 与当前不一致。"""

    def __init__(
        self,
        session_id: str,
        expected_task_id: str | None,
        actual_task_id: str | None,
    ):
        message = "Active task ID mismatch"
        super.__init__(
            409002,
            message,
            {
                "session_id": session_id,
                "expected_task_id": expected_task_id,
                "actual_task_id": actual_task_id,
            },
        )


class DuplicateRequestError(LeaderError):
 """同一 sessionId 下复用相同 clientRequestId，但请求载荷与首次不一致。"""

    def __init__(
        self,
        session_id: str,
        client_request_id: str,
    ):
        message = "Duplicate request with different payload"
        super.__init__(
            409003,
            message,
            {
                "session_id": session_id,
                "client_request_id": client_request_id,
            },
        )


# =============================================================================
# 413 系列：请求过大
# =============================================================================


class PayloadTooLargeError(LeaderError):
 """query 或请求体过大超出限制。"""

    def __init__(
        self,
        max_size: int,
        actual_size: int,
        field: str = "query",
    ):
        message = f"Request {field} too large: {actual_size} > {max_size}"
        super.__init__(
            413001,
            message,
            {
                "field": field,
                "max_size": max_size,
                "actual_size": actual_size,
            },
        )


# =============================================================================
# 429 系列：限流
# =============================================================================


class RateLimitError(LeaderError):
 """被限流。"""

    def __init__(
        self,
        retry_after: int | None = None,
        message: str = "Rate limit exceeded",
    ):
        details = {"retry_after": retry_after} if retry_after else None
        super.__init__(429001, message, details)


# =============================================================================
# 500 系列：服务端错误
# =============================================================================


class InternalError(LeaderError):
 """未预期错误。"""

    def __init__(
        self,
        message: str = "Internal server error",
        details: Any | None = None,
    ):
        super.__init__(500000, message, details)


# =============================================================================
# 503 系列：服务不可用
# =============================================================================


class ServiceUnavailableError(LeaderError):
 """依赖不可用且可恢复。"""

    def __init__(
        self,
        service: str,
        message: str | None = None,
        details: Any | None = None,
    ):
        msg = message or f"Service '{service}' temporarily unavailable"
        super.__init__(503001, msg, details or {"service": service})


# =============================================================================
# Partner 相关错误
# =============================================================================


class PartnerError(LeaderError):
 """Partner 通信或处理错误的基类。"""

    def __init__(
        self,
        code: int,
        partner_aic: str,
        message: str,
        details: Any | None = None,
    ):
        self.partner_aic = partner_aic
        super.__init__(code, message, details)


class PartnerUnavailableError(PartnerError):
 """Partner 不可达。"""

    def __init__(
        self,
        partner_aic: str,
        reason: str | None = None,
    ):
        message = f"Partner '{partner_aic}' unavailable"
        if reason:
            message += f": {reason}"
        super.__init__(503002, partner_aic, message, {"reason": reason})


class PartnerTimeoutError(PartnerError):
 """Partner 响应超时。"""

    def __init__(
        self,
        partner_aic: str,
        timeout_ms: int,
    ):
        message = f"Partner '{partner_aic}' timed out after {timeout_ms}ms"
        super.__init__(
            504001,
            partner_aic,
            message,
            {"timeout_ms": timeout_ms},
        )


class PartnerProtocolError(PartnerError):
 """Partner 协议错误（响应格式不符合 AIP）。"""

    def __init__(
        self,
        partner_aic: str,
        reason: str,
    ):
        message = f"Partner '{partner_aic}' protocol error: {reason}"
        super.__init__(502001, partner_aic, message, {"reason": reason})


# =============================================================================
# LLM 相关错误
# =============================================================================


class LLMError(LeaderError):
 """LLM 调用错误的基类。"""

    def __init__(
        self,
        code: int,
        llm_call_point: str,
        message: str,
        details: Any | None = None,
    ):
        self.llm_call_point = llm_call_point
        super.__init__(code, message, details)


class LLMTimeoutError(LLMError):
 """LLM 调用超时。"""

    def __init__(
        self,
        llm_call_point: str,
        timeout_seconds: int,
    ):
        message = f"LLM call '{llm_call_point}' timed out after {timeout_seconds}s"
        super.__init__(
            504002,
            llm_call_point,
            message,
            {"timeout_seconds": timeout_seconds},
        )


class LLMResponseError(LLMError):
 """LLM 响应解析失败（不符合预期的 JSON 结构）。"""

    def __init__(
        self,
        llm_call_point: str,
        reason: str,
        raw_response: str | None = None,
    ):
        message = f"LLM call '{llm_call_point}' response error: {reason}"
        details = {"reason": reason}
        if raw_response:
 # 截断以避免日志过长
            details["raw_response"] = raw_response[:500]
        super.__init__(502002, llm_call_point, message, details)


# =============================================================================
# 为了向后兼容，保留别名
# =============================================================================

# 兼容现有代码中使用的 TourAssistantError
TourAssistantError = LeaderError

# 别名：用于 API 层
LeaderAgentError = LeaderError


# =============================================================================
# 补充异常类型
# =============================================================================


class SessionExpiredError(LeaderError):
 """Session 已过期。"""

    def __init__(
        self,
        session_id: str,
        message: str | None = None,
    ):
        msg = message or f"Session '{session_id}' has expired"
        super.__init__(404002, msg, {"session_id": session_id})


class LLMCallError(LeaderError):
 """LLM 调用失败（通用）。"""

    def __init__(
        self,
        message: str = "LLM call failed",
        details: Any | None = None,
    ):
        super.__init__(500010, message, details)


class LLMParseError(LeaderError):
 """LLM 响应解析失败。"""

    def __init__(
        self,
        message: str = "Failed to parse LLM response",
        details: Any | None = None,
    ):
        super.__init__(500011, message, details)


class SessionClosedError(LeaderError):
 """Session 已关闭，不再接受新的 /submit 请求。"""

    def __init__(
        self,
        session_id: str,
        closed_reason: str | None = None,
        message: str | None = None,
    ):
        msg = message or f"Session '{session_id}' is closed and cannot accept new requests"
        details = {"session_id": session_id}
        if closed_reason:
            details["closed_reason"] = closed_reason
        super.__init__(403001, msg, details)
