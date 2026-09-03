"""
Leader Agent Platform - 集成测试：LLM-4 增量更新流程

测试从用户补充输入到 Partner continue 的完整流程，验证：
1. InputRouter 正确调用 LLM-4 提取字段值
2. 字段值正确路由到对应 Partner
3. 完整性验证和反问闭环
4. 与 Orchestrator 的集成
5. 降级场景的处理

本测试使用 Mock 避免实际 LLM API 调用。
"""

import json
from datetime import UTC
from typing import Any
from unittest.mock import MagicMock

import pytest
from acps_sdk.aip.aip_base_model import TaskState
from assistant.core.clarification_merger import ClarificationMerger
from assistant.core.input_router import (
    InputRouter,
    reset_input_router,
)
from assistant.core.orchestrator import Orchestrator
from assistant.models.aip import AipTaskSnapshot, TaskStatusSnapshot
from assistant.models.base import IntentType
from assistant.models.clarification import (
    ClarificationMergeInput,
    PartnerClarificationItem,
    RequiredField,
)
from assistant.models.input_routing import (
    InputRoutingRequest,
    PartnerGapInfo,
)
from assistant.models.intent import IntentDecision, TaskInstruction
from assistant.models.session import Session

pytest_plugins = ("pytest_asyncio",)


# =============================================================================
# 测试辅助函数
# =============================================================================


def create_mock_task(
    task_id: str,
    state: TaskState,
    data_items: list[dict[str, Any]],
) -> AipTaskSnapshot:
    """创建模拟的 AIP Task 快照对象。"""
    return AipTaskSnapshot(
        id=task_id,
        session_id="test-session",
        status=TaskStatusSnapshot(
            state=state,
            state_changed_at="2024-01-01T00:00:00Z",
            data_items=data_items,
        ),
    )


def create_sample_partner_gaps() -> list[PartnerGapInfo]:
    """创建示例 Partner 缺口列表。"""
    return [
        PartnerGapInfo(
            partner_aic="partner-hotel-001",
            partner_name="酒店服务",
            dimension_id="hotel",
            aip_task_id="task-hotel-001",
            awaiting_fields=[
                RequiredField(
                    field_name="budget",
                    field_label="预算",
                    field_type="number",
                    required=True,
                    constraints={"min": 100, "max": 10000},
                ),
                RequiredField(
                    field_name="check_in_date",
                    field_label="入住日期",
                    field_type="date",
                    required=True,
                ),
            ],
            question_text="请告诉我您的预算和入住日期",
        ),
        PartnerGapInfo(
            partner_aic="partner-transport-002",
            partner_name="交通服务",
            dimension_id="transport",
            aip_task_id="task-transport-002",
            awaiting_fields=[
                RequiredField(
                    field_name="travelers",
                    field_label="出行人数",
                    field_type="number",
                    required=True,
                ),
                RequiredField(
                    field_name="check_in_date",
                    field_label="出发日期",
                    field_type="date",
                    required=True,
                ),
            ],
            question_text="请告诉我出行人数和出发日期",
        ),
    ]


def create_mock_session(
    session_id: str = "test-session-001",
    with_active_task: bool = True,
    with_awaiting_partners: bool = True,
    user_context: dict[str, Any] = None,
) -> Session:
    """创建模拟的 Session 对象。"""
    from datetime import datetime, timedelta

    from assistant.models import (
        ExecutionMode,
        ScenarioRuntime,
        UserResult,
        UserResultType,
    )
    from assistant.models.base import now_iso

    now = now_iso()
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    base_scenario = ScenarioRuntime(id="base", kind="base", version="1.0.0", loaded_at=now)
    expert_scenario = ScenarioRuntime(id="tour", kind="expert", version="1.0.0", loaded_at=now)

    session = Session(
        session_id=session_id,
        mode=ExecutionMode.DIRECT_RPC,
        created_at=now,
        updated_at=now,
        touched_at=now,
        ttl_seconds=3600,
        expires_at=expires_at,
        base_scenario=base_scenario,
        expert_scenario=expert_scenario,
        user_result=UserResult(
            type=UserResultType.PENDING,
            data_items=[],
            updated_at=now,
        ),
        user_context=user_context or {},
    )

    if with_active_task:
        # 创建 ActiveTask
        from assistant.models.task import ActiveTask, PartnerTask

        partner_tasks = {}
        if with_awaiting_partners:
            # 创建 AwaitingInput 状态的 Partner 任务
            for gap in create_sample_partner_gaps():
                data_items = [
                    {"type": "text", "text": gap.question_text},
                    {
                        "type": "data",
                        "data": {
                            "requiredFields": [
                                {
                                    "name": f.field_name,
                                    "label": f.field_label,
                                    "type": f.field_type,
                                    "required": f.required,
                                    "constraints": f.constraints,
                                }
                                for f in gap.awaiting_fields
                            ]
                        },
                    },
                ]
                task = create_mock_task(
                    task_id=gap.aip_task_id,
                    state=TaskState.AwaitingInput,
                    data_items=data_items,
                )
                partner_tasks[gap.partner_aic] = PartnerTask(
                    partner_aic=gap.partner_aic,
                    dimensions=[gap.dimension_id],
                    aip_task_id=gap.aip_task_id,
                    state=TaskState.AwaitingInput,
                    last_snapshot=task,
                )

        from assistant.models.base import ActiveTaskStatus

        active_task = ActiveTask(
            active_task_id="active-task-001",
            created_at=now,
            external_status=ActiveTaskStatus.AWAITING_INPUT,
            partner_tasks=partner_tasks,
        )
        session.active_task = active_task

    return session


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_llm_client():
    """Mock LLM 客户端"""
    client = MagicMock()
    # InputRouter 调用的是 call 方法，而不是 invoke
    client.call = MagicMock(
        return_value=json.dumps(
            {
                "extractedValues": {
                    "budget": 500,
                    "check_in_date": "2024-12-25",
                    "travelers": 2,
                },
                "analysis": {"confident": True, "notes": "所有字段都已提取"},
            }
        )
    )
    return client


@pytest.fixture
def mock_scenario_loader():
    """Mock 场景加载器"""
    loader = MagicMock()
    loader.get_merged_prompts = MagicMock(
        return_value={
            "input_routing": {
                "system": "你是输入路由器。{{input_json}}",
            },
            "clarification": {
                "system_prompt": "你是澄清生成器。",
                "user_prompt": "{input_json}",
            },
        }
    )
    return loader


@pytest.fixture
def router(mock_llm_client, mock_scenario_loader):
    """创建 InputRouter 实例"""
    return InputRouter(
        llm_client=mock_llm_client,
        scenario_loader=mock_scenario_loader,
    )


@pytest.fixture
def clarification_merger(mock_llm_client, mock_scenario_loader):
    """创建 ClarificationMerger 实例"""
    return ClarificationMerger(
        llm_client=mock_llm_client,
        scenario_loader=mock_scenario_loader,
    )


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理"""
    yield
    reset_input_router()


# =============================================================================
# InputRouter 集成测试
# =============================================================================


class TestInputRouterIntegration:
    """测试 InputRouter 的集成功能"""

    @pytest.mark.asyncio
    async def test_route_complete_answer(self, router: InputRouter):
        """完整回答的路由流程"""
        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="预算500元，2人，12月25日入住",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
            scenario_id="tour",
        )

        result = await router.route(request)

        # 验证结果
        assert result.is_sufficient is True
        assert len(result.patches_by_partner) == 2
        assert "partner-hotel-001" in result.patches_by_partner
        assert "partner-transport-002" in result.patches_by_partner

        # 验证酒店补丁
        hotel_patch = result.patches_by_partner["partner-hotel-001"]
        assert hotel_patch.patch_data.get("budget") == 500
        assert hotel_patch.patch_data.get("check_in_date") == "2024-12-25"

        # 验证交通补丁
        transport_patch = result.patches_by_partner["partner-transport-002"]
        assert transport_patch.patch_data.get("travelers") == 2
        assert transport_patch.patch_data.get("check_in_date") == "2024-12-25"

    @pytest.mark.asyncio
    async def test_route_partial_answer(self, router: InputRouter):
        """部分回答的路由流程"""
        # 设置 LLM 返回部分提取
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"budget": 500},
                    "analysis": {"confident": True},
                }
            )
        )

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        result = await router.route(request)

        # 验证结果
        assert result.is_sufficient is False
        assert len(result.missing_fields) > 0
        # 只有酒店有补丁
        assert "partner-hotel-001" in result.patches_by_partner
        # 交通没有补丁（因为 budget 不是它需要的字段）
        assert "partner-transport-002" not in result.patches_by_partner

    @pytest.mark.asyncio
    async def test_route_with_user_context_autofill(self, router: InputRouter):
        """userContext 自动填充"""
        # 设置 LLM 只返回 budget
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"budget": 500},
                    "analysis": {"confident": True},
                }
            )
        )

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
            user_context={
                "check_in_date": "2024-12-25",
                "travelers": 2,
            },
        )

        result = await router.route(request)

        # 由于 userContext 补全了其他字段，应该是充分的
        assert result.is_sufficient is True


# =============================================================================
# 反问闭环集成测试
# =============================================================================


class TestClarificationLoopIntegration:
    """测试不充分输入时的反问闭环"""

    @pytest.mark.asyncio
    async def test_insufficient_triggers_clarification(
        self, router: InputRouter, clarification_merger: ClarificationMerger
    ):
        """不充分的输入应触发反问"""
        # 设置 LLM 只返回部分字段
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"budget": 500},
                    "analysis": {"confident": True},
                }
            )
        )

        # 设置 clarification LLM 返回
        clarification_merger._llm_client.invoke = MagicMock(return_value="还需要您提供入住日期和出行人数，请告诉我。")

        partner_gaps = create_sample_partner_gaps()

        # 第一步：路由
        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        routing_result = await router.route(request)
        assert routing_result.is_sufficient is False

        # 第二步：生成反问
        remaining_items = [
            PartnerClarificationItem(
                partner_aic=gap.partner_aic,
                partner_name=gap.partner_name,
                dimension_id=gap.dimension_id,
                aip_task_id=gap.aip_task_id,
                question_text=gap.question_text,
                required_fields=[
                    f
                    for f in gap.awaiting_fields
                    if any(mf.field_name == f.field_name for mf in routing_result.missing_fields)
                ],
            )
            for gap in partner_gaps
        ]
        remaining_items = [item for item in remaining_items if item.required_fields]

        merge_input = ClarificationMergeInput(
            partner_items=remaining_items,
            user_query="预算500元",
        )

        clarification = await clarification_merger.merge(merge_input)

        # 验证生成了反问
        assert clarification.question_text
        assert len(clarification.merged_fields) > 0


# =============================================================================
# Orchestrator 集成测试（Mock 版本）
# =============================================================================


class TestOrchestratorIntegration:
    """测试 Orchestrator 的 TASK_INPUT 处理"""

    @pytest.mark.asyncio
    async def test_handle_task_input_sufficient(self, router: InputRouter, mock_scenario_loader):
        """测试充分输入的处理"""
        # 创建模拟的 Session
        session = create_mock_session(
            user_context={
                "awaiting_partner_gaps": [gap.model_dump(by_alias=True) for gap in create_sample_partner_gaps()]
            }
        )

        # 创建 mock 依赖
        mock_session_manager = MagicMock()
        mock_session_manager.get_session = MagicMock(return_value=session)
        mock_session_manager.add_dialog_turn = MagicMock()
        mock_session_manager.add_event_log = MagicMock()
        mock_session_manager.update_session = MagicMock()

        mock_intent_analyzer = MagicMock()

        # 创建 Orchestrator
        orchestrator = Orchestrator(
            session_manager=mock_session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=mock_intent_analyzer,
            input_router=router,
        )

        # 模拟意图决策
        intent = IntentDecision(
            intent_type=IntentType.TASK_INPUT,
            task_instruction=TaskInstruction(text="预算500元，2人，12月25日"),
        )

        # 执行（不需要 executor）
        response = await orchestrator._handle_task_input(
            session=session,
            intent=intent,
            user_query="预算500元，2人，12月25日",
        )

        # 验证响应（没有 executor 时应该返回成功但 result 为 None）
        # 由于没有配置 executor，会返回正常响应但不执行
        assert response.error is None or response.result is not None

    @pytest.mark.asyncio
    async def test_handle_task_input_supports_sdk_camel_case_status(self, router: InputRouter, mock_scenario_loader):
        """运行态 SDK TaskStatus 使用 dataItems，需兼容缓存快照的 data_items。"""
        from acps_sdk.aip.aip_base_model import TaskStatus

        session = create_mock_session(
            user_context={
                "awaiting_partner_gaps": [gap.model_dump(by_alias=True) for gap in create_sample_partner_gaps()]
            }
        )
        assert session.active_task is not None
        first_task = next(iter(session.active_task.partner_tasks.values()))
        assert first_task.last_snapshot is not None
        object.__setattr__(
            first_task.last_snapshot,
            "status",
            TaskStatus(state=TaskState.AwaitingInput, stateChangedAt="2024-01-01T00:00:00Z", dataItems=[]),
        )

        session_manager = MagicMock()
        session_manager.get_session.return_value = session
        orchestrator = Orchestrator(
            session_manager=session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=MagicMock(),
            input_router=router,
        )

        response = await orchestrator._handle_task_input(
            session=session,
            intent=IntentDecision(
                intent_type=IntentType.TASK_INPUT,
                task_instruction=TaskInstruction(text="预算500元，2人，12月25日"),
            ),
            user_query="预算500元，2人，12月25日",
        )

        assert response.error is None or response.result is not None

    @pytest.mark.asyncio
    async def test_handle_task_input_no_active_task(self, router: InputRouter, mock_scenario_loader):
        """测试没有活跃任务时的处理"""
        # 创建没有活跃任务的 Session
        session = create_mock_session(with_active_task=False)

        mock_session_manager = MagicMock()
        mock_session_manager.get_session = MagicMock(return_value=session)

        mock_intent_analyzer = MagicMock()

        orchestrator = Orchestrator(
            session_manager=mock_session_manager,
            scenario_loader=mock_scenario_loader,
            intent_analyzer=mock_intent_analyzer,
            input_router=router,
        )

        intent = IntentDecision(
            intent_type=IntentType.TASK_INPUT,
            task_instruction=TaskInstruction(text="预算500元"),
        )

        response = await orchestrator._handle_task_input(
            session=session,
            intent=intent,
            user_query="预算500元",
        )

        # 应该返回错误
        assert response.error is not None
        assert "没有活跃任务" in response.error.message


# =============================================================================
# 字段映射测试
# =============================================================================


class TestFieldMappingIntegration:
    """测试多 Partner 共享字段的映射"""

    @pytest.mark.asyncio
    async def test_shared_field_routing(self, router: InputRouter):
        """测试共享字段（如 check_in_date）正确路由到所有需要的 Partner"""
        # 设置 LLM 只返回共享字段
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"check_in_date": "2024-12-25"},
                    "analysis": {"confident": True},
                }
            )
        )

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="12月25日出发",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        result = await router.route(request)

        # check_in_date 被两个 Partner 共享
        assert "partner-hotel-001" in result.patches_by_partner
        assert "partner-transport-002" in result.patches_by_partner

        # 两个 Partner 都应该收到日期
        hotel_patch = result.patches_by_partner["partner-hotel-001"]
        transport_patch = result.patches_by_partner["partner-transport-002"]

        assert hotel_patch.patch_data.get("check_in_date") == "2024-12-25"
        assert transport_patch.patch_data.get("check_in_date") == "2024-12-25"


# =============================================================================
# 降级场景测试
# =============================================================================


class TestFallbackScenarios:
    """测试降级场景"""

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self, router: InputRouter):
        """LLM 调用失败时使用规则匹配降级"""
        router._llm_client.call = MagicMock(side_effect=Exception("LLM API Error"))

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        # 不应该抛出异常
        result = await router.route(request)

        # 应该使用降级提取
        assert result is not None
        # 规则匹配应该能提取到 budget
        if "partner-hotel-001" in result.patches_by_partner:
            patch = result.patches_by_partner["partner-hotel-001"]
            assert patch.patch_data.get("budget") == 500.0

    @pytest.mark.asyncio
    async def test_invalid_llm_response_fallback(self, router: InputRouter):
        """LLM 返回无效响应时使用降级"""
        router._llm_client.call = MagicMock(return_value="这不是有效的 JSON")

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="预算500元，2人",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        result = await router.route(request)

        # 应该用规则匹配降级
        assert result is not None


# =============================================================================
# 边界情况测试
# =============================================================================


class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_empty_partner_gaps(self, router: InputRouter):
        """空 Partner 缺口"""
        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=[],
            active_task_id="active-001",
        )

        result = await router.route(request)

        assert result.is_sufficient is True
        assert result.patches_by_partner == {}

    @pytest.mark.asyncio
    async def test_no_matching_fields(self, router: InputRouter):
        """用户输入不匹配任何字段"""
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {},
                    "analysis": {"confident": True, "notes": "无法提取字段"},
                }
            )
        )

        partner_gaps = create_sample_partner_gaps()

        request = InputRoutingRequest(
            user_input="你好，帮我查一下",  # 不包含任何字段信息
            partner_gaps=partner_gaps,
            active_task_id="active-001",
        )

        result = await router.route(request)

        assert result.is_sufficient is False
        assert len(result.missing_fields) > 0
        assert result.patches_by_partner == {}

    @pytest.mark.asyncio
    async def test_partial_fields_with_optional(self, router: InputRouter):
        """部分必填字段 + 可选字段"""
        # 设置一个包含可选字段的 gap
        gaps = [
            PartnerGapInfo(
                partner_aic="partner-001",
                dimension_id="hotel",
                aip_task_id="task-001",
                awaiting_fields=[
                    RequiredField(
                        field_name="budget",
                        field_label="预算",
                        field_type="number",
                        required=True,
                    ),
                    RequiredField(
                        field_name="notes",
                        field_label="备注",
                        field_type="string",
                        required=False,
                    ),
                ],
            )
        ]

        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"budget": 500},
                    "analysis": {"confident": True},
                }
            )
        )

        request = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=gaps,
            active_task_id="active-001",
        )

        result = await router.route(request)

        # 必填字段已填，可选字段未填，应该是充分的
        assert result.is_sufficient is True


# =============================================================================
# 多轮交互模拟测试
# =============================================================================


class TestMultiTurnInteraction:
    """测试多轮交互场景"""

    @pytest.mark.asyncio
    async def test_incremental_field_filling(self, router: InputRouter):
        """模拟分步填充字段"""
        partner_gaps = create_sample_partner_gaps()

        # 第一轮：只提供预算
        router._llm_client.call = MagicMock(
            return_value=json.dumps({"extractedValues": {"budget": 500}, "analysis": {"confident": True}})
        )

        request1 = InputRoutingRequest(
            user_input="预算500元",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
            user_context={},
        )

        result1 = await router.route(request1)
        assert result1.is_sufficient is False

        # 记录已填充的字段到 user_context
        filled_context = {"budget": 500}

        # 第二轮：提供日期和人数，加上之前的 context
        router._llm_client.call = MagicMock(
            return_value=json.dumps(
                {
                    "extractedValues": {"check_in_date": "2024-12-25", "travelers": 2},
                    "analysis": {"confident": True},
                }
            )
        )

        request2 = InputRoutingRequest(
            user_input="12月25日，2人",
            partner_gaps=partner_gaps,
            active_task_id="active-001",
            user_context=filled_context,  # 携带之前填充的值
        )

        result2 = await router.route(request2)

        # 现在应该充分了
        assert result2.is_sufficient is True
        assert len(result2.patches_by_partner) == 2
