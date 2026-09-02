"""
Leader Agent Platform - 基础类型与通用约定

本模块定义所有其他模型依赖的基础类型别名和枚举常量。
"""

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field

# =============================================================================
# 类型别名 (Type Aliases)
# =============================================================================

# ISO 8601 时间戳字符串
# 跨服务/跨语言传递时避免时区歧义；与 AIP/ACS 的时间字段保持一致。
# 约束：必须包含时区信息（如 `+08:00` 或 `Z`）。
IsoDateTimeString = Annotated[str, Field(description="ISO 8601 时间戳字符串，必须包含时区信息")]

# AIC（智能体身份识别码）
# Leader/Partner 之间所有身份绑定都应以 AIC 为主键。
# 在 mTLS 场景下：证书中携带的 AIC 必须与消息中的 senderId/partnerAic 一致。
AgentAic = Annotated[str, Field(description="智能体身份识别码 (AIC)")]

# Session 的对外标识
# Session 只存在于 Leader，外部只使用 sessionId 关联请求。
SessionId = Annotated[str, Field(description="Session 对外标识")]

# 用户视角的活跃任务标识（Leader 内部使用）
# 用户只理解"我这次要完成的事"，不关心底层有几个 Partner task。
ActiveTaskId = Annotated[str, Field(description="用户视角的活跃任务标识")]

# AIP 视角任务标识（Leader 与 Partner 通信使用）
# 每个 Partner 在同一 activeTask 下必须有独立 taskId，避免多方并发覆盖状态。
# 推荐派生：`aipTaskId = activeTaskId + ":" + partnerAic`
AipTaskId = Annotated[str, Field(description="AIP 视角任务标识")]

# 业务维度 ID
# 维度拆解与一致性校验需要稳定的维度主键。
# Demo 中可直接使用配置文件定义的 key（如 hotel/food/transport）。
DimensionId = Annotated[str, Field(description="业务维度 ID")]

# 客户端请求去重 ID
ClientRequestId = Annotated[str, Field(description="客户端请求去重 ID")]


# =============================================================================
# 枚举类型 (Enums)
# =============================================================================


class ExecutionMode(str, Enum):
 """
    Leader 执行模式。

    约束：mode 在 Session 创建时确定且不可变更，
    避免同一会话混用语义导致实现复杂度爆炸。
 """

    DIRECT_RPC = "direct_rpc"
    GROUP = "group"


class IntentType(str, Enum):
 """
    Leader 的意图分类（用于决策分支，不等价于 AIP TaskCommand）。

    说明：把「用户输入」映射为「平台动作」：增量补齐 / 新任务 / 闲聊。
 """

    TASK_INPUT = "TASK_INPUT"
    TASK_NEW = "TASK_NEW"
    CHIT_CHAT = "CHIT_CHAT"


class UserResultType(str, Enum):
 """
    用户结果类型。

    Demo 最小集：clarification / final。
    可选扩展：progress / intent_notice / error / pending。
 """

    PENDING = "pending"
    CLARIFICATION = "clarification"
    FINAL = "final"
    PROGRESS = "progress"
    INTENT_NOTICE = "intent_notice"
    ERROR = "error"


class ResponseType(str, Enum):
 """
    对话历史中的响应类型。
 """

    PENDING = "pending"
    CLARIFICATION = "clarification"
    FINAL = "final"
    CHAT = "chat"


class ActiveTaskStatus(str, Enum):
 """
    activeTask 对外的收敛状态（面向用户/前端）。

    说明：多 Partner 的 TaskState 无法直接暴露给用户；
    前端需要一个稳定的"显示状态"。
 """

    PENDING = "pending"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EventLogType(str, Enum):
 """
    Session 内事件日志的类型。

    让调试工具按类型聚合，而不是靠字符串全文检索。
 """

    USER_SUBMIT = "user_submit"
    INTENT_DECISION = "intent_decision"
    PLANNING_RESULT = "planning_result"
    PARTNER_SELECTED = "partner_selected"
    AIP_REQUEST = "aip_request"
    AIP_RESPONSE = "aip_response"
    AIP_ERROR = "aip_error"
    USER_RESULT_UPDATE = "user_result_update"
    SESSION_TOUCHED = "session_touched"
    SESSION_CLOSED = "session_closed"


class SessionClosedReason(str, Enum):
 """
    Session 关闭原因。

    Demo 最小枚举。
 """

    TTL_EXPIRED = "ttl_expired"
    USER_CANCEL = "user_cancel"
    SYSTEM_SHUTDOWN = "system_shutdown"
    REPLACED_BY_NEW_TASK = "replaced_by_new_task"


# =============================================================================
# 常量 (Constants)
# =============================================================================

# Session 默认 TTL（秒）
DEFAULT_SESSION_TTL_SECONDS = 3600

# Session TTL（分钟）
SESSION_TTL_MINUTES = 60

# EventLog 最大条目数（FIFO）
MAX_EVENT_LOG_ENTRIES = 500

# DialogContext 保留的最近轮次数
DEFAULT_RECENT_TURNS_KEEP = 5

# 触发 LLM-7 历史压缩的阈值
HISTORY_COMPRESSION_THRESHOLD = 8

# LLM 调用超时时间（秒）
LLM_CALL_TIMEOUT_SECONDS = 60

# LLM-6 (Aggregation) 单独超时时间（秒）- 输入数据量大，需要更长处理时间
LLM6_CALL_TIMEOUT_SECONDS = 180

# LLM 调用最大重试次数
LLM_MAX_RETRIES = 3


# =============================================================================
# 工具函数 (Utility Functions)
# =============================================================================


def now_iso -> str:
 """返回当前时间的 ISO 8601 格式字符串（带时区）。"""
    return datetime.now.astimezone.isoformat


def generate_aip_task_id(active_task_id: str, partner_aic: str) -> str:
 """
    生成 AIP 任务 ID。

    推荐格式：`activeTaskId:partnerAic`，可读且幂等。
 """
    return f"{active_task_id}:{partner_aic}"


def generate_session_id -> str:
 """生成唯一的 Session ID。"""
    import uuid

    return f"sess_{uuid.uuid4.hex[:16]}"


def generate_active_task_id -> str:
 """生成唯一的 Active Task ID。"""
    import uuid

    return f"task_{uuid.uuid4.hex[:16]}"


class EventType(str, Enum):
 """
    事件日志类型（简化版）。

    用于 Session 内部事件追踪。
 """

    USER_INPUT = "user_input"
    INTENT_ANALYZED = "intent_analyzed"
    SCENARIO_SWITCH = "scenario_switch"
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_COMPLETED = "task_completed"
    PLANNING_COMPLETED = "planning_completed"
    TASK_DISPATCHED = "task_dispatched"  # 任务分发到 Partner
    PARTNER_CALLED = "partner_called"
    PARTNER_RESPONSE = "partner_response"
    ERROR = "error"


class SessionStatus(str, Enum):
 """
    Session 状态。
 """

    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
