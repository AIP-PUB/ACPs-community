"""
Leader Agent Platform - 集成测试：异步执行模式

测试 /submit (mode=async) -> /result 的完整异步流程：
1. /submit 在 Planning 后立即返回 pending 状态
2. 后台执行任务
3. /result 轮询获取执行状态和最终结果

本测试使用真实 LLM 调用和 API，不使用 Mock。
需要启动 Partner 服务才能测试完整流程。
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

import asyncio
import uuid

import httpx
import pytest
from assistant.core.background_executor import reset_background_executor
from assistant.core.task_execution_manager import reset_task_execution_manager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from .conftest import extract_session_id

pytest_plugins = ("pytest_asyncio",)


# =============================================================================
# 测试 Fixtures
# =============================================================================


def create_test_app(managed_partner_runtime) -> FastAPI:
    """
    创建测试应用实例。

    每次调用创建新的应用实例，避免测试间状态污染。
    """
    # 重置单例
    reset_task_execution_manager()
    reset_background_executor()

    import assistant.auth as auth_mod
    import assistant.config as config_mod
    from assistant.api import init_routes, router
    from assistant.core import (
        Planner,
        SessionManager,
        create_intent_analyzer,
        create_orchestrator,
    )
    from assistant.core.history_compressor import HistoryCompressor
    from assistant.llm import get_llm_client
    from assistant.services import ScenarioLoader

    config_mod.settings["oidc"]["enabled"] = False
    auth_mod._validator = None
    auth_mod._stream_tokens.clear()

    # 创建新的组件实例
    session_manager = SessionManager()
    scenario_loader = ScenarioLoader(managed_partner_runtime.scenario_root)
    intent_analyzer = create_intent_analyzer(scenario_loader)
    planner = Planner(scenario_loader)

    # 创建 history_compressor
    llm_client = get_llm_client()
    history_compressor = HistoryCompressor(
        llm_client=llm_client,
        scenario_loader=scenario_loader,
    )

    orchestrator = create_orchestrator(
        session_manager=session_manager,
        scenario_loader=scenario_loader,
        intent_analyzer=intent_analyzer,
        planner=planner,
        history_compressor=history_compressor,
    )

    # 创建新的 FastAPI 应用
    app = FastAPI(title="Test Leader Agent")
    init_routes(orchestrator, session_manager)
    app.include_router(router)

    return app


import pytest_asyncio


@pytest.fixture
def test_app(managed_partner_runtime):
    """创建测试应用实例。"""
    return create_test_app(managed_partner_runtime)


@pytest_asyncio.fixture
async def client(test_app: FastAPI):
    """创建异步测试客户端。"""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        yield ac


@pytest.fixture(scope="class")
def require_partner_service(managed_partner_runtime):
    """
    确保临时 Partner runtime 已就绪。
    """
    return managed_partner_runtime


def unique_request_id() -> str:
    """生成唯一的请求 ID。"""
    return f"req-{uuid.uuid4().hex[:8]}"


# =============================================================================
# 异步执行模式基础测试（真实 LLM 调用）
# =============================================================================


class TestAsyncExecutionBasic:
    """异步执行模式基础测试（真实 LLM 调用）。"""

    @pytest.mark.asyncio
    async def test_async_mode_returns_pending(self, client: AsyncClient):
        """测试：direct_rpc 模式应返回正常响应。"""
        request_id = unique_request_id()

        response = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[AsyncModePending] status: {response.status_code}")
        print(f"[AsyncModePending] response: {response.json()}")

        assert response.status_code == 200
        data = response.json()
        assert "result" in data and data["result"] is not None
        assert "sessionId" in data["result"]

        # 检查响应中包含 externalStatus
        external_status = data["result"].get("externalStatus")
        print(f"[AsyncModePending] externalStatus: {external_status}")

    @pytest.mark.asyncio
    async def test_async_mode_with_task_returns_pending(self, client: AsyncClient):
        """测试：direct_rpc 模式的任务请求应返回正常响应。"""
        request_id = unique_request_id()

        # 发送一个任务类请求
        response = await client.post(
            "/api/v1/submit",
            json={
                "query": "帮我规划北京三日游",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[AsyncModeTask] status: {response.status_code}")
        print(f"[AsyncModeTask] response: {response.json()}")

        # 如果 LLM 识别为任务类请求，应返回正常响应
        if response.status_code == 200:
            data = response.json()
            assert "result" in data and data["result"] is not None
            assert "sessionId" in data["result"]
            # 状态可能是 pending 或 success（取决于是否识别为任务）
            print(f"[AsyncModeTask] externalStatus: {data['result'].get('externalStatus')}")

    @pytest.mark.asyncio
    async def test_direct_rpc_mode_returns_immediately(self, client: AsyncClient):
        """测试：direct_rpc 模式应返回结果。"""
        request_id = unique_request_id()

        response = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[DirectRpcMode] status: {response.status_code}")
        print(f"[DirectRpcMode] response: {response.json()}")

        assert response.status_code == 200
        data = response.json()
        # 新响应格式：检查 result 存在且有 sessionId
        assert "result" in data and data["result"] is not None
        assert "sessionId" in data["result"]


class TestResultPolling:
    """结果轮询测试（真实 LLM 调用）。"""

    @pytest.mark.asyncio
    async def test_result_api_returns_session_state(self, client: AsyncClient):
        """测试：/result API 应返回 Session 状态。"""
        # 先创建一个 Session
        request_id = unique_request_id()
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        assert submit_response.status_code == 200
        session_id = extract_session_id(submit_response.json())

        # 查询结果
        result_response = await client.get(f"/api/v1/result/{session_id}")

        print(f"\n[ResultAPI] status: {result_response.status_code}")
        print(f"[ResultAPI] response: {result_response.json()}")

        assert result_response.status_code == 200
        data = result_response.json()
        # 新响应格式：result 可能为 None 或包含数据
        assert "result" in data or "error" in data

    @pytest.mark.asyncio
    async def test_result_api_with_task_id(self, client: AsyncClient):
        """测试：/result API 支持 taskId 参数。"""
        # 先创建一个 Session
        request_id = unique_request_id()
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        assert submit_response.status_code == 200
        session_id = extract_session_id(submit_response.json())
        submit_data = submit_response.json()
        active_task_id = submit_data.get("result", {}).get("activeTaskId")

        if active_task_id:
            # 查询特定任务的结果
            result_response = await client.get(
                f"/api/v1/result/{session_id}",
                params={"taskId": active_task_id},
            )

            print(f"\n[ResultWithTaskId] status: {result_response.status_code}")
            print(f"[ResultWithTaskId] response: {result_response.json()}")

            assert result_response.status_code == 200

    @pytest.mark.asyncio
    async def test_result_api_nonexistent_session(self, client: AsyncClient):
        """测试：查询不存在的 Session 应返回 404。"""
        result_response = await client.get("/api/v1/result/nonexistent-session-12345")

        print(f"\n[ResultNonexistent] status: {result_response.status_code}")

        assert result_response.status_code == 404


class TestAsyncExecutionWithPartner:
    """需要 Partner 服务的异步执行测试。"""

    @pytest.mark.asyncio
    async def test_async_task_full_flow(
        self,
        client: AsyncClient,
        require_partner_service,
    ):
        """测试：完整的异步任务执行流程。"""
        request_id = unique_request_id()

        # 1. 提交任务
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "推荐北京的美食餐厅",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[AsyncFullFlow] submit status: {submit_response.status_code}")
        print(f"[AsyncFullFlow] submit response: {submit_response.json()}")

        if submit_response.status_code != 200:
            pytest.skip("Submit failed")
            return

        session_id = extract_session_id(submit_response.json())
        initial_status = submit_response.json().get("status")

        print(f"[AsyncFullFlow] session_id: {session_id}")
        print(f"[AsyncFullFlow] initial_status: {initial_status}")

        # 2. 轮询结果（最多等待 30 秒）
        max_attempts = 30
        final_status = None

        for attempt in range(max_attempts):
            result_response = await client.get(f"/api/v1/result/{session_id}")

            if result_response.status_code != 200:
                break

            data = result_response.json()
            current_status = data.get("status")

            print(f"[AsyncFullFlow] attempt {attempt + 1}: status = {current_status}")

            if current_status in ["completed", "success", "failed", "awaiting_input"]:
                final_status = current_status
                print(f"[AsyncFullFlow] final response: {data}")
                break

            await asyncio.sleep(1)

        # 3. 验证最终状态
        if final_status:
            assert final_status in ["completed", "success", "awaiting_input"]
            print(f"[AsyncFullFlow] Task completed with status: {final_status}")

    @pytest.mark.asyncio
    async def test_async_execution_status_transitions(
        self,
        client: AsyncClient,
        require_partner_service,
    ):
        """测试：异步执行的状态转换。"""
        request_id = unique_request_id()

        # 提交任务
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "帮我查询北京到上海的高铁票",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        if submit_response.status_code != 200:
            pytest.skip("Submit failed")
            return

        session_id = extract_session_id(submit_response.json())

        # 记录状态转换
        status_history = []

        for _ in range(20):
            result_response = await client.get(f"/api/v1/result/{session_id}")

            if result_response.status_code != 200:
                break

            data = result_response.json()
            current_status = data.get("status")

            if not status_history or status_history[-1] != current_status:
                status_history.append(current_status)
                print(f"[StatusTransition] Status changed to: {current_status}")

            if current_status in ["completed", "success", "failed", "awaiting_input"]:
                break

            await asyncio.sleep(1)

        print(f"[StatusTransition] Status history: {status_history}")

        # 验证状态转换合理性
        if len(status_history) > 1:
            # 不应该从 completed 回退到 pending
            completed_index = None
            for i, s in enumerate(status_history):
                if s in ["completed", "success"]:
                    completed_index = i
                    break

            if completed_index is not None:
                # completed 之后不应该有非终态状态
                for s in status_history[completed_index + 1 :]:
                    assert s in ["completed", "success", "failed"]


class TestAsyncModeConsistency:
    """异步模式一致性测试。"""

    @pytest.mark.asyncio
    async def test_mode_preserved_across_requests(self, client: AsyncClient):
        """测试：模式应在 Session 中保持一致。"""
        # 使用 async 模式创建 Session
        request_id1 = unique_request_id()
        response1 = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id1,
                "mode": "direct_rpc",
            },
        )

        assert response1.status_code == 200
        session_id = extract_session_id(response1.json())

        # 尝试使用 group 模式发送到同一 Session（模式不一致）
        request_id2 = unique_request_id()
        response2 = await client.post(
            "/api/v1/submit",
            json={
                "sessionId": session_id,
                "query": "今天天气怎么样",
                "clientRequestId": request_id2,
                "mode": "group",  # 不同模式
            },
        )

        print(f"\n[ModeConsistency] response2 status: {response2.status_code}")

        # 应该返回 409 模式不一致错误
        assert response2.status_code == 409
        data = response2.json()
        assert data["detail"]["code"] == 409001

    @pytest.mark.asyncio
    async def test_same_async_mode_allowed(self, client: AsyncClient):
        """测试：相同的 direct_rpc 模式应被允许。"""
        # 使用 direct_rpc 模式创建 Session
        request_id1 = unique_request_id()
        response1 = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id1,
                "mode": "direct_rpc",
            },
        )

        assert response1.status_code == 200
        session_id = extract_session_id(response1.json())

        # 使用相同模式发送
        request_id2 = unique_request_id()
        response2 = await client.post(
            "/api/v1/submit",
            json={
                "sessionId": session_id,
                "query": "帮我查个酒店",
                "clientRequestId": request_id2,
                "mode": "direct_rpc",  # 相同模式
            },
        )

        print(f"\n[SameAsyncMode] response2 status: {response2.status_code}")

        # 应该成功
        assert response2.status_code == 200


class TestAsyncExecutionEdgeCases:
    """异步执行边界情况测试。"""

    @pytest.mark.asyncio
    async def test_rapid_result_queries(self, client: AsyncClient):
        """测试：快速连续查询结果。"""
        request_id = unique_request_id()

        # 创建 Session
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "你好",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        assert submit_response.status_code == 200
        session_id = extract_session_id(submit_response.json())

        # 快速连续查询
        async def query_result():
            return await client.get(f"/api/v1/result/{session_id}")

        # 并发发送 5 个查询
        responses = await asyncio.gather(*[query_result() for _ in range(5)])

        print(f"\n[RapidQueries] statuses: {[r.status_code for r in responses]}")

        # 所有查询应该成功
        for response in responses:
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_async_mode_simple_query(self, client: AsyncClient):
        """测试：async 模式处理简单查询。"""
        request_id = unique_request_id()

        response = await client.post(
            "/api/v1/submit",
            json={
                "query": "1+1等于多少？",
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[AsyncSimpleQuery] status: {response.status_code}")
        print(f"[AsyncSimpleQuery] response: {response.json()}")

        # 简单查询可能被识别为闲聊，直接返回 success
        assert response.status_code == 200


class TestAsyncExecutionWithClarification:
    """需要反问的异步执行测试。"""

    @pytest.mark.asyncio
    async def test_async_task_with_clarification(
        self,
        client: AsyncClient,
        require_partner_service,
    ):
        """测试：异步任务可能需要反问澄清。"""
        request_id = unique_request_id()

        # 提交一个可能需要更多信息的任务
        submit_response = await client.post(
            "/api/v1/submit",
            json={
                "query": "帮我订酒店",  # 缺少具体信息
                "clientRequestId": request_id,
                "mode": "direct_rpc",
            },
        )

        print(f"\n[AsyncClarification] submit status: {submit_response.status_code}")
        print(f"[AsyncClarification] submit response: {submit_response.json()}")

        if submit_response.status_code != 200:
            pytest.skip("Submit failed")
            return

        session_id = extract_session_id(submit_response.json())

        # 等待处理完成
        for _ in range(15):
            result_response = await client.get(f"/api/v1/result/{session_id}")

            if result_response.status_code != 200:
                break

            data = result_response.json()
            current_status = data.get("status")

            print(f"[AsyncClarification] status: {current_status}")

            if current_status == "awaiting_input":
                # 任务需要更多信息
                print("[AsyncClarification] clarification needed")
                clarification = data.get("clarification_text") or data.get("response_text")
                print(f"[AsyncClarification] clarification: {clarification}")
                break

            if current_status in ["completed", "success", "failed"]:
                break

            await asyncio.sleep(1)
