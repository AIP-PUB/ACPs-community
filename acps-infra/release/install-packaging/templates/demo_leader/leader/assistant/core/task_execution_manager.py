"""
Leader Agent Platform - TaskExecutionManager

本模块实现任务执行的内存管理器，负责：
1. 任务执行状态的存储与查询
2. 任务生命周期管理
3. 并发安全的状态更新

设计说明：
- 当前版本使用内存存储，生产环境可扩展为 Redis/数据库
- 支持按 session_id 和 task_id 查询
- 支持任务过期自动清理（可选）
"""

import asyncio
import logging
from datetime import UTC, datetime
from threading import Lock

from ..models.base import ActiveTaskId, SessionId, now_iso
from ..models.task_execution import (
    TaskExecution,
    TaskExecutionPhase,
)

logger = logging.getLogger(__name__)

# 默认任务保留时间（秒）
DEFAULT_TASK_RETENTION_SECONDS = 3600  # 1 小时


class TaskExecutionManager:
 """
    任务执行管理器。

    提供任务执行状态的 CRUD 操作和生命周期管理。
    使用线程锁保证并发安全。
 """

    def __init__(
        self,
        retention_seconds: int = DEFAULT_TASK_RETENTION_SECONDS,
    ):
 """
        初始化任务执行管理器。

        Args:
            retention_seconds: 已完成任务的保留时间（秒）
 """
 # 主存储：task_id -> TaskExecution
        self._tasks: dict[ActiveTaskId, TaskExecution] = {}

 # 索引：session_id -> List[task_id]
        self._session_index: dict[SessionId, list[ActiveTaskId]] = {}

 # 并发锁
        self._lock = Lock

 # 配置
        self._retention_seconds = retention_seconds

 # 清理任务句柄
        self._cleanup_task: asyncio.Task | None = None

 # =========================================================================
 # 生命周期方法
 # =========================================================================

    async def start(self) -> None:
 """启动管理器（包括定期清理任务）。"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop)
            logger.info("TaskExecutionManager started with cleanup loop")

    async def stop(self) -> None:
 """停止管理器。"""
        if self._cleanup_task:
            self._cleanup_task.cancel
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("TaskExecutionManager stopped")

    async def _cleanup_loop(self) -> None:
 """定期清理过期任务的后台循环。"""
        while True:
            try:
                await asyncio.sleep(300)  # 每 5 分钟检查一次
                self.cleanup_expired_tasks
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    def cleanup_expired_tasks(self) -> int:
 """
        清理过期的已完成任务。

        Returns:
            清理的任务数量
 """
        with self._lock:
            now = datetime.now(UTC)
            expired_task_ids = []

            for task_id, task in self._tasks.items:
                if task.is_terminal and task.completed_at:
 # 解析完成时间
                    try:
                        completed_time = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))
                        if (now - completed_time).total_seconds > self._retention_seconds:
                            expired_task_ids.append(task_id)
                    except ValueError, AttributeError:
                        pass

 # 删除过期任务
            for task_id in expired_task_ids:
                task = self._tasks.pop(task_id, None)
                if task:
 # 从索引中移除
                    session_tasks = self._session_index.get(task.session_id, [])
                    if task_id in session_tasks:
                        session_tasks.remove(task_id)
                    if not session_tasks:
                        self._session_index.pop(task.session_id, None)

            if expired_task_ids:
                logger.info(f"Cleaned up {len(expired_task_ids)} expired tasks")

            return len(expired_task_ids)

 # =========================================================================
 # CRUD 操作
 # =========================================================================

    def create_task(
        self,
        task_id: ActiveTaskId,
        session_id: SessionId,
        planning_result: dict | None = None,
        metadata: dict | None = None,
    ) -> TaskExecution:
 """
        创建新的任务执行记录。

        Args:
            task_id: 任务 ID
            session_id: 会话 ID
            planning_result: 规划结果
            metadata: 元数据

        Returns:
            创建的 TaskExecution 实例
 """
        with self._lock:
            if task_id in self._tasks:
                logger.warning(f"Task {task_id} already exists, returning existing")
                return self._tasks[task_id]

            task = TaskExecution.create(
                task_id=task_id,
                session_id=session_id,
                planning_result=planning_result,
                metadata=metadata,
            )

            self._tasks[task_id] = task

 # 更新索引
            if session_id not in self._session_index:
                self._session_index[session_id] = []
            self._session_index[session_id].append(task_id)

            logger.debug(f"Created task execution: {task_id}")
            return task

    def get_task(self, task_id: ActiveTaskId) -> TaskExecution | None:
 """
        获取任务执行记录。

        Args:
            task_id: 任务 ID

        Returns:
            TaskExecution 实例，不存在则返回 None
 """
        with self._lock:
            return self._tasks.get(task_id)

    def get_tasks_by_session(
        self,
        session_id: SessionId,
        include_terminal: bool = True,
    ) -> list[TaskExecution]:
 """
        获取会话的所有任务执行记录。

        Args:
            session_id: 会话 ID
            include_terminal: 是否包含已完成/失败/取消的任务

        Returns:
            TaskExecution 列表
 """
        with self._lock:
            task_ids = self._session_index.get(session_id, [])
            tasks = []
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task:
                    if include_terminal or not task.is_terminal:
                        tasks.append(task)
            return tasks

    def get_active_task_for_session(
        self,
        session_id: SessionId,
    ) -> TaskExecution | None:
 """
        获取会话当前活跃的任务（非终态）。

        Args:
            session_id: 会话 ID

        Returns:
            活跃的 TaskExecution，不存在则返回 None
 """
        tasks = self.get_tasks_by_session(session_id, include_terminal=False)
 # 返回最新创建的非终态任务
        if tasks:
            return max(tasks, key=lambda t: t.created_at)
        return None

    def get_latest_task_for_session(
        self,
        session_id: SessionId,
    ) -> TaskExecution | None:
 """
        获取会话最新的任务（包括已完成的）。

        Args:
            session_id: 会话 ID

        Returns:
            最新的 TaskExecution，不存在则返回 None
 """
        tasks = self.get_tasks_by_session(session_id, include_terminal=True)
        if tasks:
            return max(tasks, key=lambda t: t.created_at)
        return None

    def update_task(
        self,
        task_id: ActiveTaskId,
        **kwargs,
    ) -> TaskExecution | None:
 """
        更新任务执行记录。

        Args:
            task_id: 任务 ID
            **kwargs: 要更新的字段

        Returns:
            更新后的 TaskExecution，不存在则返回 None
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            for key, value in kwargs.items:
                if hasattr(task, key):
                    setattr(task, key, value)

            task.updated_at = now_iso
            return task

    def delete_task(self, task_id: ActiveTaskId) -> bool:
 """
        删除任务执行记录。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
 """
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task:
 # 从索引中移除
                session_tasks = self._session_index.get(task.session_id, [])
                if task_id in session_tasks:
                    session_tasks.remove(task_id)
                if not session_tasks:
                    self._session_index.pop(task.session_id, None)
                logger.debug(f"Deleted task execution: {task_id}")
                return True
            return False

 # =========================================================================
 # 状态更新便捷方法
 # =========================================================================

    def mark_task_running(self, task_id: ActiveTaskId) -> TaskExecution | None:
 """
        将任务标记为运行中。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.mark_running
                logger.debug(f"Task {task_id} marked as running")
            return task

    def mark_task_awaiting_input(
        self,
        task_id: ActiveTaskId,
        clarification_text: str,
        execution_result: dict | None = None,
    ) -> TaskExecution | None:
 """
        将任务标记为等待用户输入。

        Args:
            task_id: 任务 ID
            clarification_text: 反问文本
            execution_result: 执行结果（包含 partner 状态信息）

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.mark_awaiting_input(clarification_text, execution_result)
                logger.debug(f"Task {task_id} marked as awaiting input")
            return task

    def mark_task_completed(
        self,
        task_id: ActiveTaskId,
        execution_result: dict | None = None,
        aggregation_result: dict | None = None,
        response_text: str | None = None,
    ) -> TaskExecution | None:
 """
        将任务标记为完成。

        Args:
            task_id: 任务 ID
            execution_result: 执行结果
            aggregation_result: 聚合结果
            response_text: 最终响应文本

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.mark_completed(
                    execution_result=execution_result,
                    aggregation_result=aggregation_result,
                    response_text=response_text,
                )
                logger.debug(f"Task {task_id} marked as completed")
            return task

    def mark_task_failed(
        self,
        task_id: ActiveTaskId,
        error_message: str,
        error_details: dict | None = None,
    ) -> TaskExecution | None:
 """
        将任务标记为失败。

        Args:
            task_id: 任务 ID
            error_message: 错误信息
            error_details: 详细错误

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.mark_failed(error_message, error_details)
                logger.error(f"Task {task_id} marked as failed: {error_message}")
            return task

    def mark_task_cancelled(
        self,
        task_id: ActiveTaskId,
    ) -> TaskExecution | None:
 """
        将任务标记为已取消。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.mark_cancelled
                logger.info(f"Task {task_id} marked as cancelled")
            return task

    def update_task_progress(
        self,
        task_id: ActiveTaskId,
        phase: TaskExecutionPhase | None = None,
        total_partners: int | None = None,
        completed_partners: int | None = None,
        failed_partners: int | None = None,
        awaiting_input_partners: int | None = None,
    ) -> TaskExecution | None:
 """
        更新任务执行进度。

        Args:
            task_id: 任务 ID
            phase: 当前阶段
            total_partners: 总 Partner 数
            completed_partners: 已完成数
            failed_partners: 失败数
            awaiting_input_partners: 等待输入数

        Returns:
            更新后的 TaskExecution
 """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                if total_partners is not None and task.progress:
                    task.progress.total_partners = total_partners
                task.update_progress(
                    phase=phase,
                    completed_partners=completed_partners,
                    failed_partners=failed_partners,
                    awaiting_input_partners=awaiting_input_partners,
                )
            return task

 # =========================================================================
 # 统计方法
 # =========================================================================

    def get_stats(self) -> dict[str, int]:
 """
        获取管理器统计信息。

        Returns:
            统计数据字典
 """
        with self._lock:
            stats = {
                "total_tasks": len(self._tasks),
                "total_sessions": len(self._session_index),
                "pending": 0,
                "running": 0,
                "awaiting_input": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
            }

            for task in self._tasks.values:
                status_key = task.status.value
                if status_key in stats:
                    stats[status_key] += 1

            return stats


# =============================================================================
# 单例获取函数
# =============================================================================

_task_execution_manager: TaskExecutionManager | None = None


def get_task_execution_manager -> TaskExecutionManager:
 """
    获取 TaskExecutionManager 单例。

    Returns:
        TaskExecutionManager 实例
 """
    global _task_execution_manager
    if _task_execution_manager is None:
        _task_execution_manager = TaskExecutionManager
    return _task_execution_manager


def reset_task_execution_manager -> None:
 """
    重置 TaskExecutionManager 单例（仅用于测试）。
 """
    global _task_execution_manager
    _task_execution_manager = None
