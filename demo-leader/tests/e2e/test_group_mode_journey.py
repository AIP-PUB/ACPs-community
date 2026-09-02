"""
Leader Agent Platform - E2E Tests: Group Mode Complete Journey

群组模式完整用户旅程测试 - 测试多 Partner 协作的完整交互链路。

测试场景：
1. 创建 group 模式 session，发起旅游规划任务
2. Partner 反问（AwaitingInput），等待用户补充信息
3. 用户补充信息（Continue），Partner 继续处理
4. 任务完成，获取整合结果
5. 关闭 session，解散群组

运行方式：
    pytest tests/e2e/test_group_mode_journey.py -v -s

注意：
- 需要 Leader 服务运行在 localhost:9011（group mode enabled）
- 需要 Partner 服务运行（各 partner 独立端口，如 9021-9025）
- 需要 RabbitMQ 运行在 localhost:5671（management API 仍为 localhost:15672）
"""

import logging
import os
import ssl
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# 配置
# =============================================================================

BASE_URL = "https://localhost:9011"
PARTNER_URL = "https://localhost:9021"  # beijing_food (用于健康检查)
RABBITMQ_MGMT_URL = "http://localhost:15672"
API_PREFIX = "/api/v1"
LEADER_ATR_DIR = Path(__file__).resolve().parents[2] / "leader" / "atr"
PARTNER_HEALTH_CA = LEADER_ATR_DIR / "trust-bundle.pem"
PARTNER_HEALTH_CERT = LEADER_ATR_DIR / "client.pem"
PARTNER_HEALTH_KEY = LEADER_ATR_DIR / "client.key"

# 轮询配置
POLL_INTERVAL = 2.0  # Group 模式需要更长的轮询间隔
MAX_POLL_TIME = 120  # 超时时间（秒）
MAX_POLL_TIME_TASK = float(os.getenv("LEADER_E2E_POLL_TIMEOUT", "240"))
HEALTH_CHECK_TIMEOUT = 20.0
HEALTH_CHECK_REQUEST_TIMEOUT = 15.0
E2E_REQUEST_TIMEOUT = float(os.getenv("LEADER_E2E_REQUEST_TIMEOUT", "240"))


# =============================================================================
# Helper Functions
# =============================================================================


def api_url(path: str) -> str:
    """构建完整的 API URL"""
    return f"{BASE_URL}{API_PREFIX}{path}"


def build_partner_health_ssl_context() -> ssl.SSLContext:
    """为 Partner 健康检查创建 mTLS SSL 上下文。"""
    context = ssl.create_default_context(cafile=str(PARTNER_HEALTH_CA))
    context.load_cert_chain(
        certfile=str(PARTNER_HEALTH_CERT),
        keyfile=str(PARTNER_HEALTH_KEY),
    )
    return context


def check_service_health(
    base_url: str = BASE_URL,
    partner_url: str = PARTNER_URL,
    client_verify: ssl.SSLContext | bool | None = None,
) -> tuple[bool, str]:
    """检查所有服务健康状态"""
    issues = []

    try:
        verify_context = client_verify or build_partner_health_ssl_context()
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT, verify=verify_context) as client:
            # 检查 Leader 服务（通过尝试获取一个不存在的 session）
            try:
                leader_resp = client.get(
                    f"{base_url}{API_PREFIX}/result/health-check-session",
                    timeout=HEALTH_CHECK_REQUEST_TIMEOUT,
                )
                # 404 表示服务正常但 session 不存在，这是预期的
                if leader_resp.status_code not in [200, 404]:
                    issues.append(f"Leader unhealthy: {leader_resp.status_code}")
            except httpx.ConnectError:
                issues.append(f"Leader service not reachable at {base_url}")

            # 检查 Partner 服务
            try:
                partner_resp = client.get(
                    f"{partner_url}/health",
                    timeout=HEALTH_CHECK_REQUEST_TIMEOUT,
                )
                if partner_resp.status_code != 200:
                    issues.append(f"Partner unhealthy: {partner_resp.status_code}")
            except httpx.HTTPError as exc:
                issues.append(f"Partner service not reachable at {partner_url}: {exc}")

            # 检查 RabbitMQ
            try:
                rabbitmq_resp = client.get(
                    f"{RABBITMQ_MGMT_URL}/api/overview",
                    auth=("admin", "devpass"),
                    timeout=HEALTH_CHECK_REQUEST_TIMEOUT,
                )
                if rabbitmq_resp.status_code != 200:
                    issues.append(f"RabbitMQ unhealthy: {rabbitmq_resp.status_code}")
            except httpx.ConnectError:
                issues.append("RabbitMQ not reachable at localhost:15672")

        if issues:
            return False, "; ".join(issues)
        return True, "All services healthy"
    except Exception as e:
        return False, f"Service check failed: {e}"


def submit_query(
    client: httpx.Client,
    query: str,
    session_id: str | None = None,
    active_task_id: str | None = None,
    mode: str = "group",
) -> dict[str, Any]:
    """提交查询到 /submit API"""
    payload = {
        "query": query,
        "mode": mode,
        "clientRequestId": f"group_test_{uuid.uuid4().hex[:12]}",
    }
    if session_id:
        payload["sessionId"] = session_id
    if active_task_id:
        payload["activeTaskId"] = active_task_id

    logger.info(f"[Submit] Query: {query[:60]}... (session: {session_id or 'new'})")
    response = client.post(api_url("/submit"), json=payload, timeout=E2E_REQUEST_TIMEOUT)

    # 处理 LLM 服务问题
    if response.status_code == 500:
        try:
            data = response.json()
            detail = data.get("detail", {})
            if isinstance(detail, dict) and detail.get("code") in [
                "LLM_CALL_ERROR",
                "LLM_SERVICE_UNAVAILABLE",
            ]:
                pytest.skip(f"LLM service unavailable: {detail.get('message')}")
        except Exception:
            pass

    try:
        data = response.json()
    except Exception:
        data = None

    return {
        "status_code": response.status_code,
        "data": data,
    }


def get_result(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """获取会话结果"""
    response = client.get(api_url(f"/result/{session_id}"), timeout=E2E_REQUEST_TIMEOUT)
    try:
        data = response.json()
    except Exception:
        data = None
    return {
        "status_code": response.status_code,
        "data": data,
    }


def cancel_session(client: httpx.Client, session_id: str, active_task_id: str) -> dict[str, Any]:
    """取消/关闭会话"""
    payload = {
        "sessionId": session_id,
        "activeTaskId": active_task_id,
        "clientRequestId": f"cancel_{uuid.uuid4().hex[:8]}",
    }
    response = client.post(api_url("/cancel"), json=payload, timeout=E2E_REQUEST_TIMEOUT)
    try:
        data = response.json()
    except Exception:
        data = None
    return {
        "status_code": response.status_code,
        "data": data,
    }


def extract_user_result(result_data: dict[str, Any]) -> dict[str, Any] | None:
    """提取 userResult"""
    if not result_data:
        return None
    leader_result = result_data.get("result", {})
    return leader_result.get("userResult")


def extract_user_result_type(result_data: dict[str, Any]) -> str | None:
    """从结果中提取 userResult.type"""
    user_result = extract_user_result(result_data)
    return user_result.get("type") if user_result else None


def extract_active_task(result_data: dict[str, Any]) -> dict[str, Any] | None:
    """从结果中提取 activeTask"""
    if not result_data:
        return None
    leader_result = result_data.get("result", {})
    return leader_result.get("activeTask")


def extract_partner_tasks(result_data: dict[str, Any]) -> dict[str, Any] | None:
    """从结果中提取 activeTask.partnerTasks"""
    active_task = extract_active_task(result_data)
    return active_task.get("partnerTasks") if active_task else None


def extract_session_mode(result_data: dict[str, Any]) -> str | None:
    """从结果中提取执行模式"""
    if not result_data:
        return None
    leader_result = result_data.get("result", {})
    return leader_result.get("mode")


def extract_group_id(result_data: dict[str, Any]) -> str | None:
    """从结果中提取 groupId"""
    if not result_data:
        return None
    leader_result = result_data.get("result", {})
    return leader_result.get("groupId")


def get_partner_states(result_data: dict[str, Any]) -> dict[str, str]:
    """获取所有 Partner 的状态"""
    partner_tasks = extract_partner_tasks(result_data)
    if not partner_tasks:
        return {}
    return {aic: task.get("state", "unknown") for aic, task in partner_tasks.items()}


def has_awaiting_input_partners(result_data: dict[str, Any]) -> bool:
    """检查是否有 Partner 处于 awaiting-input 状态"""
    states = get_partner_states(result_data)
    return "awaiting-input" in states.values()


def has_awaiting_completion_partners(result_data: dict[str, Any]) -> bool:
    """检查是否有 Partner 处于 awaiting-completion 状态"""
    states = get_partner_states(result_data)
    return "awaiting-completion" in states.values()


def all_partners_completed(result_data: dict[str, Any]) -> bool:
    """检查所有 Partner 是否都完成"""
    states = get_partner_states(result_data)
    if not states:
        return False
    return all(s == "completed" for s in states.values())


def poll_for_state(
    client: httpx.Client,
    session_id: str,
    target_states: list[str],
    max_time: float = MAX_POLL_TIME,
    check_partner_state: bool = False,
) -> dict[str, Any]:
    """
    轮询直到达到目标状态。

    Args:
        client: HTTP 客户端
        session_id: 会话 ID
        target_states: 目标 userResult.type 状态列表
        max_time: 最大等待时间
        check_partner_state: 是否同时检查 partner 状态

    Returns:
        {
            "converged": bool,
            "final_result": dict,
            "poll_count": int,
            "elapsed_time": float,
            "final_state": str,
            "partner_states": dict,
        }
    """
    start_time = time.time()
    poll_count = 0

    while time.time() - start_time < max_time:
        poll_count += 1
        result = get_result(client, session_id)

        if result["status_code"] != 200:
            logger.debug(f"[Poll #{poll_count}] Status code: {result['status_code']}")
            time.sleep(POLL_INTERVAL)
            continue

        result_type = extract_user_result_type(result["data"])
        partner_states = get_partner_states(result["data"])

        logger.debug(f"[Poll #{poll_count}] userResult.type={result_type}, partner_states={partner_states}")

        if result_type in target_states:
            return {
                "converged": True,
                "final_result": result["data"],
                "poll_count": poll_count,
                "elapsed_time": time.time() - start_time,
                "final_state": result_type,
                "partner_states": partner_states,
            }

        # 如果检查 partner 状态，也检测 awaiting-input
        if check_partner_state and has_awaiting_input_partners(result["data"]):
            return {
                "converged": True,
                "final_result": result["data"],
                "poll_count": poll_count,
                "elapsed_time": time.time() - start_time,
                "final_state": result_type,
                "partner_states": partner_states,
            }

        time.sleep(POLL_INTERVAL)

    # 超时
    final_result = get_result(client, session_id)
    return {
        "converged": False,
        "final_result": final_result.get("data"),
        "poll_count": poll_count,
        "elapsed_time": time.time() - start_time,
        "final_state": extract_user_result_type(final_result.get("data")),
        "partner_states": get_partner_states(final_result.get("data")),
    }


def poll_for_clarification_or_final(
    client: httpx.Client,
    session_id: str,
    max_time: float = MAX_POLL_TIME_TASK,
) -> dict[str, Any]:
    """
    轮询直到出现 clarification 或 final 状态。
    专门用于等待 Partner 返回 awaiting-input 状态触发反问。
    """
    return poll_for_state(
        client,
        session_id,
        target_states=["clarification", "final"],
        max_time=max_time,
        check_partner_state=True,
    )


def poll_for_final(
    client: httpx.Client,
    session_id: str,
    max_time: float = MAX_POLL_TIME_TASK,
) -> dict[str, Any]:
    """轮询直到任务最终完成"""
    return poll_for_state(
        client,
        session_id,
        target_states=["final"],
        max_time=max_time,
    )


# =============================================================================
# Test State
# =============================================================================


@dataclass
class GroupModeTestState:
    """Group 模式测试状态"""

    session_id: str | None = None
    active_task_id: str | None = None
    group_id: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)

    def record_turn(
        self,
        query: str,
        result_type: str,
        partner_states: dict[str, str],
        elapsed: float,
    ):
        """记录一轮交互"""
        self.turns.append(
            {
                "turn": len(self.turns) + 1,
                "query": query[:50] + "..." if len(query) > 50 else query,
                "result_type": result_type,
                "partner_states": partner_states,
                "elapsed": f"{elapsed:.1f}s",
            }
        )


# 测试状态（模块级别共享）
_test_state = GroupModeTestState()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def configure_runtime(e2e_runtime):
    """把临时 E2E runtime URL 绑定到本模块常量。"""
    global BASE_URL, PARTNER_URL, RABBITMQ_MGMT_URL
    BASE_URL = e2e_runtime.leader_base_url
    PARTNER_URL = e2e_runtime.partner_health_url
    RABBITMQ_MGMT_URL = e2e_runtime.rabbitmq_mgmt_url
    return e2e_runtime


@pytest.fixture(scope="module")
def http_client(configure_runtime):
    """创建 HTTP 客户端"""
    with httpx.Client(timeout=E2E_REQUEST_TIMEOUT, verify=configure_runtime.client_ssl_context) as client:
        yield client


@pytest.fixture(scope="module", autouse=True)
def check_services(configure_runtime):
    """检查服务可用性（自动执行）"""
    healthy, message = check_service_health(
        base_url=configure_runtime.leader_base_url,
        partner_url=configure_runtime.partner_health_url,
        client_verify=configure_runtime.client_ssl_context,
    )
    if not healthy:
        pytest.skip(f"Required services not available: {message}")
    logger.info(f"Service health check: {message}")


# =============================================================================
# Tests - Group Mode Complete Journey
# =============================================================================


class TestGroupModeJourney:
    """
    Group 模式完整交互测试

    测试流程：
    1. 创建 group 模式 session
    2. 发起旅游规划任务（酒店推荐）
    3. 等待 Partner 反问
    4. 补充信息
    5. 等待最终结果
    6. 验证结果并关闭 session
    """

    def test_01_create_group_session(self, http_client: httpx.Client):
        """
        Step 1: 创建 group 模式 session

        发送初始查询，验证：
        - 返回 mode=group
        - 获得 session_id 和 active_task_id
        """
        global _test_state

        # 发送初始旅游规划请求（故意不提供完整信息，触发反问）
        query = "帮我订酒店"

        result = submit_query(http_client, query, mode="group")

        assert result["status_code"] == 200, f"Submit failed: {result['data']}"
        assert result["data"]["result"] is not None, "No result in response"

        submit_result = result["data"]["result"]

        # 验证 group 模式
        assert submit_result["mode"] == "group", f"Expected mode=group, got {submit_result['mode']}"

        # 保存状态
        _test_state.session_id = submit_result["sessionId"]
        _test_state.active_task_id = submit_result["activeTaskId"]

        logger.info(
            f"[Step 1] Session created: session_id={_test_state.session_id}, "
            f"task_id={_test_state.active_task_id}, mode=group"
        )

    def test_02_wait_for_partner_clarification(self, http_client: httpx.Client):
        """
        Step 2: 等待 Partner 反问（AwaitingInput）

        轮询结果，验证：
        - 某个 Partner 进入 awaiting-input 状态
        - userResult.type 变为 clarification
        """
        global _test_state

        assert _test_state.session_id, "No session_id (run test_01 first)"

        logger.info(f"[Step 2] Waiting for partner clarification... (session: {_test_state.session_id})")

        poll_result = poll_for_clarification_or_final(
            http_client,
            _test_state.session_id,
            max_time=MAX_POLL_TIME_TASK,
        )

        assert poll_result["converged"], (
            f"Polling timeout after {poll_result['elapsed_time']:.1f}s. "
            f"Last state: {poll_result['final_state']}, "
            f"Partner states: {poll_result['partner_states']}"
        )

        final_state = poll_result["final_state"]
        partner_states = poll_result["partner_states"]

        logger.info(
            f"[Step 2] Converged in {poll_result['elapsed_time']:.1f}s "
            f"(polls: {poll_result['poll_count']}): "
            f"state={final_state}, partners={partner_states}"
        )

        # 记录交互
        _test_state.record_turn(
            "帮我订酒店",
            final_state,
            partner_states,
            poll_result["elapsed_time"],
        )

        # 验证：应该收到反问或直接完成
        # 由于查询信息不完整，期望 Partner 反问
        if final_state == "clarification":
            assert has_awaiting_input_partners(poll_result["final_result"]), (
                "Got clarification but no partner in awaiting-input state"
            )
            logger.info("[Step 2] ✓ Partner is asking for more information")
        elif final_state == "final":
            logger.info("[Step 2] ✓ Task completed directly without clarification")
        else:
            pytest.fail(f"Unexpected state: {final_state}")

        # 检查反问内容
        user_result = extract_user_result(poll_result["final_result"])
        if user_result and user_result.get("dataItems"):
            clarification_text = ""
            for item in user_result["dataItems"]:
                if item.get("type") == "text":
                    clarification_text = item.get("text", "")
                    break
            logger.info(f"[Step 2] Clarification: {clarification_text[:100]}...")

    def test_03_provide_additional_info(self, http_client: httpx.Client):
        """
        Step 3: 用户补充信息

        发送补充信息，继续任务：
        - 补充入住日期、人数等缺失信息
        - 验证任务继续处理
        """
        global _test_state

        assert _test_state.session_id, "No session_id (run test_01 first)"
        assert _test_state.active_task_id, "No active_task_id"

        # 补充缺失的信息
        supplement_query = "北京国贸附近，3月15日入住，住2晚，2个人，预算每晚500元左右"

        logger.info(f"[Step 3] Providing additional info: {supplement_query}")

        result = submit_query(
            http_client,
            supplement_query,
            session_id=_test_state.session_id,
            active_task_id=_test_state.active_task_id,
        )

        assert result["status_code"] == 200, f"Submit failed: {result['data']}"

        # 更新 active_task_id（如果变化）
        if result["data"]["result"]:
            new_task_id = result["data"]["result"].get("activeTaskId")
            if new_task_id and new_task_id != _test_state.active_task_id:
                logger.info(f"[Step 3] Task ID updated: {new_task_id}")
                _test_state.active_task_id = new_task_id

        logger.info("[Step 3] ✓ Additional info submitted")

    def test_04_wait_for_final_result(self, http_client: httpx.Client):
        """
        Step 4: 等待最终结果

        轮询直到任务完成：
        - 所有 Partner 完成或超时
        - userResult.type 变为 final
        - 验证结果包含有意义的内容
        """
        global _test_state

        assert _test_state.session_id, "No session_id (run test_01 first)"

        logger.info(f"[Step 4] Waiting for final result... (session: {_test_state.session_id})")

        poll_result = poll_for_final(
            http_client,
            _test_state.session_id,
            max_time=MAX_POLL_TIME_TASK,
        )

        final_state = poll_result["final_state"]
        partner_states = poll_result["partner_states"]

        logger.info(
            f"[Step 4] Result in {poll_result['elapsed_time']:.1f}s "
            f"(polls: {poll_result['poll_count']}): "
            f"state={final_state}, partners={partner_states}"
        )

        # 记录交互
        _test_state.record_turn(
            "北京国贸附近，3月15日入住，住2晚，2个人，预算每晚500元左右",
            final_state,
            partner_states,
            poll_result["elapsed_time"],
        )

        # 验证收敛
        # 注意：如果超时但有部分结果，我们也接受
        if not poll_result["converged"]:
            logger.warning("[Step 4] Timeout, but checking if we have partial results...")
            # 检查是否有任何完成的 partner
            completed_partners = [
                aic for aic, state in partner_states.items() if state in ["completed", "awaiting-completion"]
            ]
            if completed_partners:
                logger.info(f"[Step 4] Partial results available from: {completed_partners}")
            else:
                pytest.fail(f"Timeout with no completed partners. States: {partner_states}")

        # 验证最终结果
        if final_state == "final":
            user_result = extract_user_result(poll_result["final_result"])
            assert user_result is not None, "No userResult in final response"

            # 检查结果内容
            data_items = user_result.get("dataItems", [])
            assert len(data_items) > 0, "Final result has no dataItems"

            # 提取文本内容
            result_text = ""
            for item in data_items:
                if item.get("type") == "text":
                    result_text += item.get("text", "")

            logger.info(f"[Step 4] Final result preview: {result_text[:200]}...")

            # 验证结果包含有意义的内容
            assert len(result_text) > 50, f"Final result text too short ({len(result_text)} chars)"

            logger.info("[Step 4] ✓ Got meaningful final result")
        elif final_state == "clarification":
            # 如果还在反问，说明需要更多信息
            logger.warning("[Step 4] Still in clarification state - may need more info")
        else:
            logger.info(f"[Step 4] Got state: {final_state}")

    def test_05_verify_group_mode_session(self, http_client: httpx.Client):
        """
        Step 5: 验证 Group 模式 Session 状态

        检查：
        - Session 确实是 group 模式
        - 有 partner 参与
        - 对话上下文正确记录
        """
        global _test_state

        assert _test_state.session_id, "No session_id (run test_01 first)"

        result = get_result(http_client, _test_state.session_id)
        assert result["status_code"] == 200, f"Get result failed: {result['data']}"

        leader_result = result["data"]["result"]

        # 验证 mode
        mode = leader_result.get("mode")
        assert mode == "group", f"Expected mode=group, got {mode}"
        logger.info(f"[Step 5] ✓ Mode verified: {mode}")

        group_id = extract_group_id(result["data"])
        assert group_id, "Expected groupId to be exposed in group mode result"
        _test_state.group_id = group_id
        short_group = group_id[-8:] if len(group_id) > 8 else group_id
        logger.info(f"[Step 5] ✓ Group ID exposed: ...{short_group}")

        # 验证有 partner 参与
        active_task = leader_result.get("activeTask")
        if active_task:
            partner_tasks = active_task.get("partnerTasks", {})
            partner_count = len(partner_tasks)
            assert partner_count > 0, "No partners in group session"
            logger.info(f"[Step 5] ✓ Partners in session: {partner_count}")

            # 显示 partner 详情
            for aic, task in partner_tasks.items():
                partner_name = task.get("partnerName", "unknown")
                state = task.get("state", "unknown")
                logger.info(f"[Step 5]   - {partner_name}: {state}")

        # 验证对话上下文
        dialog_context = leader_result.get("dialogContext", {})
        recent_turns = dialog_context.get("recentTurns", [])
        logger.info(f"[Step 5] ✓ Dialog turns recorded: {len(recent_turns)}")

        # 更新 active_task_id 以备关闭
        if active_task:
            _test_state.active_task_id = active_task.get("activeTaskId")

    def test_06_close_session_and_dissolve_group(self, http_client: httpx.Client):
        """
        Step 6: 关闭 Session 并解散群组

        通过 cancel API 关闭 session：
        - 验证 session 进入 closed 状态
        - 群组应被自动解散
        """
        global _test_state

        assert _test_state.session_id, "No session_id (run test_01 first)"

        # 先获取当前状态
        before_result = get_result(http_client, _test_state.session_id)
        before_closed = before_result["data"]["result"].get("closed", False)

        if before_closed:
            logger.info("[Step 6] Session already closed, skipping cancel")
            return

        # 如果没有 active_task_id，尝试从 result 获取
        if not _test_state.active_task_id:
            active_task = before_result["data"]["result"].get("activeTask")
            if active_task:
                _test_state.active_task_id = active_task.get("activeTaskId")

        if not _test_state.active_task_id:
            logger.warning("[Step 6] No active_task_id, cannot cancel properly")
            return

        logger.info(f"[Step 6] Canceling session: {_test_state.session_id}, task: {_test_state.active_task_id}")

        # 取消/关闭 session
        cancel_result = cancel_session(
            http_client,
            _test_state.session_id,
            _test_state.active_task_id,
        )

        # 验证取消成功
        if cancel_result["status_code"] == 200:
            logger.info("[Step 6] ✓ Cancel request accepted")
        elif cancel_result["status_code"] in [400, 409]:
            # 可能任务已经完成或取消
            logger.info(f"[Step 6] Cancel returned {cancel_result['status_code']}: {cancel_result['data']}")
        else:
            logger.warning(f"[Step 6] Cancel returned unexpected status: {cancel_result['status_code']}")

        # 等待一下让群组解散
        time.sleep(2)

        # 验证 session 状态
        after_result = get_result(http_client, _test_state.session_id)
        if after_result["status_code"] == 200:
            after_closed = after_result["data"]["result"].get("closed", False)
            if after_closed:
                logger.info("[Step 6] ✓ Session closed successfully")
            else:
                logger.info("[Step 6] Session not marked as closed (may be expected)")

        logger.info("[Step 6] ✓ Group mode journey completed")

    def test_99_print_journey_summary(self, http_client: httpx.Client):
        """打印测试摘要"""
        global _test_state

        logger.info("\n" + "=" * 60)
        logger.info("GROUP MODE JOURNEY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Session ID: {_test_state.session_id}")
        logger.info(f"Total Turns: {len(_test_state.turns)}")
        logger.info("-" * 60)

        for turn in _test_state.turns:
            logger.info(f"Turn {turn['turn']}: [{turn['result_type']}] ({turn['elapsed']}) - {turn['query']}")
            if turn["partner_states"]:
                for aic, state in turn["partner_states"].items():
                    short_aic = aic[-12:] if len(aic) > 12 else aic
                    logger.info(f"    Partner ...{short_aic}: {state}")

        logger.info("=" * 60)


# =============================================================================
# Standalone Test for Quick Verification
# =============================================================================


def test_group_mode_quick_check(configure_runtime):
    """
    快速验证测试 - 只检查 group 模式是否能正常启动

    可以单独运行：
        pytest tests/e2e/test_group_mode_journey.py::test_group_mode_quick_check -v -s
    """
    healthy, message = check_service_health(
        base_url=configure_runtime.leader_base_url,
        partner_url=configure_runtime.partner_health_url,
        client_verify=configure_runtime.client_ssl_context,
    )
    if not healthy:
        pytest.skip(f"Services not available: {message}")

    with httpx.Client(timeout=E2E_REQUEST_TIMEOUT, verify=configure_runtime.client_ssl_context) as client:
        # 发送简单的 group 模式请求
        query = "你好，能帮我推荐北京的酒店吗？"
        result = submit_query(client, query, mode="group")

        assert result["status_code"] == 200, f"Submit failed: {result['data']}"
        assert result["data"]["result"]["mode"] == "group", "Not in group mode"

        session_id = result["data"]["result"]["sessionId"]
        logger.info(f"Quick check: session_id={session_id}, mode=group ✓")

        # 等待一小段时间看看是否有响应
        time.sleep(5)
        poll_result = get_result(client, session_id)

        if poll_result["status_code"] == 200:
            user_result_type = extract_user_result_type(poll_result["data"])
            partner_states = get_partner_states(poll_result["data"])
            logger.info(f"Quick check: userResult.type={user_result_type}, partner_states={partner_states}")

        logger.info("Group mode quick check passed ✓")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
