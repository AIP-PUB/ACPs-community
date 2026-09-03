"""
Leader Agent Platform - Core 模块

本模块包含核心业务逻辑组件：
- session_manager: Session 生命周期管理
- intent_analyzer: LLM-1 意图分析
- planner: LLM-2 全量规划
- clarification_merger: LLM-3 反问合并
- input_router: LLM-4 输入路由
- executor: 任务执行器 (Direct RPC)
- group_manager: 群组管理器 (Group Mode)
- group_executor: 群组任务执行器 (Group Mode)
- completion_gate: LLM-5 完成闸门
- aggregator: LLM-6 结果整合
- history_compressor: LLM-7 历史压缩
- orchestrator: 主编排器
"""

from .aggregator import (
    AggregationInput,
    AggregationResult,
    Aggregator,
    DegradationInfo,
    PartnerOutput,
    get_aggregator,
)
from .clarification_merger import (
    ClarificationMerger,
    get_clarification_merger,
)
from .completion_gate import (
    AwaitingCompletionDecision,
    AwaitingCompletionGateResult,
    CompletionGate,
    PartnerProductSummary,
    get_completion_gate,
)
from .executor import (
    ExecutionPhase,
    ExecutionResult,
    ExecutorConfig,
    PartnerExecutionResult,
    TaskExecutor,
)
from .group_executor import GroupTaskExecutor
from .group_manager import (
    GroupConfig,
    GroupManager,
    RabbitMQConfig,
    create_group_manager,
)
from .history_compressor import (
    HistoryCompressor,
    get_history_compressor,
    reset_history_compressor,
)
from .input_router import (
    InputRouter,
    get_input_router,
    reset_input_router,
)
from .intent_analyzer import IntentAnalyzer, create_intent_analyzer
from .orchestrator import Orchestrator, create_orchestrator
from .planner import Planner, get_planner
from .session_manager import SessionManager, get_session_manager

__all__ = [
    # Session Manager
    "SessionManager",
    "get_session_manager",
    # Intent Analyzer
    "IntentAnalyzer",
    "create_intent_analyzer",
    # Planner
    "Planner",
    "get_planner",
    # Clarification Merger (LLM-3)
    "ClarificationMerger",
    "get_clarification_merger",
    # Input Router (LLM-4)
    "InputRouter",
    "get_input_router",
    "reset_input_router",
    # Executor (Direct RPC)
    "TaskExecutor",
    "ExecutorConfig",
    "ExecutionResult",
    "ExecutionPhase",
    "PartnerExecutionResult",
    # Group Manager & Executor (Group Mode)
    "GroupManager",
    "create_group_manager",
    "GroupConfig",
    "RabbitMQConfig",
    "GroupTaskExecutor",
    # Completion Gate (LLM-5)
    "CompletionGate",
    "AwaitingCompletionDecision",
    "AwaitingCompletionGateResult",
    "PartnerProductSummary",
    "get_completion_gate",
    # Aggregator (LLM-6)
    "Aggregator",
    "AggregationResult",
    "AggregationInput",
    "PartnerOutput",
    "DegradationInfo",
    "get_aggregator",
    # History Compressor (LLM-7)
    "HistoryCompressor",
    "get_history_compressor",
    "reset_history_compressor",
    # Orchestrator
    "Orchestrator",
    "create_orchestrator",
]
