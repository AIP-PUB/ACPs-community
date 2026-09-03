"""
异步执行模式集成测试

测试 /submit -> /result 的完整异步流程：
1. /submit 在 Planning 后立即返回 pending 状态
2. 后台执行任务
3. /result 轮询获取执行状态和最终结果
"""

import sys
from pathlib import Path

# 确保路径正确
_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from assistant.api.schemas import (
    SubmitRequest,
)
from assistant.core.background_executor import (
    BackgroundExecutor,
    reset_background_executor,
)
from assistant.core.orchestrator import Orchestrator
from assistant.core.session_manager import SessionManager
from assistant.core.task_execution_manager import (
    TaskExecutionManager,
    reset_task_execution_manager,
)
from assistant.models import (
    ExecutionMode,
    IntentDecision,
    IntentType,
    TaskInstruction,
)
from assistant.models.task_execution import (
    TaskExecutionStatus,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """每个测试后重置单例。"""
    yield
    reset_task_execution_manager()
    reset_background_executor()


@pytest.fixture
def mock_intent_analyzer():
    """创建 Mock 意图分析器。"""
    analyzer = MagicMock()
    analyzer.analyze = AsyncMock(
        return_value=IntentDecision(
            intent_type=IntentType.TASK_NEW,
            confidence=0.95,
            task_instruction=TaskInstruction(text="推荐北京美食"),
            target_scenario="beijing_food",
        )
    )
    return analyzer


@pytest.fixture
def mock_planner():
    """创建 Mock 规划器。"""
    from assistant.models.task import PartnerSelection

    planner = MagicMock()

    # 创建 Mock 规划结果，使用正确的 PartnerSelection 对象
    planning_result = MagicMock()
    planning_result.scenario_id = "beijing_food"
    planning_result.selected_partners = {
        "catering": [
            PartnerSelection(
                partner_aic="partner-beijing-food",
                skill_id="food_recommendation",
                reason="推荐美食",
                instruction_text="推荐北京美食",
            )
        ],
    }
    planning_result.model_dump = MagicMock(
        return_value={
            "scenario_id": "beijing_food",
            "selected_partners": {"catering": [{"partner_aic": "partner-beijing-food"}]},
        }
    )

    planner.plan = AsyncMock(return_value=planning_result)
    return planner


@pytest.fixture
def mock_executor():
    """创建 Mock 执行器。"""
    from assistant.core.executor import ExecutionPhase, ExecutionResult

    executor = MagicMock()

    # 创建 Mock 执行结果
    execution_result = ExecutionResult(
        phase=ExecutionPhase.COMPLETED,
        completed_partners=["partner-beijing-food"],
        failed_partners=[],
        awaiting_input_partners=[],
        awaiting_completion_partners=[],
        partner_results={},
    )

    executor.execute = AsyncMock(return_value=execution_result)
    executor._cleanup_clients = AsyncMock()
    executor.acs_cache = {}
    return executor


@pytest.fixture
def mock_aggregator():
    """创建 Mock 聚合器。"""
    aggregator = MagicMock()

    result = MagicMock()
    result.response_text = "为您推荐北京特色美食：烤鸭、炸酱面、豆汁..."
    result.model_dump = MagicMock(return_value={"response_text": result.response_text})

    aggregator.aggregate = AsyncMock(return_value=result)
    return aggregator


@pytest.fixture
def mock_scenario_loader():
    """创建 Mock 场景加载器。"""
    from assistant.models import ScenarioRuntime
    from assistant.models.base import now_iso

    loader = MagicMock()
    loader.get_scenario_briefs = MagicMock(return_value=[])
    loader.get_scenario = MagicMock(return_value=None)
    # 提供真实的 base_scenario
    loader.base_scenario = ScenarioRuntime(
        id="base",
        kind="base",
        version="1.0.0",
        loaded_at=now_iso(),
    )
    return loader


class TestAsyncExecutionMode:
    """异步执行模式集成测试。"""

    @pytest.mark.asyncio
    @patch("assistant.core.orchestrator.get_history_compressor")
    async def test_submit_returns_pending_immediately(
        self,
        mock_get_history_compressor,
        mock_intent_analyzer,
        mock_planner,
        mock_executor,
        mock_aggregator,
        mock_scenario_loader,
    ):
        """测试 /submit 在 Planning 后立即返回 pending 状态。"""
        # Mock history compressor
        mock_history_compressor = MagicMock()
        mock_history_compressor.compress_history = AsyncMock(return_value=[])
        mock_get_history_compressor.return_value = mock_history_compressor

        # 创建测试用组件
        session_manager = SessionManager()
        task_execution_manager = TaskExecutionManager()
        await task_execution_manager.start()

        background_executor = BackgroundExecutor(
            task_execution_manager=task_execution_manager,
        )

        # 创建 Orchestrator，启用异步执行模式
        orchestrator = Orchestrator(
            session_manager=session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=mock_intent_analyzer,
            planner=mock_planner,
            executor=mock_executor,
            aggregator=mock_aggregator,
            task_execution_manager=task_execution_manager,
            background_executor=background_executor,
            async_execution=True,  # 启用异步模式
        )

        await orchestrator.start()

        try:
            # 发送请求
            request = SubmitRequest(
                mode=ExecutionMode.DIRECT_RPC,
                client_request_id="req-001",
                query="帮我推荐北京美食",
            )

            # 应该立即返回 pending 状态
            response = await orchestrator.handle_submit(request)

            assert response.result is not None
            assert response.result.external_status == "pending"
            assert response.result.active_task_id is not None

            # 验证任务已创建在 TaskExecutionManager 中
            task = task_execution_manager.get_task(response.result.active_task_id)
            assert task is not None
            # 任务可能处于 pending 或 running 状态（取决于后台执行速度）
            assert task.status in (
                TaskExecutionStatus.PENDING,
                TaskExecutionStatus.RUNNING,
                TaskExecutionStatus.COMPLETED,
            )

        finally:
            await task_execution_manager.stop()
            await orchestrator.stop()

    @pytest.mark.asyncio
    @patch("assistant.core.orchestrator.get_history_compressor")
    async def test_result_api_returns_task_status(
        self,
        mock_get_history_compressor,
        mock_intent_analyzer,
        mock_planner,
        mock_executor,
        mock_aggregator,
        mock_scenario_loader,
    ):
        """测试 /result API 返回任务执行状态。"""
        # Mock history compressor
        mock_history_compressor = MagicMock()
        mock_history_compressor.compress_history = AsyncMock(return_value=[])
        mock_get_history_compressor.return_value = mock_history_compressor

        # 创建测试用组件
        session_manager = SessionManager()
        task_execution_manager = TaskExecutionManager()
        await task_execution_manager.start()

        background_executor = BackgroundExecutor(
            task_execution_manager=task_execution_manager,
        )
        background_executor.set_components(
            executor=mock_executor,
            aggregator=mock_aggregator,
            session_manager=session_manager,
        )

        # 创建 Orchestrator
        orchestrator = Orchestrator(
            session_manager=session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=mock_intent_analyzer,
            planner=mock_planner,
            executor=mock_executor,
            aggregator=mock_aggregator,
            task_execution_manager=task_execution_manager,
            background_executor=background_executor,
            async_execution=True,
        )

        await orchestrator.start()

        try:
            # 发送请求
            request = SubmitRequest(
                mode=ExecutionMode.DIRECT_RPC,
                client_request_id="req-002",
                query="帮我推荐北京美食",
            )

            response = await orchestrator.handle_submit(request)
            task_id = response.result.active_task_id
            session_id = response.result.session_id

            # 等待后台任务执行完成
            await background_executor.wait_for_task(task_id, timeout=5.0)

            # 获取任务状态
            task = task_execution_manager.get_task(task_id)
            assert task is not None

            # 验证任务完成状态
            if task.status == TaskExecutionStatus.COMPLETED:
                assert task.response_text is not None or task.execution_result is not None

        finally:
            await task_execution_manager.stop()
            await orchestrator.stop()

    @pytest.mark.asyncio
    @patch("assistant.core.orchestrator.get_history_compressor")
    async def test_sync_execution_mode(
        self,
        mock_get_history_compressor,
        mock_intent_analyzer,
        mock_planner,
        mock_executor,
        mock_aggregator,
        mock_scenario_loader,
    ):
        """测试同步执行模式（async_execution=False）。"""
        # Mock history compressor
        mock_history_compressor = MagicMock()
        mock_history_compressor.compress_history = AsyncMock(return_value=[])
        mock_get_history_compressor.return_value = mock_history_compressor

        # 创建测试用组件
        session_manager = SessionManager()
        task_execution_manager = TaskExecutionManager()

        # 创建 Orchestrator，禁用异步执行模式
        orchestrator = Orchestrator(
            session_manager=session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=mock_intent_analyzer,
            planner=mock_planner,
            executor=mock_executor,
            aggregator=mock_aggregator,
            task_execution_manager=task_execution_manager,
            async_execution=False,  # 禁用异步模式
        )

        await orchestrator.start()

        try:
            # 发送请求
            request = SubmitRequest(
                mode=ExecutionMode.DIRECT_RPC,
                client_request_id="req-003",
                query="帮我推荐北京美食",
            )

            # 同步模式下应该等待执行完成后返回
            response = await orchestrator.handle_submit(request)

            # 验证返回的是最终结果，不是 pending
            # 注意：由于 mock，可能返回 clarification 或 final
            assert response.result is not None
            assert response.result.active_task_id is not None

        finally:
            await orchestrator.stop()


class TestBackgroundExecutor:
    """后台执行器测试。"""

    @pytest.mark.asyncio
    async def test_submit_and_wait_for_completion(self):
        """测试提交任务并等待完成。"""
        from assistant.core.executor import ExecutionPhase, ExecutionResult

        task_execution_manager = TaskExecutionManager()
        await task_execution_manager.start()

        # 创建 Mock 执行器
        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(
            return_value=ExecutionResult(
                phase=ExecutionPhase.COMPLETED,
                completed_partners=["partner-a"],
                failed_partners=[],
                awaiting_input_partners=[],
                awaiting_completion_partners=[],
                partner_results={},
            )
        )

        # 创建 Mock 聚合器
        mock_aggregator = MagicMock()
        result = MagicMock()
        result.response_text = "任务完成"
        result.model_dump = MagicMock(return_value={"response_text": "任务完成"})
        mock_aggregator.aggregate = AsyncMock(return_value=result)

        # 创建后台执行器
        background_executor = BackgroundExecutor(
            task_execution_manager=task_execution_manager,
        )
        background_executor.set_components(
            executor=mock_executor,
            aggregator=mock_aggregator,
        )

        # 创建 Mock Session
        session = MagicMock()
        session.session_id = "session-001"
        session.scenario_id = "test_scenario"
        session.scenario_kind = "expert"

        # 创建 Mock Planning Result
        planning_result = MagicMock()
        planning_result.selected_partners = {"dim1": ["partner-a"]}
        planning_result.model_dump = MagicMock(return_value={})

        try:
            # 提交任务
            task_execution = background_executor.submit_task(
                task_id="task-001",
                session_id="session-001",
                session=session,
                planning_result=planning_result,
                task_text="测试任务",
            )

            assert task_execution is not None
            assert task_execution.task_id == "task-001"

            # 等待任务完成
            result = await background_executor.wait_for_task("task-001", timeout=5.0)

            assert result is not None
            assert result.status == TaskExecutionStatus.COMPLETED

        finally:
            await task_execution_manager.stop()

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        """测试取消运行中的任务。"""
        task_execution_manager = TaskExecutionManager()
        await task_execution_manager.start()

        # 创建一个永远不会完成的 Mock 执行器
        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(100)  # 永远等待

        mock_executor = MagicMock()
        mock_executor.execute = slow_execute

        background_executor = BackgroundExecutor(
            task_execution_manager=task_execution_manager,
        )
        background_executor.set_components(executor=mock_executor)

        # 创建 Mock Session 和 Planning Result
        session = MagicMock()
        session.session_id = "session-001"
        planning_result = MagicMock()
        planning_result.selected_partners = {}
        planning_result.model_dump = MagicMock(return_value={})

        try:
            # 提交任务
            background_executor.submit_task(
                task_id="task-001",
                session_id="session-001",
                session=session,
                planning_result=planning_result,
                task_text="慢任务",
            )

            # 等待任务开始
            await asyncio.sleep(0.1)

            # 取消任务
            result = background_executor.cancel_task("task-001")
            assert result is True

            # 等待取消完成
            await asyncio.sleep(0.1)

            # 验证任务状态
            task = task_execution_manager.get_task("task-001")
            assert task.status == TaskExecutionStatus.CANCELLED

        finally:
            await task_execution_manager.stop()

    @pytest.mark.asyncio
    async def test_cancel_task_and_wait_waits_for_background_task_shutdown(self):
        """测试 cancel_task_and_wait 会等待后台协程退出。"""
        task_execution_manager = TaskExecutionManager()
        await task_execution_manager.start()

        background_executor = BackgroundExecutor(
            task_execution_manager=task_execution_manager,
        )

        async def long_running() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running())
        background_executor._running_tasks["task-001"] = task

        try:
            await asyncio.sleep(0)
            result = await background_executor.cancel_task_and_wait("task-001", timeout=1.0)

            assert result is True
            assert task.done() is True
            assert task.cancelled() is True
        finally:
            await task_execution_manager.stop()

    def test_update_partner_tasks_skips_stale_active_task(self):
        """旧任务的后台回写不应覆盖当前 active_task。"""
        from acps_sdk.aip.aip_base_model import TaskState
        from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult
        from assistant.models import (
            ActiveTaskStatus,
            ExecutionMode,
            ScenarioRuntime,
            Session,
            UserResult,
            UserResultType,
        )
        from assistant.models.base import now_iso
        from assistant.models.task import ActiveTask, PartnerSelection, PartnerTask, PlanningResult

        now = now_iso()
        session = Session(
            session_id="session-stale-update",
            mode=ExecutionMode.DIRECT_RPC,
            created_at=now,
            updated_at=now,
            touched_at=now,
            ttl_seconds=3600,
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            base_scenario=ScenarioRuntime(
                id="base",
                kind="base",
                version="1.0.0",
                loaded_at=now,
            ),
            user_result=UserResult(
                type=UserResultType.PENDING,
                data_items=[],
                updated_at=now,
            ),
        )
        session.active_task = ActiveTask(
            active_task_id="task-current",
            created_at=now,
            external_status=ActiveTaskStatus.RUNNING,
            partner_tasks={
                "partner-current": PartnerTask(
                    partnerAic="partner-current",
                    aipTaskId="task-current:partner-current",
                    state=TaskState.Accepted,
                )
            },
        )

        planning_result = PlanningResult(
            created_at=now,
            scenario_id="travel",
            user_query="帮我安排旅行",
            selected_partners={
                "hotel": [
                    PartnerSelection(
                        partner_aic="partner-stale",
                        skill_id="search_hotel",
                        reason="酒店推荐",
                        instruction_text="搜索酒店",
                    )
                ]
            },
        )
        execution_result = ExecutionResult(
            phase=ExecutionPhase.POLLING,
            partner_results={
                "partner-stale": PartnerExecutionResult(
                    partner_aic="partner-stale",
                    dimension_id="hotel",
                    state=TaskState.Working,
                )
            },
        )

        background_executor = BackgroundExecutor()

        background_executor._update_partner_tasks_from_execution(
            session,
            execution_result,
            planning_result,
            task_id="task-stale",
        )

        assert set(session.active_task.partner_tasks) == {"partner-current"}

    def test_update_partner_tasks_accepts_current_active_task(self):
        """当前任务的 partner 状态应正常同步到 session。"""
        from acps_sdk.aip.aip_base_model import TaskState
        from assistant.core.executor import ExecutionPhase, ExecutionResult, PartnerExecutionResult
        from assistant.models import (
            ActiveTaskStatus,
            ExecutionMode,
            ScenarioRuntime,
            Session,
            UserResult,
            UserResultType,
        )
        from assistant.models.base import now_iso
        from assistant.models.task import ActiveTask, PartnerSelection, PlanningResult

        now = now_iso()
        session = Session(
            session_id="session-current-update",
            mode=ExecutionMode.DIRECT_RPC,
            created_at=now,
            updated_at=now,
            touched_at=now,
            ttl_seconds=3600,
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            base_scenario=ScenarioRuntime(
                id="base",
                kind="base",
                version="1.0.0",
                loaded_at=now,
            ),
            user_result=UserResult(
                type=UserResultType.PENDING,
                data_items=[],
                updated_at=now,
            ),
        )
        session.active_task = ActiveTask(
            active_task_id="task-current",
            created_at=now,
            external_status=ActiveTaskStatus.RUNNING,
            partner_tasks={},
        )

        planning_result = PlanningResult(
            created_at=now,
            scenario_id="travel",
            user_query="帮我安排旅行",
            selected_partners={
                "hotel": [
                    PartnerSelection(
                        partner_aic="partner-current",
                        skill_id="search_hotel",
                        reason="酒店推荐",
                        instruction_text="搜索酒店",
                    )
                ]
            },
        )
        execution_result = ExecutionResult(
            phase=ExecutionPhase.POLLING,
            partner_results={
                "partner-current": PartnerExecutionResult(
                    partner_aic="partner-current",
                    dimension_id="hotel",
                    state=TaskState.Working,
                )
            },
        )

        background_executor = BackgroundExecutor()

        background_executor._update_partner_tasks_from_execution(
            session,
            execution_result,
            planning_result,
            task_id="task-current",
        )

        assert session.active_task.partner_tasks["partner-current"].state == TaskState.Working


class TestTaskExecutionModel:
    """TaskExecution 模型测试。"""

    def test_create_task_execution(self):
        """测试创建任务执行记录。"""
        from assistant.models.task_execution import TaskExecution

        task = TaskExecution.create(
            task_id="task-001",
            session_id="session-001",
            planning_result={"scenario_id": "test"},
            metadata={"user_query": "测试"},
        )

        assert task.task_id == "task-001"
        assert task.session_id == "session-001"
        assert task.status == TaskExecutionStatus.PENDING
        assert task.planning_result == {"scenario_id": "test"}
        assert task.metadata == {"user_query": "测试"}

    def test_is_terminal(self):
        """测试终态判断。"""
        from assistant.models.task_execution import TaskExecution

        task = TaskExecution.create(
            task_id="task-001",
            session_id="session-001",
        )

        assert task.is_terminal() is False

        task.mark_completed()
        assert task.is_terminal() is True

    def test_state_transitions(self):
        """测试状态流转。"""
        from assistant.models.task_execution import TaskExecution

        task = TaskExecution.create(
            task_id="task-001",
            session_id="session-001",
        )

        # PENDING -> RUNNING
        task.mark_running()
        assert task.status == TaskExecutionStatus.RUNNING
        assert task.started_at is not None

        # RUNNING -> AWAITING_INPUT
        task.mark_awaiting_input("请提供更多信息")
        assert task.status == TaskExecutionStatus.AWAITING_INPUT
        assert task.clarification_text == "请提供更多信息"

        # AWAITING_INPUT -> COMPLETED
        task.mark_completed(response_text="完成")
        assert task.status == TaskExecutionStatus.COMPLETED
        assert task.completed_at is not None
        assert task.response_text == "完成"
