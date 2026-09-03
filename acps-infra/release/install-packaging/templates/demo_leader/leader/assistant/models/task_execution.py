"""
Leader Agent Platform - TaskExecution 模型

本模块定义异步任务执行的状态模型。

设计目标：
- /submit 在 Planning 完成后立即返回（pending 状态）
- 后续执行异步进行，通过 /result 轮询获取结果
- 支持多种执行阶段的状态跟踪
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import (
    ActiveTaskId,
    IsoDateTimeString,
    SessionId,
    now_iso,
)


class TaskExecutionStatus(str, Enum):
 """
    任务执行状态枚举。

    状态流转：
    - PENDING: 已提交，等待执行（Planning 完成后）
    - RUNNING: 正在执行（Executor 开始执行）
    - AWAITING_INPUT: 等待用户输入（Partner 返回 AwaitingInput）
    - COMPLETED: 执行完成（所有 Partner 完成）
    - FAILED: 执行失败
    - CANCELLED: 已取消
 """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskExecutionPhase(str, Enum):
 """
    任务执行阶段（细粒度跟踪）。
 """

 # Planning 阶段
    PLANNING_STARTED = "planning_started"
    PLANNING_COMPLETED = "planning_completed"

 # Execution 阶段
    EXECUTION_STARTED = "execution_started"
    EXECUTION_POLLING = "execution_polling"
    EXECUTION_CLARIFICATION = "execution_clarification"  # 等待用户反问
    EXECUTION_COMPLETION_GATE = "execution_completion_gate"  # LLM-5 决策
    EXECUTION_COMPLETED = "execution_completed"

 # Aggregation 阶段
    AGGREGATION_STARTED = "aggregation_started"
    AGGREGATION_COMPLETED = "aggregation_completed"


class TaskExecutionProgress(BaseModel):
 """
    任务执行进度信息。
 """

    current_phase: TaskExecutionPhase = Field(
        ...,
        alias="currentPhase",
        description="当前执行阶段",
    )
    total_partners: int = Field(
        default=0,
        alias="totalPartners",
        description="总 Partner 数量",
    )
    completed_partners: int = Field(
        default=0,
        alias="completedPartners",
        description="已完成的 Partner 数量",
    )
    failed_partners: int = Field(
        default=0,
        alias="failedPartners",
        description="失败的 Partner 数量",
    )
    awaiting_input_partners: int = Field(
        default=0,
        alias="awaitingInputPartners",
        description="等待用户输入的 Partner 数量",
    )
    progress_percent: float = Field(
        default=0.0,
        alias="progressPercent",
        description="执行进度百分比 (0-100)",
    )

    model_config = ConfigDict(populate_by_name=True)


class TaskExecution(BaseModel):
 """
    任务执行状态模型。

    用于跟踪从 Planning 到最终结果的整个异步执行过程。
 """

 # 标识信息
    task_id: ActiveTaskId = Field(
        ...,
        alias="taskId",
        description="任务 ID",
    )
    session_id: SessionId = Field(
        ...,
        alias="sessionId",
        description="会话 ID",
    )

 # 状态信息
    status: TaskExecutionStatus = Field(
        default=TaskExecutionStatus.PENDING,
        description="执行状态",
    )
    progress: TaskExecutionProgress | None = Field(
        default=None,
        description="执行进度",
    )

 # 时间戳
    created_at: IsoDateTimeString = Field(
        ...,
        alias="createdAt",
        description="创建时间（Planning 完成时）",
    )
    started_at: IsoDateTimeString | None = Field(
        default=None,
        alias="startedAt",
        description="开始执行时间",
    )
    completed_at: IsoDateTimeString | None = Field(
        default=None,
        alias="completedAt",
        description="完成时间",
    )
    updated_at: IsoDateTimeString = Field(
        ...,
        alias="updatedAt",
        description="最后更新时间",
    )

 # 规划结果（来自 LLM-2）
    planning_result: dict[str, Any] | None = Field(
        default=None,
        alias="planningResult",
        description="规划结果的序列化形式",
    )

 # 执行结果（来自 Executor）
    execution_result: dict[str, Any] | None = Field(
        default=None,
        alias="executionResult",
        description="执行结果的序列化形式",
    )

 # 聚合结果（来自 LLM-6，最终用户可见结果）
    aggregation_result: dict[str, Any] | None = Field(
        default=None,
        alias="aggregationResult",
        description="聚合结果的序列化形式",
    )

 # 最终响应文本
    response_text: str | None = Field(
        default=None,
        alias="responseText",
        description="最终响应文本（用户可见）",
    )

 # 反问信息（如果处于 AWAITING_INPUT 状态）
    clarification_text: str | None = Field(
        default=None,
        alias="clarificationText",
        description="反问文本（需要用户回答的问题）",
    )

 # 错误信息
    error_message: str | None = Field(
        default=None,
        alias="errorMessage",
        description="错误信息（如果失败）",
    )
    error_details: dict[str, Any] | None = Field(
        default=None,
        alias="errorDetails",
        description="详细错误信息",
    )

 # 元数据
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="附加元数据",
    )

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def create(
        cls,
        task_id: ActiveTaskId,
        session_id: SessionId,
        planning_result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskExecution:
 """
        创建新的任务执行记录。

        Args:
            task_id: 任务 ID
            session_id: 会话 ID
            planning_result: 规划结果
            metadata: 元数据

        Returns:
            新创建的 TaskExecution 实例
 """
        now = now_iso
        return cls(
            task_id=task_id,
            session_id=session_id,
            status=TaskExecutionStatus.PENDING,
            progress=TaskExecutionProgress(
                current_phase=TaskExecutionPhase.PLANNING_COMPLETED,
            ),
            created_at=now,
            updated_at=now,
            planning_result=planning_result,
            metadata=metadata or {},
        )

    def mark_running(self) -> None:
 """标记为正在执行状态。"""
        self.status = TaskExecutionStatus.RUNNING
        self.started_at = now_iso
        self.updated_at = now_iso
        if self.progress:
            self.progress.current_phase = TaskExecutionPhase.EXECUTION_STARTED

    def mark_awaiting_input(
        self,
        clarification_text: str,
        execution_result: dict[str, Any] | None = None,
    ) -> None:
 """
        标记为等待用户输入状态。

        Args:
            clarification_text: 需要用户回答的问题
            execution_result: 执行结果（包含 partner 状态信息）
 """
        self.status = TaskExecutionStatus.AWAITING_INPUT
        self.clarification_text = clarification_text
        if execution_result:
            self.execution_result = execution_result
        self.updated_at = now_iso
        if self.progress:
            self.progress.current_phase = TaskExecutionPhase.EXECUTION_CLARIFICATION

    def mark_completed(
        self,
        execution_result: dict[str, Any] | None = None,
        aggregation_result: dict[str, Any] | None = None,
        response_text: str | None = None,
    ) -> None:
 """
        标记为完成状态。

        Args:
            execution_result: 执行结果
            aggregation_result: 聚合结果
            response_text: 最终响应文本
 """
        self.status = TaskExecutionStatus.COMPLETED
        self.completed_at = now_iso
        self.updated_at = now_iso
        if execution_result:
            self.execution_result = execution_result
        if aggregation_result:
            self.aggregation_result = aggregation_result
        if response_text:
            self.response_text = response_text
        if self.progress:
            self.progress.current_phase = TaskExecutionPhase.AGGREGATION_COMPLETED
            self.progress.progress_percent = 100.0

    def mark_failed(
        self,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> None:
 """
        标记为失败状态。

        Args:
            error_message: 错误信息
            error_details: 详细错误信息
 """
        self.status = TaskExecutionStatus.FAILED
        self.completed_at = now_iso
        self.updated_at = now_iso
        self.error_message = error_message
        if error_details:
            self.error_details = error_details

    def mark_cancelled(self) -> None:
 """标记为已取消状态。"""
        self.status = TaskExecutionStatus.CANCELLED
        self.completed_at = now_iso
        self.updated_at = now_iso

    def update_progress(
        self,
        phase: TaskExecutionPhase | None = None,
        completed_partners: int | None = None,
        failed_partners: int | None = None,
        awaiting_input_partners: int | None = None,
    ) -> None:
 """
        更新执行进度。

        Args:
            phase: 当前阶段
            completed_partners: 已完成的 Partner 数量
            failed_partners: 失败的 Partner 数量
            awaiting_input_partners: 等待输入的 Partner 数量
 """
        if not self.progress:
            self.progress = TaskExecutionProgress(current_phase=phase or TaskExecutionPhase.EXECUTION_STARTED)

        if phase:
            self.progress.current_phase = phase
        if completed_partners is not None:
            self.progress.completed_partners = completed_partners
        if failed_partners is not None:
            self.progress.failed_partners = failed_partners
        if awaiting_input_partners is not None:
            self.progress.awaiting_input_partners = awaiting_input_partners

 # 计算进度百分比
        total = self.progress.total_partners
        if total > 0:
            done = self.progress.completed_partners + self.progress.failed_partners
            self.progress.progress_percent = (done / total) * 100.0

        self.updated_at = now_iso

    def is_terminal(self) -> bool:
 """检查是否处于终态。"""
        return self.status in (
            TaskExecutionStatus.COMPLETED,
            TaskExecutionStatus.FAILED,
            TaskExecutionStatus.CANCELLED,
        )

    def is_pending_or_running(self) -> bool:
 """检查是否处于待执行或执行中状态。"""
        return self.status in (
            TaskExecutionStatus.PENDING,
            TaskExecutionStatus.RUNNING,
        )
