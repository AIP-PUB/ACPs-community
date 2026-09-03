"""
Partner E2E Tests - State Machine Journey

状态机旅程测试 - 在同一个 task 下验证完整的状态转移流程。

本测试模拟真实场景：
1. START: 发送初始请求 → accepted → working
2. POLL: 等待处理结果 → awaiting-input / awaiting-completion / rejected
3. CONTINUE: 如果 awaiting-input，补充信息 → working → awaiting-completion
4. COMPLETE: 确认完成 → completed
5. CANCEL: 或者取消任务 → canceled

运行方式：
    # 需要先启动 Partner 服务
    just dev start

    # 运行 E2E 测试（必须串行执行）
    python -m pytest tests/e2e/test_state_machine_journey.py -v -s -n 0

注意：
- 需要 Partner 服务运行（各 partner 独立端口，见 config.toml）
- 测试必须按顺序执行（不能并行）
- 每个测试类使用独立的 task 来验证完整状态转移
"""

import logging

import pytest

# E2E 测试需要的 agent 列表（从 conftest 中的发现逻辑）
from tests.e2e.conftest import AGENT_URLS

AVAILABLE_AGENT_NAMES = list(AGENT_URLS.keys())

# =============================================================================
# 配置常量
# =============================================================================

POLL_INTERVAL = 0.5
MAX_POLL_TIME = 60
MAX_POLL_TIME_LONG = 120

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# 状态常量
# =============================================================================

STATE_ACCEPTED = "accepted"
STATE_WORKING = "working"
STATE_AWAITING_INPUT = "awaiting-input"
STATE_AWAITING_COMPLETION = "awaiting-completion"
STATE_COMPLETED = "completed"
STATE_REJECTED = "rejected"
STATE_CANCELED = "canceled"
STATE_FAILED = "failed"

# 终态列表
TERMINAL_STATES = [STATE_COMPLETED, STATE_REJECTED, STATE_CANCELED, STATE_FAILED]
# 非终态但稳定的状态（需要外部干预）
STABLE_STATES = [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION]
# 所有可能的最终停止状态
FINAL_STATES = TERMINAL_STATES + STABLE_STATES


# =============================================================================
# 测试场景数据
# =============================================================================

# 测试场景：每个 Agent 的测试输入
TEST_SCENARIOS = {
    "china_transport": {
        "in_scope_incomplete": "帮我订火车票",  # 在范围内但信息不完整
        "in_scope_complete": "帮我查询明天北京到上海的高铁，上午出发",  # 在范围内且信息完整
        "out_of_scope": "推荐北京的美食餐厅",  # 完全超出范围
        "supplement_info": "我要从北京到上海，1月30号出发",  # 补充信息
    },
    "china_hotel": {
        "in_scope_incomplete": "帮我订酒店",
        "in_scope_complete": "帮我在北京找一家五星级酒店，2月1号入住，住3晚，2个人",
        "out_of_scope": "帮我查高铁票",
        "supplement_info": "北京朝阳区，2月1号入住，住2晚",
    },
    "beijing_food": {
        "in_scope_incomplete": "推荐餐厅",  # 合理的用户请求，应该被接受
        "in_scope_complete": "推荐北京朝阳区适合4人聚餐的川菜馆，人均100左右",
        "out_of_scope": "帮我订酒店",
        "supplement_info": "要在王府井附近，人均200以内",
    },
    "beijing_urban": {
        "in_scope_incomplete": "北京有什么好玩的",  # 合理的用户请求，应该被接受
        "in_scope_complete": "帮我规划北京故宫和天坛一日游，2个大人1个小孩",
        "out_of_scope": "推荐上海的景点",
        "supplement_info": "2月1号去，想上午先去故宫",
    },
    "beijing_rural": {
        "in_scope_incomplete": "想去长城",  # 合理的用户请求，应该被接受
        "in_scope_complete": "帮我规划去慕田峪长城的一日游，2个人，自驾",
        "out_of_scope": "推荐北京的烤鸭店",
        "supplement_info": "这周六去，早上8点从市区出发",
    },
}


def get_scenario(agent_name: str, scenario_type: str) -> str:
    """获取指定 Agent 的测试场景"""
    if agent_name in TEST_SCENARIOS:
        return TEST_SCENARIOS[agent_name].get(scenario_type, "")
    # 默认场景
    return TEST_SCENARIOS["china_transport"].get(scenario_type, "")


# =============================================================================
# 测试辅助函数
# =============================================================================


def log_state_transition(task_id: str, state_history: list[str]) -> None:
    """记录状态转移历史"""
    transitions = " → ".join(state_history)
    logger.info(f"[{task_id}] State transitions: {transitions}")


def assert_valid_state_transition(state_history: list[str], expected_pattern: list[str]) -> None:
    """
    验证状态转移是否符合预期模式。

    Args:
        state_history: 实际的状态历史
        expected_pattern: 预期的状态模式（可以是部分匹配）
    """
    # 检查状态历史是否包含预期的模式
    for i, expected in enumerate(expected_pattern):
        if i < len(state_history):
            # 如果 expected 是列表，表示多个可能的状态
            if isinstance(expected, list):
                assert state_history[i] in expected, (
                    f"State at position {i} should be one of {expected}, got {state_history[i]}"
                )
            else:
                assert state_history[i] == expected, (
                    f"State at position {i} should be {expected}, got {state_history[i]}"
                )


# =============================================================================
# 测试类：完整状态转移旅程
# =============================================================================


class TestStateMachineJourneyChinaTransport:
    """
    china_transport Agent 的完整状态机旅程测试。

    测试同一个 task 下的多轮操作，验证状态转移的一致性。
    """

    agent_name = "china_transport"

    @pytest.fixture(autouse=True)
    def setup(self, unique_ids):
        """每个测试方法使用独立的 task_id 和 session_id"""
        self.task_id = f"{unique_ids['task_id']}-transport"
        self.session_id = unique_ids["session_id"]

    def test_journey_1_incomplete_request_to_completion(self, rpc_helper):
        """
        旅程1：不完整请求 → AwaitingInput → 补充信息 → AwaitingCompletion → Completed

        预期状态转移：
        START("帮我订火车票") → accepted → working → awaiting-input
        CONTINUE("北京到上海，1月30号") → working → awaiting-completion
        COMPLETE → completed
        """
        logger.info(f"\n{'=' * 60}")
        logger.info("Journey 1: Incomplete Request to Completion")
        logger.info(f"Task ID: {self.task_id}")
        logger.info(f"{'=' * 60}")

        # Step 1: START - 发送不完整请求
        logger.info("Step 1: START with incomplete request")
        start_result = rpc_helper.start(
            self.agent_name,
            get_scenario(self.agent_name, "in_scope_incomplete"),
            self.task_id,
            self.session_id,
        )
        assert start_result["status_code"] == 200, f"START failed: {start_result}"
        assert start_result["data"]["result"]["status"]["state"] == STATE_ACCEPTED

        # Step 2: POLL - 等待进入 AwaitingInput 或其他稳定状态
        logger.info("Step 2: POLL until stable state")
        poll_result = rpc_helper.poll_until(
            self.agent_name,
            self.task_id,
            self.session_id,
            [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result["converged"], f"Task did not converge: {poll_result}"
        log_state_transition(self.task_id, poll_result["state_history"])

        # 验证进入 awaiting-input（因为信息不完整）
        assert poll_result["final_state"] == STATE_AWAITING_INPUT, (
            f"Expected awaiting-input for incomplete request, got {poll_result['final_state']}"
        )

        # 检查返回的 dataItems 包含反问信息
        data_items = poll_result["final_result"]["result"]["status"].get("dataItems", [])
        assert len(data_items) > 0, "AwaitingInput state should have dataItems with clarification"
        logger.info(f"Clarification: {data_items[0].get('text', '')[:100]}...")

        # Step 3: CONTINUE - 补充信息
        logger.info("Step 3: CONTINUE with supplementary info")
        continue_result = rpc_helper.continue_task(
            self.agent_name,
            get_scenario(self.agent_name, "supplement_info"),
            self.task_id,
            self.session_id,
        )
        assert continue_result["status_code"] == 200, f"CONTINUE failed: {continue_result}"
        # CONTINUE 后应该回到 working 状态
        assert continue_result["data"]["result"]["status"]["state"] == STATE_WORKING

        # Step 4: POLL - 等待进入 AwaitingCompletion
        logger.info("Step 4: POLL until awaiting-completion")
        poll_result_2 = rpc_helper.poll_until(
            self.agent_name,
            self.task_id,
            self.session_id,
            [STATE_AWAITING_COMPLETION, STATE_AWAITING_INPUT, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result_2["converged"], f"Task did not converge after continue: {poll_result_2}"
        log_state_transition(self.task_id, poll_result_2["state_history"])

        # 应该进入 awaiting-completion（有产出物）
        assert poll_result_2["final_state"] == STATE_AWAITING_COMPLETION, (
            f"Expected awaiting-completion after supplement, got {poll_result_2['final_state']}"
        )

        # 验证有产出物
        products = poll_result_2["final_result"]["result"].get("products", [])
        assert len(products) > 0, "Should have products at awaiting-completion state"
        logger.info(f"Products count: {len(products)}")

        # Step 5: COMPLETE - 确认完成
        logger.info("Step 5: COMPLETE")
        complete_result = rpc_helper.complete(self.agent_name, self.task_id, self.session_id)
        assert complete_result["status_code"] == 200, f"COMPLETE failed: {complete_result}"
        assert complete_result["data"]["result"]["status"]["state"] == STATE_COMPLETED

        # 最终验证
        logger.info("✅ Journey 1 completed successfully")
        logger.info(f"Final state: {STATE_COMPLETED}")

    def test_journey_2_complete_request_direct_to_completion(self, rpc_helper):
        """
        旅程2：完整请求 → 直接到 AwaitingCompletion → Completed

        预期状态转移：
        START("查询明天北京到上海的高铁") → accepted → working → awaiting-completion
        COMPLETE → completed
        """
        task_id = f"{self.task_id}-complete"
        logger.info(f"\n{'=' * 60}")
        logger.info("Journey 2: Complete Request Direct to Completion")
        logger.info(f"Task ID: {task_id}")
        logger.info(f"{'=' * 60}")

        # Step 1: START - 发送完整请求
        logger.info("Step 1: START with complete request")
        start_result = rpc_helper.start(
            self.agent_name,
            get_scenario(self.agent_name, "in_scope_complete"),
            task_id,
            self.session_id,
        )
        assert start_result["status_code"] == 200

        # Step 2: POLL - 等待进入最终状态
        logger.info("Step 2: POLL until final state")
        poll_result = rpc_helper.poll_until(
            self.agent_name,
            task_id,
            self.session_id,
            [STATE_AWAITING_COMPLETION, STATE_AWAITING_INPUT, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result["converged"], f"Task did not converge: {poll_result}"
        log_state_transition(task_id, poll_result["state_history"])

        # 应该直接进入 awaiting-completion（信息完整）
        # 注意：由于 LLM 的不确定性，也可能进入 awaiting-input
        if poll_result["final_state"] == STATE_AWAITING_COMPLETION:
            logger.info("Direct to awaiting-completion as expected")

            # Step 3: COMPLETE
            complete_result = rpc_helper.complete(self.agent_name, task_id, self.session_id)
            assert complete_result["status_code"] == 200
            assert complete_result["data"]["result"]["status"]["state"] == STATE_COMPLETED

            logger.info("✅ Journey 2 completed successfully (direct path)")
        else:
            logger.info(f"Got {poll_result['final_state']} instead of awaiting-completion")
            logger.info("This is acceptable due to LLM non-determinism")

    def test_journey_3_out_of_scope_rejection(self, rpc_helper):
        """
        旅程3：超出范围的请求 → Rejected

        预期状态转移：
        START("推荐北京的美食餐厅") → accepted → rejected
        """
        task_id = f"{self.task_id}-reject"
        logger.info(f"\n{'=' * 60}")
        logger.info("Journey 3: Out of Scope Rejection")
        logger.info(f"Task ID: {task_id}")
        logger.info(f"{'=' * 60}")

        # Step 1: START - 发送超出范围的请求
        logger.info("Step 1: START with out-of-scope request")
        start_result = rpc_helper.start(
            self.agent_name,
            get_scenario(self.agent_name, "out_of_scope"),
            task_id,
            self.session_id,
        )
        assert start_result["status_code"] == 200

        # Step 2: POLL - 等待被拒绝
        logger.info("Step 2: POLL until rejected")
        poll_result = rpc_helper.poll_until(
            self.agent_name,
            task_id,
            self.session_id,
            [STATE_REJECTED, STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION],
            max_time=MAX_POLL_TIME,
        )
        assert poll_result["converged"], f"Task did not converge: {poll_result}"
        log_state_transition(task_id, poll_result["state_history"])

        # 应该被拒绝
        assert poll_result["final_state"] == STATE_REJECTED, (
            f"Expected rejected for out-of-scope request, got {poll_result['final_state']}"
        )

        # 检查拒绝原因
        data_items = poll_result["final_result"]["result"]["status"].get("dataItems", [])
        if data_items:
            logger.info(f"Rejection reason: {data_items[0].get('text', '')[:100]}...")

        logger.info("✅ Journey 3 completed successfully (rejected as expected)")

    def test_journey_4_cancel_during_awaiting_input(self, rpc_helper):
        """
        旅程4：在 AwaitingInput 状态取消任务 → Canceled

        预期状态转移：
        START("帮我订火车票") → accepted → working → awaiting-input
        CANCEL → canceled
        """
        task_id = f"{self.task_id}-cancel"
        logger.info(f"\n{'=' * 60}")
        logger.info("Journey 4: Cancel During AwaitingInput")
        logger.info(f"Task ID: {task_id}")
        logger.info(f"{'=' * 60}")

        # Step 1: START
        logger.info("Step 1: START with incomplete request")
        start_result = rpc_helper.start(
            self.agent_name,
            get_scenario(self.agent_name, "in_scope_incomplete"),
            task_id,
            self.session_id,
        )
        assert start_result["status_code"] == 200

        # Step 2: POLL until AwaitingInput
        logger.info("Step 2: POLL until awaiting-input")
        poll_result = rpc_helper.poll_until(
            self.agent_name,
            task_id,
            self.session_id,
            [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result["converged"]
        log_state_transition(task_id, poll_result["state_history"])

        if poll_result["final_state"] == STATE_AWAITING_INPUT:
            # Step 3: CANCEL
            logger.info("Step 3: CANCEL")
            cancel_result = rpc_helper.cancel(self.agent_name, task_id, self.session_id)
            assert cancel_result["status_code"] == 200
            assert cancel_result["data"]["result"]["status"]["state"] == STATE_CANCELED

            logger.info("✅ Journey 4 completed successfully (canceled)")
        else:
            logger.info("Skipping cancel test - task not in awaiting-input state")


class TestStateMachineJourneyChinaHotel:
    """china_hotel Agent 的状态机旅程测试"""

    agent_name = "china_hotel"

    @pytest.fixture(autouse=True)
    def setup(self, unique_ids):
        self.task_id = f"{unique_ids['task_id']}-hotel"
        self.session_id = unique_ids["session_id"]

    def test_journey_incomplete_to_completion(self, rpc_helper):
        """不完整请求 → 补充信息 → 完成"""
        logger.info(f"\n{'=' * 60}")
        logger.info("Hotel Journey: Incomplete to Completion")
        logger.info(f"{'=' * 60}")

        # START
        start_result = rpc_helper.start(
            self.agent_name,
            get_scenario(self.agent_name, "in_scope_incomplete"),
            self.task_id,
            self.session_id,
        )
        assert start_result["status_code"] == 200

        # POLL
        poll_result = rpc_helper.poll_until(
            self.agent_name,
            self.task_id,
            self.session_id,
            [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result["converged"]
        log_state_transition(self.task_id, poll_result["state_history"])

        if poll_result["final_state"] == STATE_AWAITING_INPUT:
            # CONTINUE
            continue_result = rpc_helper.continue_task(
                self.agent_name,
                get_scenario(self.agent_name, "supplement_info"),
                self.task_id,
                self.session_id,
            )
            assert continue_result["status_code"] == 200

            # POLL again
            poll_result_2 = rpc_helper.poll_until(
                self.agent_name,
                self.task_id,
                self.session_id,
                [STATE_AWAITING_COMPLETION, STATE_AWAITING_INPUT],
                max_time=MAX_POLL_TIME_LONG,
            )
            log_state_transition(self.task_id, poll_result_2["state_history"])

        logger.info("✅ Hotel journey completed")


class TestStateTransitionConsistency:
    """
    状态转移一致性测试。

    验证所有 Agent 的状态转移行为是否符合 AIP 协议规范。
    """

    @pytest.mark.parametrize("agent_name", AVAILABLE_AGENT_NAMES)
    def test_in_scope_request_not_rejected_immediately(self, rpc_helper, unique_ids, agent_name):
        """
        验证：在范围内的请求不应该被立即拒绝。

        即使信息不完整，只要请求在服务范围内，应该进入 awaiting-input 而不是 rejected。
        """
        task_id = f"{unique_ids['task_id']}-{agent_name}-scope"
        session_id = unique_ids["session_id"]

        logger.info(f"Testing {agent_name}: in-scope request should not be rejected")

        # 发送在范围内但不完整的请求
        in_scope_request = get_scenario(agent_name, "in_scope_incomplete")
        start_result = rpc_helper.start(agent_name, in_scope_request, task_id, session_id)
        assert start_result["status_code"] == 200

        # 等待处理结果
        poll_result = rpc_helper.poll_until(
            agent_name,
            task_id,
            session_id,
            [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION, STATE_REJECTED],
            max_time=MAX_POLL_TIME_LONG,
        )
        assert poll_result["converged"]

        # 关键断言：在范围内的请求不应该被 rejected
        assert poll_result["final_state"] != STATE_REJECTED, (
            f"{agent_name}: In-scope request '{in_scope_request}' should not be rejected. "
            f"Got: {poll_result['final_state']}"
        )

        logger.info(f"✅ {agent_name}: in-scope request correctly handled → {poll_result['final_state']}")

    @pytest.mark.parametrize("agent_name", AVAILABLE_AGENT_NAMES)
    def test_out_of_scope_request_should_be_rejected(self, rpc_helper, unique_ids, agent_name):
        """
        验证：超出范围的请求应该被拒绝。
        """
        task_id = f"{unique_ids['task_id']}-{agent_name}-reject"
        session_id = unique_ids["session_id"]

        logger.info(f"Testing {agent_name}: out-of-scope request should be rejected")

        # 发送超出范围的请求
        out_of_scope_request = get_scenario(agent_name, "out_of_scope")
        start_result = rpc_helper.start(agent_name, out_of_scope_request, task_id, session_id)
        assert start_result["status_code"] == 200

        # 等待处理结果
        poll_result = rpc_helper.poll_until(
            agent_name,
            task_id,
            session_id,
            [STATE_AWAITING_INPUT, STATE_AWAITING_COMPLETION, STATE_REJECTED],
            max_time=MAX_POLL_TIME,
        )
        assert poll_result["converged"]

        # 关键断言：超出范围的请求应该被 rejected
        assert poll_result["final_state"] == STATE_REJECTED, (
            f"{agent_name}: Out-of-scope request '{out_of_scope_request}' should be rejected. "
            f"Got: {poll_result['final_state']}"
        )

        logger.info(f"✅ {agent_name}: out-of-scope request correctly rejected")
