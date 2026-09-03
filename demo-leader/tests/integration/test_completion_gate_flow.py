"""
Leader Agent Platform - 集成测试：CompletionGate 完整流程

测试 LLM-5 CompletionGate 的完整业务流程：
1. LLM-1: 意图分析
2. LLM-2: 全量规划
3. Task Executor: 下发任务到 Partner
4. Partner: 返回 AwaitingCompletion
5. LLM-5: CompletionGate 评估决策
6. 执行 complete/continue
7. LLM-6: Aggregator 结果整合
8. 返回最终用户结果

需要先在 demo-partner 仓库中启动 Partner 服务：just dev bootstrap && just dev start
本测试使用真实大模型 API 调用。
"""

import os
import sys

# 确保路径正确
_current_dir = os.path.dirname(os.path.abspath(__file__))
_tests_root = os.path.dirname(_current_dir)
_project_root = os.path.dirname(_tests_root)
_leader_dir = os.path.join(_project_root, "leader")
if _leader_dir not in sys.path:
    sys.path.insert(0, _leader_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


import httpx
import pytest
from acps_sdk.aip.aip_base_model import TaskState
from assistant.core.aggregator import (
    Aggregator,
    DegradationInfo,
    PartnerOutput,
    get_aggregator,
)
from assistant.core.completion_gate import (
    CompletionGate,
    PartnerProductSummary,
    get_completion_gate,
)
from assistant.core.executor import (
    ExecutionPhase,
    ExecutionResult,
    ExecutorConfig,
    TaskExecutor,
    extract_partner_endpoint,
)
from assistant.core.intent_analyzer import IntentAnalyzer, create_intent_analyzer
from assistant.core.planner import Planner
from assistant.models import IntentType
from assistant.models.session import Session
from assistant.services import ScenarioLoader

pytest_plugins = ("pytest_asyncio",)


# =============================================================================
# ACS 缓存配置（Partner 服务端点）
# =============================================================================

ACS_CACHE: dict[str, dict[str, list[dict[str, str]]]] = {}

DIM_TO_PARTNER_ENDPOINT: dict[str, str] = {}


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="class")
def partner_endpoint_cache(managed_partner_runtime):
    """
    构建基于临时 Partner runtime 的 ACS 缓存和维度端点映射。
    """
    ACS_CACHE.clear()
    ACS_CACHE.update(
        {
            "10001000011K9TG3R62N7L14TSRCD015": {
                "endPoints": [
                    {
                        "url": f"{managed_partner_runtime.agent_urls['china_hotel']}/rpc",
                        "transport": "HTTP",
                    }
                ]
            },
            "10001000011K9TG3R62N7L14TSRCD014": {
                "endPoints": [
                    {
                        "url": f"{managed_partner_runtime.agent_urls['china_transport']}/rpc",
                        "transport": "HTTP",
                    }
                ]
            },
            "10001000011K9TG3R62N7L14TSRCD012": {
                "endPoints": [
                    {
                        "url": f"{managed_partner_runtime.agent_urls['beijing_food']}/rpc",
                        "transport": "HTTP",
                    }
                ]
            },
        }
    )
    DIM_TO_PARTNER_ENDPOINT.clear()
    DIM_TO_PARTNER_ENDPOINT.update(
        {
            "hotel": f"{managed_partner_runtime.agent_urls['china_hotel']}/rpc",
            "transport": f"{managed_partner_runtime.agent_urls['china_transport']}/rpc",
            "food": f"{managed_partner_runtime.agent_urls['beijing_food']}/rpc",
        }
    )
    return managed_partner_runtime


@pytest.fixture(scope="class")
def require_partner_service(partner_endpoint_cache):
    """确保临时 Partner runtime 已就绪。"""
    return partner_endpoint_cache


@pytest.fixture
def scenario_loader(managed_partner_runtime):
    """场景加载器"""
    return ScenarioLoader(managed_partner_runtime.scenario_root)


@pytest.fixture
def intent_analyzer(scenario_loader: ScenarioLoader):
    """意图分析器"""
    return create_intent_analyzer(scenario_loader)


@pytest.fixture
def planner(scenario_loader: ScenarioLoader):
    """规划器"""
    return Planner(scenario_loader)


@pytest.fixture
def executor(partner_endpoint_cache):
    """任务执行器"""
    exec_instance = TaskExecutor(
        leader_aic="test-leader-e2e",
        config=ExecutorConfig(
            poll_interval_ms=1000,
            max_execution_rounds=30,
            convergence_timeout_s=120,
        ),
    )
    exec_instance.acs_cache = ACS_CACHE
    return exec_instance


@pytest.fixture
def completion_gate():
    """完成闸门"""
    return get_completion_gate()


@pytest.fixture
def aggregator():
    """结果整合器"""
    return get_aggregator()


def create_test_session(session_id: str) -> Session:
    """创建测试 Session"""
    from assistant.models import (
        DialogContext,
        ExecutionMode,
        ScenarioRuntime,
        SessionStatus,
        UserResult,
        UserResultType,
        now_iso,
    )

    now = now_iso()
    base_scenario = ScenarioRuntime(
        id="base",
        kind="base",
        version="1.0.0",
        loaded_at=now,
        prompts={},
    )
    return Session(
        session_id=session_id,
        status=SessionStatus.ACTIVE,
        mode=ExecutionMode.DIRECT_RPC,
        base_scenario=base_scenario,
        expert_scenario=base_scenario,
        created_at=now,
        updated_at=now,
        touched_at=now,
        ttl_seconds=3600,
        expires_at=now,
        dialog_context=DialogContext(
            session_id=session_id,
            updated_at=now,
            recent_turns=[],
            history_summary="",
        ),
        event_log=[],
        active_task=None,
        pending_clarification=None,
        user_context={},
        user_result=UserResult(type=UserResultType.PENDING, updated_at=now),
    )


# =============================================================================
# 辅助函数
# =============================================================================


def build_partner_summaries(
    execution_result: ExecutionResult,
    active_task_id: str,
) -> list[PartnerProductSummary]:
    """构建 Partner 产出物摘要列表"""
    summaries = []
    for pac in execution_result.awaiting_completion_partners:
        pr = execution_result.partner_results.get(pac)
        if not pr:
            continue

        data_items = []
        if pr.data_items:
            for item in pr.data_items:
                if hasattr(item, "model_dump"):
                    data_items.append(item.model_dump())
                elif hasattr(item, "text"):
                    data_items.append({"type": "text", "text": item.text})

        summary = PartnerProductSummary(
            partner_aic=pac,
            aip_task_id=f"{active_task_id}:{pac}",
            dimension_id=pr.dimension_id,
            state=pr.state.value,
            data_items=data_items,
        )
        summaries.append(summary)
    return summaries


def build_partner_outputs(
    execution_result: ExecutionResult,
) -> list[PartnerOutput]:
    """构建 PartnerOutput 列表"""
    outputs = []
    for partner_aic, result in execution_result.partner_results.items():
        data_items = []
        for item in result.data_items:
            if hasattr(item, "model_dump"):
                data_items.append(item.model_dump())
            elif hasattr(item, "dict"):
                data_items.append(item.dict())
            elif isinstance(item, dict):
                data_items.append(item)
            else:
                data_items.append({"text": str(item)})

        products = []
        if partner_aic in execution_result.products:
            for prod in execution_result.products[partner_aic]:
                if hasattr(prod, "model_dump"):
                    products.append(prod.model_dump())
                elif hasattr(prod, "dict"):
                    products.append(prod.dict())
                elif isinstance(prod, dict):
                    products.append(prod)
                else:
                    products.append({"text": str(prod)})

        po = PartnerOutput(
            partner_aic=partner_aic,
            dimension_id=result.dimension_id,
            state=(result.state.value if hasattr(result.state, "value") else str(result.state)),
            data_items=data_items,
            products=products,
            error=result.error,
        )
        outputs.append(po)
    return outputs


def get_endpoint_for_partner(
    partner_aic: str,
    execution_result: ExecutionResult,
) -> str:
    """获取 Partner 端点"""
    endpoint = extract_partner_endpoint(ACS_CACHE.get(partner_aic, {}))
    if not endpoint:
        pr = execution_result.partner_results.get(partner_aic)
        if pr:
            endpoint = DIM_TO_PARTNER_ENDPOINT.get(pr.dimension_id)
    return endpoint


# =============================================================================
# 测试类
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.usefixtures("require_partner_service")
class TestCompletionGateFlow:
    """
    CompletionGate 完整流程测试

    需要运行 Partner 服务：cd ../demo-partner && just dev bootstrap && just dev start

    如果 Partner 服务未启动，测试会立即失败并提示启动命令。
    """

    async def test_hotel_query_full_flow(
        self,
        intent_analyzer: IntentAnalyzer,
        planner: Planner,
        executor: TaskExecutor,
        completion_gate: CompletionGate,
        aggregator: Aggregator,
    ):
        """
        测试用例：酒店查询完整流程

        Submit → 意图分析 → 规划 → 执行 → 完成闸门 → 结果整合
        """
        import uuid

        # 提供完整参数，包括入住人数，避免 Partner 返回 AwaitingInput
        user_query = "帮我查询北京国贸附近的酒店，入住日期1月20日，离店1月22日，预算800-1200元/晚，2人入住"
        # 使用动态 ID 避免 Partner 缓存问题
        unique_suffix = uuid.uuid4().hex[:8]
        session_id = f"e2e-hotel-test-{unique_suffix}"
        active_task_id = f"e2e-task-hotel-{unique_suffix}"

        # 创建 Session
        session = create_test_session(session_id)

        # =====================================================================
        # Stage 1: LLM-1 意图分析
        # =====================================================================
        print("\n=== Stage 1: LLM-1 意图分析 ===")

        intent_result = await intent_analyzer.analyze(
            user_query=user_query,
            session=session,
        )

        print(f"意图类型: {intent_result.intent_type.value}")
        print(f"目标场景: {intent_result.target_scenario}")

        # 验证意图分析结果
        assert intent_result.intent_type == IntentType.TASK_NEW, f"Expected TASK_NEW, got {intent_result.intent_type}"
        assert intent_result.target_scenario is not None, "Expected target_scenario to be set"

        # 更新 Session
        scenario_id = intent_result.target_scenario
        session.expert_scenario.id = scenario_id
        session.expert_scenario.kind = "expert"

        # =====================================================================
        # Stage 2: LLM-2 全量规划
        # =====================================================================
        print("\n=== Stage 2: LLM-2 全量规划 ===")

        planning_result = await planner.plan(
            user_query=user_query,
            session=session,
            intent=intent_result,
        )

        active_dims = [dim_id for dim_id, partners in planning_result.selected_partners.items() if partners]
        partner_count = sum(len(partners) for partners in planning_result.selected_partners.values())

        print(f"场景ID: {planning_result.scenario_id}")
        print(f"激活维度: {active_dims}")
        print(f"Partner 数: {partner_count}")

        # 验证规划结果
        assert partner_count > 0, "Expected at least one partner selected"
        assert "hotel" in active_dims, "Expected hotel dimension to be active"

        # =====================================================================
        # Stage 3: Task Executor 执行
        # =====================================================================
        print("\n=== Stage 3: Task Executor 执行 ===")

        execution_result = await executor.execute(
            session_id=session_id,
            active_task_id=active_task_id,
            planning_result=planning_result,
        )

        print(f"执行阶段: {execution_result.phase.value}")
        print(f"AwaitingCompletion: {execution_result.awaiting_completion_partners}")
        print(f"Completed: {execution_result.completed_partners}")

        # 验证执行结果
        assert execution_result.phase in (
            ExecutionPhase.AWAITING_COMPLETION,
            ExecutionPhase.COMPLETED,
        ), f"Unexpected phase: {execution_result.phase}"

        # =====================================================================
        # Stage 4: LLM-5 CompletionGate 评估（如果需要）
        # =====================================================================
        if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
            print("\n=== Stage 4: LLM-5 CompletionGate 评估 ===")

            partner_summaries = build_partner_summaries(execution_result, active_task_id)

            gate_result = await completion_gate.evaluate(
                partner_summaries=partner_summaries,
                user_constraints=None,
                scenario_id=scenario_id,
            )

            print(f"决策数: {len(gate_result.decisions)}")
            assert len(gate_result.decisions) > 0, "Expected at least one decision"

            # 执行决策
            for decision in gate_result.decisions:
                print(f"Partner: {decision.partner_aic[:12]}... -> {decision.next_action}")

                endpoint = get_endpoint_for_partner(decision.partner_aic, execution_result)

                if decision.next_action == "complete":
                    task, error = await executor.complete_partner(
                        session_id=session_id,
                        partner_aic=decision.partner_aic,
                        aip_task_id=decision.aip_task_id,
                        endpoint=endpoint,
                    )

                    if task:
                        print(f"完成后状态: {task.status.state}")
                        pr = execution_result.partner_results.get(decision.partner_aic)
                        if pr:
                            pr.state = task.status.state
                            pr.task = task
                            if task.products:
                                for product in task.products:
                                    if product.dataItems:
                                        pr.data_items.extend(product.dataItems)
                            if task.status.state == TaskState.Completed:
                                if decision.partner_aic in execution_result.awaiting_completion_partners:
                                    execution_result.awaiting_completion_partners.remove(decision.partner_aic)
                                if decision.partner_aic not in execution_result.completed_partners:
                                    execution_result.completed_partners.append(decision.partner_aic)

                        assert task.status.state == TaskState.Completed, f"Expected Completed, got {task.status.state}"
                    elif error:
                        pytest.fail(f"Complete failed: {error}")

                elif decision.next_action == "continue":
                    followup_text = decision.followup.text if decision.followup else "请继续"
                    task, error = await executor.continue_partner(
                        session_id=session_id,
                        partner_aic=decision.partner_aic,
                        aip_task_id=decision.aip_task_id,
                        endpoint=endpoint,
                        user_input=followup_text,
                    )

            if execution_result.completed_partners and not execution_result.awaiting_completion_partners:
                execution_result.phase = ExecutionPhase.COMPLETED

        # =====================================================================
        # Stage 5: LLM-6 Aggregator 结果整合
        # =====================================================================
        print("\n=== Stage 5: LLM-6 Aggregator 结果整合 ===")

        partner_outputs = build_partner_outputs(execution_result)
        degradations = [
            DegradationInfo(
                dimension_id=po.dimension_id,
                reason=po.error or "执行失败",
                suggestion="请稍后重试",
            )
            for po in partner_outputs
            if po.state in ("Failed", "Rejected")
        ]

        print(f"Partner 产出数: {len(partner_outputs)}")

        aggregation_result = await aggregator.aggregate(
            partner_outputs=partner_outputs,
            degradations=degradations,
            user_query=user_query,
            scenario_id=scenario_id,
        )

        print(f"结果类型: {aggregation_result.type}")
        print(f"文本长度: {len(aggregation_result.text)} 字符")
        print(f"\n最终结果预览:\n{aggregation_result.text[:500]}...")

        # 验证整合结果
        assert aggregation_result.type == "final", f"Expected final, got {aggregation_result.type}"
        assert len(aggregation_result.text) > 100, "Expected substantial response text"

        print("\n✅ 完整流程测试通过！")

    async def test_multi_dimension_flow(
        self,
        intent_analyzer: IntentAnalyzer,
        planner: Planner,
        executor: TaskExecutor,
        completion_gate: CompletionGate,
        aggregator: Aggregator,
    ):
        """
        测试用例：多维度请求完整流程

        测试涉及多个维度（酒店+交通）的请求
        """
        import uuid

        user_query = "帮我规划北京两日游，需要酒店和交通安排"
        # 使用动态 ID 避免 Partner 缓存问题
        unique_suffix = uuid.uuid4().hex[:8]
        session_id = f"e2e-multi-test-{unique_suffix}"
        active_task_id = f"e2e-task-multi-{unique_suffix}"

        session = create_test_session(session_id)

        # Stage 1: 意图分析
        print("\n=== Stage 1: LLM-1 意图分析 ===")
        intent_result = await intent_analyzer.analyze(
            user_query=user_query,
            session=session,
        )

        print(f"意图类型: {intent_result.intent_type.value}")
        assert intent_result.intent_type == IntentType.TASK_NEW

        scenario_id = intent_result.target_scenario
        session.expert_scenario.id = scenario_id
        session.expert_scenario.kind = "expert"

        # Stage 2: 规划
        print("\n=== Stage 2: LLM-2 全量规划 ===")
        planning_result = await planner.plan(
            user_query=user_query,
            session=session,
            intent=intent_result,
        )

        active_dims = [dim_id for dim_id, partners in planning_result.selected_partners.items() if partners]
        print(f"激活维度: {active_dims}")
        assert len(active_dims) >= 1, "Expected at least one dimension"

        # Stage 3: 执行
        print("\n=== Stage 3: Task Executor 执行 ===")
        execution_result = await executor.execute(
            session_id=session_id,
            active_task_id=active_task_id,
            planning_result=planning_result,
        )

        print(f"执行阶段: {execution_result.phase.value}")
        print(f"Partner 结果数: {len(execution_result.partner_results)}")

        # Stage 4: CompletionGate（如果需要）
        if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
            print("\n=== Stage 4: LLM-5 CompletionGate 评估 ===")

            partner_summaries = build_partner_summaries(execution_result, active_task_id)

            gate_result = await completion_gate.evaluate(
                partner_summaries=partner_summaries,
                user_constraints=None,
                scenario_id=scenario_id,
            )

            print(f"决策数: {len(gate_result.decisions)}")

            for decision in gate_result.decisions:
                endpoint = get_endpoint_for_partner(decision.partner_aic, execution_result)

                if decision.next_action == "complete":
                    task, error = await executor.complete_partner(
                        session_id=session_id,
                        partner_aic=decision.partner_aic,
                        aip_task_id=decision.aip_task_id,
                        endpoint=endpoint,
                    )
                    if task:
                        pr = execution_result.partner_results.get(decision.partner_aic)
                        if pr:
                            pr.state = task.status.state
                            if task.status.state == TaskState.Completed:
                                if decision.partner_aic in execution_result.awaiting_completion_partners:
                                    execution_result.awaiting_completion_partners.remove(decision.partner_aic)
                                if decision.partner_aic not in execution_result.completed_partners:
                                    execution_result.completed_partners.append(decision.partner_aic)

        # Stage 5: 结果整合
        print("\n=== Stage 5: LLM-6 Aggregator 结果整合 ===")

        partner_outputs = build_partner_outputs(execution_result)

        aggregation_result = await aggregator.aggregate(
            partner_outputs=partner_outputs,
            user_query=user_query,
            scenario_id=scenario_id,
        )

        print(f"结果类型: {aggregation_result.type}")
        print(f"文本长度: {len(aggregation_result.text)} 字符")

        assert aggregation_result.type == "final"
        assert len(aggregation_result.text) > 50

        print("\n✅ 多维度流程测试通过！")

    async def test_food_query_flow(
        self,
        intent_analyzer: IntentAnalyzer,
        planner: Planner,
        executor: TaskExecutor,
        completion_gate: CompletionGate,
        aggregator: Aggregator,
    ):
        """
        测试用例：餐饮查询流程

        测试单维度餐饮查询的完整流程
        """
        import uuid

        user_query = "推荐北京三里屯附近的川菜馆，适合朋友聚餐，人均100-150元"
        # 使用动态 ID 避免 Partner 缓存问题
        unique_suffix = uuid.uuid4().hex[:8]
        session_id = f"e2e-food-test-{unique_suffix}"
        active_task_id = f"e2e-task-food-{unique_suffix}"

        session = create_test_session(session_id)

        # Stage 1: 意图分析
        print("\n=== Stage 1: LLM-1 意图分析 ===")
        intent_result = await intent_analyzer.analyze(
            user_query=user_query,
            session=session,
        )

        print(f"意图类型: {intent_result.intent_type.value}")
        # 可能是 TASK_NEW 或其他
        if intent_result.intent_type != IntentType.TASK_NEW:
            pytest.skip(f"意图为 {intent_result.intent_type}，跳过后续流程")

        scenario_id = intent_result.target_scenario
        session.expert_scenario.id = scenario_id
        session.expert_scenario.kind = "expert"

        # Stage 2: 规划
        print("\n=== Stage 2: LLM-2 全量规划 ===")
        planning_result = await planner.plan(
            user_query=user_query,
            session=session,
            intent=intent_result,
        )

        active_dims = [dim_id for dim_id, partners in planning_result.selected_partners.items() if partners]
        print(f"激活维度: {active_dims}")

        partner_count = sum(len(partners) for partners in planning_result.selected_partners.values())

        if partner_count == 0:
            pytest.skip("未选中任何 Partner")

        # Stage 3: 执行
        print("\n=== Stage 3: Task Executor 执行 ===")
        execution_result = await executor.execute(
            session_id=session_id,
            active_task_id=active_task_id,
            planning_result=planning_result,
        )

        print(f"执行阶段: {execution_result.phase.value}")

        # Stage 4: CompletionGate（如果需要）
        if execution_result.phase == ExecutionPhase.AWAITING_COMPLETION:
            print("\n=== Stage 4: LLM-5 CompletionGate 评估 ===")

            partner_summaries = build_partner_summaries(execution_result, active_task_id)

            gate_result = await completion_gate.evaluate(
                partner_summaries=partner_summaries,
                user_constraints=None,
                scenario_id=scenario_id,
            )

            for decision in gate_result.decisions:
                endpoint = get_endpoint_for_partner(decision.partner_aic, execution_result)

                if decision.next_action == "complete":
                    task, _ = await executor.complete_partner(
                        session_id=session_id,
                        partner_aic=decision.partner_aic,
                        aip_task_id=decision.aip_task_id,
                        endpoint=endpoint,
                    )
                    if task:
                        pr = execution_result.partner_results.get(decision.partner_aic)
                        if pr:
                            pr.state = task.status.state

        # Stage 5: 结果整合
        print("\n=== Stage 5: LLM-6 Aggregator 结果整合 ===")

        partner_outputs = build_partner_outputs(execution_result)

        aggregation_result = await aggregator.aggregate(
            partner_outputs=partner_outputs,
            user_query=user_query,
            scenario_id=scenario_id,
        )

        print(f"结果类型: {aggregation_result.type}")
        print(f"文本长度: {len(aggregation_result.text)} 字符")

        assert aggregation_result.type == "final"
        assert len(aggregation_result.text) > 50

        print("\n✅ 餐饮查询流程测试通过！")
