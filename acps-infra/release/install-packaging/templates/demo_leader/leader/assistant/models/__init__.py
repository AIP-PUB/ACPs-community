"""
Leader Agent Platform - Models

本模块定义 Leader 平台的核心业务模型。

模块结构：
- base: 基础类型别名、枚举常量、工具函数
- exceptions: 业务异常定义
- aip: AIP 协议镜像类型
- partner: Partner 相关模型
- task: 任务相关模型
- session: Session 聚合根
- intent: 意图决策相关模型
"""

# =============================================================================
# 基础类型与枚举
# =============================================================================
# 从 acps_sdk.acs 导出 ACS 标准模型，供其他模块使用
from acps_sdk.acs import (
    AgentCapabilities,
    AgentCapabilitySpec,
    AgentEndPoint,
    AgentProvider,
    AgentSkill,
)

# =============================================================================
# AIP 镜像类型
# =============================================================================
from .aip import (
    AipMessageDraft,
    AipTaskSnapshot,
    DataItem,
    DataItemBase,
    FileDataItem,
    StructuredDataItem,
    TaskCommand,
    TaskState,
    TaskStatusSnapshot,
    TextDataItem,
    create_structured_item,
    create_text_item,
)
from .base import (
    DEFAULT_RECENT_TURNS_KEEP,
 # 常量
    DEFAULT_SESSION_TTL_SECONDS,
    HISTORY_COMPRESSION_THRESHOLD,
    LLM6_CALL_TIMEOUT_SECONDS,
    LLM_CALL_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
    MAX_EVENT_LOG_ENTRIES,
    SESSION_TTL_MINUTES,
    ActiveTaskId,
    ActiveTaskStatus,
    AgentAic,
    AipTaskId,
    ClientRequestId,
    DimensionId,
    EventLogType,
    EventType,
 # 枚举
    ExecutionMode,
    IntentType,
 # 类型别名
    IsoDateTimeString,
    ResponseType,
    SessionClosedReason,
    SessionId,
    SessionStatus,
    UserResultType,
    generate_active_task_id,
    generate_aip_task_id,
    generate_session_id,
 # 工具函数
    now_iso,
)

# =============================================================================
# 反问/澄清相关（LLM-3）
# =============================================================================
from .clarification import (
    ClarificationMergeInput,
    MergedClarification,
    PartnerClarificationItem,
    RequiredField,
    extract_clarification_from_task_status,
)

# =============================================================================
# 异常
# =============================================================================
from .exceptions import (
    ActiveTaskMismatchError,
    BadRequestError,
    DuplicateRequestError,
    InternalError,
    LeaderAgentError,
    LeaderError,
    LLMCallError,
    LLMError,
    LLMParseError,
    LLMResponseError,
    LLMTimeoutError,
    ModeMismatchError,
    PartnerError,
    PartnerProtocolError,
    PartnerTimeoutError,
    PartnerUnavailableError,
    PayloadTooLargeError,
    RateLimitError,
    ServiceUnavailableError,
    SessionClosedError,
    SessionExpiredError,
    SessionNotFoundError,
    TourAssistantError,  # 兼容别名
    ValidationError,
)

# =============================================================================
# 历史压缩相关（LLM-7）
# =============================================================================
from .history_compression import (
    COMPRESSION_THRESHOLD,
    MAX_SUMMARY_LENGTH,
    TURNS_TO_KEEP,
    CompressionTurn,
    HistoryCompressionRequest,
    HistoryCompressionResult,
)

# =============================================================================
# 输入路由相关（LLM-4）
# =============================================================================
from .input_routing import (
    ContinueMessagePlan,
    InputRoutingRequest,
    InputRoutingResult,
    PartnerGapInfo,
    PartnerPatch,
    build_continue_message_plans,
    extract_partner_gaps_from_execution_result,
)

# =============================================================================
# 意图决策相关
# =============================================================================
from .intent import (
    AggregationResult,
    ClarificationResult,
    CompletionDecision,
    CompletionGateResult,
    HistoryCompressionResult,
    IntentDecision,
    MissingFieldSpec,
    PartnerInputPatch,
    TaskInputRoutingResult,
    TaskInstruction,
)

# =============================================================================
# Partner 相关
# =============================================================================
from .partner import (
    PartnerAvailabilityDetails,
    PartnerLastError,
    PartnerRuntimeState,
    PeerIdentityVerification,
    ResolvedPartnerEndpoint,
)

# =============================================================================
# Session 相关
# =============================================================================
from .session import (
    DialogContext,
    DialogTurn,
    EventLogEntry,
    GroupRoutingInfo,
    ScenarioBrief,
    ScenarioRuntime,
    Session,
)

# =============================================================================
# Task 相关
# =============================================================================
from .task import (
    ActiveTask,
    DimensionNote,
    LLMPlanningOutput,
    PartnerSelection,
    PartnerTask,
    PartnerTaskControl,
    PlanningResult,
    UserResult,
)

# =============================================================================
# 任务执行相关（异步执行模式）
# =============================================================================
from .task_execution import (
    TaskExecution,
    TaskExecutionPhase,
    TaskExecutionProgress,
    TaskExecutionStatus,
)

__all__ = [
 # base
    "IsoDateTimeString",
    "AgentAic",
    "SessionId",
    "ActiveTaskId",
    "AipTaskId",
    "DimensionId",
    "ClientRequestId",
    "ExecutionMode",
    "IntentType",
    "UserResultType",
    "ResponseType",
    "ActiveTaskStatus",
    "EventLogType",
    "SessionClosedReason",
    "EventType",
    "SessionStatus",
    "DEFAULT_SESSION_TTL_SECONDS",
    "SESSION_TTL_MINUTES",
    "MAX_EVENT_LOG_ENTRIES",
    "DEFAULT_RECENT_TURNS_KEEP",
    "HISTORY_COMPRESSION_THRESHOLD",
    "LLM_CALL_TIMEOUT_SECONDS",
    "LLM6_CALL_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "now_iso",
    "generate_aip_task_id",
    "generate_session_id",
    "generate_active_task_id",
 # exceptions
    "LeaderError",
    "LeaderAgentError",
    "TourAssistantError",
    "BadRequestError",
    "ValidationError",
    "SessionNotFoundError",
    "SessionExpiredError",
    "SessionClosedError",
    "ModeMismatchError",
    "ActiveTaskMismatchError",
    "DuplicateRequestError",
    "PayloadTooLargeError",
    "RateLimitError",
    "InternalError",
    "ServiceUnavailableError",
    "PartnerError",
    "PartnerUnavailableError",
    "PartnerTimeoutError",
    "PartnerProtocolError",
    "LLMError",
    "LLMTimeoutError",
    "LLMResponseError",
    "LLMCallError",
    "LLMParseError",
 # aip
    "TaskState",
    "TaskCommand",
    "DataItemBase",
    "TextDataItem",
    "FileDataItem",
    "StructuredDataItem",
    "DataItem",
    "TaskStatusSnapshot",
    "AipTaskSnapshot",
    "AipMessageDraft",
    "create_text_item",
    "create_structured_item",
 # partner (Leader 运行时模型)
    "ResolvedPartnerEndpoint",
    "PeerIdentityVerification",
    "PartnerAvailabilityDetails",
    "PartnerLastError",
    "PartnerRuntimeState",
 # ACS 标准模型 (来自 acps_sdk.acs)
    "AgentCapabilitySpec",
    "AgentSkill",
    "AgentEndPoint",
    "AgentCapabilities",
    "AgentProvider",
 # task
    "UserResult",
    "PartnerTaskControl",
    "PartnerTask",
    "PartnerSelection",
    "DimensionNote",
    "PlanningResult",
    "ActiveTask",
 # session
    "ScenarioBrief",
    "ScenarioRuntime",
    "DialogTurn",
    "DialogContext",
    "EventLogEntry",
    "GroupRoutingInfo",
    "Session",
 # intent
    "TaskInstruction",
    "IntentDecision",
    "MissingFieldSpec",
    "ClarificationResult",
    "PartnerInputPatch",
    "TaskInputRoutingResult",
    "CompletionDecision",
    "CompletionGateResult",
    "AggregationResult",
    "HistoryCompressionResult",
 # clarification (LLM-3)
    "RequiredField",
    "PartnerClarificationItem",
    "MergedClarification",
    "ClarificationMergeInput",
    "extract_clarification_from_task_status",
 # input_routing (LLM-4)
    "PartnerGapInfo",
    "InputRoutingRequest",
    "PartnerPatch",
    "InputRoutingResult",
    "ContinueMessagePlan",
    "extract_partner_gaps_from_execution_result",
    "build_continue_message_plans",
 # history_compression (LLM-7)
    "CompressionTurn",
    "HistoryCompressionRequest",
    "HistoryCompressionResult",
    "COMPRESSION_THRESHOLD",
    "TURNS_TO_KEEP",
    "MAX_SUMMARY_LENGTH",
 # task_execution (异步执行模式)
    "TaskExecutionStatus",
    "TaskExecutionPhase",
    "TaskExecutionProgress",
    "TaskExecution",
]
