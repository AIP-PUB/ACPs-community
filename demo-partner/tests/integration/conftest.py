"""
Integration Test Fixtures - conftest.py

提供集成测试所需的 fixtures，包括：
- 同步包装的 ASGI client
- 真实 Agent 访问
- 测试数据工厂
"""

import asyncio
import os
import sys
import tomllib
from collections.abc import Callable, Generator
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from dotenv import load_dotenv
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv(Path(PROJECT_ROOT) / ".env", override=False)

# ASGITransport 不携带真实 TLS 证书；集成测试默认关闭身份绑定。
os.environ.setdefault("AIP_IDENTITY_BINDING_ENABLED", "false")

from acps_sdk.aip.aip_base_model import (  # noqa: E402
    TaskCommand,
    TaskCommandType,
    TaskState,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_model import RpcRequest, RpcRequestParams  # noqa: E402

from partners.main import create_agent_app  # noqa: E402
from partners.utils import discover_agents  # noqa: E402

# --- 时区常量 ---
BEIJING_TZ = timezone(timedelta(hours=8))


# --- LLM API 可用性检查 ---
def _check_llm_api_available() -> bool:
    """
    检查 LLM API 是否可用

    优先检查任意一个 partner 的 config.toml 中是否配置了有效的 API Key
    """
    # 首先检查 partner 配置文件
    partners_dir = Path(PROJECT_ROOT) / "partners" / "online"
    if partners_dir.exists():
        for entry in partners_dir.iterdir():
            config_path = entry / "config.toml"
            if config_path.exists():
                try:
                    with config_path.open("rb") as f:
                        config = tomllib.load(f)
                    llm_config = config.get("llm", {})
                    for profile in llm_config.values():
                        if isinstance(profile, dict):
                            api_key_env = profile.get("api_key_env", "")
                            if api_key_env and os.getenv(api_key_env):
                                return True

                            api_key = profile.get("api_key", "")
                            if api_key and not api_key.startswith("${") and len(api_key) > 10:
                                return True
                except Exception:  # noqa: S112
                    continue

    # 回退：检查环境变量
    api_key_vars = [
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "DEEPSEEK_API_KEY",
        "QWEN_API_KEY",
    ]
    return any(os.getenv(var) for var in api_key_vars)


LLM_API_AVAILABLE = _check_llm_api_available()

# 用于跳过需要 LLM 的测试（当配置文件和环境变量都没有 API Key 时才跳过）
requires_llm = pytest.mark.skipif(
    not LLM_API_AVAILABLE,
    reason="LLM API key not available in config.toml or environment variables",
)


# --- 测试用 Agent 发现 ---
_test_agents = discover_agents()
_first_agent_name = next(iter(_test_agents)) if _test_agents else None
_first_agent_path = _test_agents.get(_first_agent_name, "") if _first_agent_name else ""


# --- 同步包装的 ASGI client ---
class SyncASGIClient:
    """同步包装的 ASGI client，替代已弃用的 TestClient。"""

    def __init__(
        self,
        app: FastAPI,
        *,
        base_url: str = "http://testserver",
        raise_app_exceptions: bool = True,
    ) -> None:
        self._app = app
        self._base_url = base_url
        self._raise_app_exceptions = raise_app_exceptions
        self._runner: asyncio.Runner | None = None
        self._lifespan_cm: AbstractAsyncContextManager[Any] | None = None
        self._client: AsyncClient | None = None

    def __enter__(self) -> SyncASGIClient:
        self._runner = asyncio.Runner()
        self._runner.__enter__()

        async def _startup() -> None:
            lifespan_cm = self._app.router.lifespan_context(self._app)
            self._lifespan_cm = lifespan_cm
            await lifespan_cm.__aenter__()
            self._client = AsyncClient(
                transport=ASGITransport(
                    app=self._app,
                    raise_app_exceptions=self._raise_app_exceptions,
                ),
                base_url=self._base_url,
            )

        self._runner.run(_startup())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
        if self._runner is not None:
            self._runner.__exit__(exc_type, exc, tb)
            self._runner = None

    def close(self) -> None:
        if self._runner is None:
            return

        async def _shutdown() -> None:
            if self._client is not None:
                await self._client.aclose()
            if self._lifespan_cm is not None:
                await self._lifespan_cm.__aexit__(None, None, None)

        self._runner.run(_shutdown())
        self._client = None
        self._lifespan_cm = None

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self._runner is None or self._client is None:
            raise RuntimeError("SyncASGIClient must be used as a context manager")
        return self._runner.run(self._client.request(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)


@pytest.fixture(scope="module")
def client() -> Generator[SyncASGIClient]:
    """
    提供同步包装的 ASGI client。
    scope="module" 确保在每个测试模块中只创建一次。
    使用第一个 online agent 创建测试应用。
    """
    if not _first_agent_name:
        pytest.skip("No online agents available")
    test_app = create_agent_app(_first_agent_name, _first_agent_path)
    with SyncASGIClient(test_app) as c:
        yield c


@pytest.fixture(scope="module")
def async_client() -> AsyncClient:
    """提供异步 HTTP 客户端"""
    if not _first_agent_name:
        pytest.skip("No online agents available")
    test_app = create_agent_app(_first_agent_name, _first_agent_path)
    return AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


@pytest.fixture
def client_for_agent() -> Generator[Callable[[str], SyncASGIClient]]:
    """按 agent 名称懒加载并缓存对应的同步 ASGI client。"""

    active_clients: dict[str, SyncASGIClient] = {}

    def _get(agent_name: str) -> SyncASGIClient:
        agent_path = _test_agents.get(agent_name)
        if not agent_path:
            pytest.fail(f"Unknown online agent: {agent_name}")

        client = active_clients.get(agent_name)
        if client is None:
            test_app = create_agent_app(agent_name, agent_path)
            client = SyncASGIClient(test_app)
            client.__enter__()
            active_clients[agent_name] = client

        return client

    try:
        yield _get
    finally:
        for active_client in active_clients.values():
            active_client.__exit__(None, None, None)


# --- Agent 信息 ---
@pytest.fixture(scope="module")
def online_agents():
    """获取所有在线的 Agent 名称列表"""
    return list(_test_agents.keys())


@pytest.fixture
def get_agent_runner() -> Callable[[str], None]:
    """获取指定 Agent 的 GenericRunner 实例（测试环境下不可用）"""

    def _get(agent_name: str) -> None:
        return None

    return _get


# --- 命令和请求工厂 ---
@pytest.fixture
def message_factory():
    """创建测试 TaskCommand 的工厂函数"""

    def create_command(
        text: str,
        command_type: TaskCommandType = TaskCommandType.Start,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> TaskCommand:
        now = datetime.now(BEIJING_TZ)
        return TaskCommand(
            id=f"cmd-{now.timestamp()}",
            sentAt=now.isoformat(),
            senderRole="leader",
            senderId="test-leader-001",
            command=command_type,  # 使用 command 字段名
            taskId=task_id or f"task-{now.timestamp()}",
            sessionId=session_id or f"session-{now.timestamp()}",
            dataItems=[TextDataItem(text=text)],
        )

    return create_command


@pytest.fixture
def rpc_request_factory(message_factory):
    """创建测试 RPC 请求的工厂函数"""

    def create_request(
        text: str,
        command_type: TaskCommandType = TaskCommandType.Start,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        command = message_factory(text, command_type, task_id, session_id)
        request = RpcRequest(
            id=f"rpc-{datetime.now(BEIJING_TZ).timestamp()}",
            params=RpcRequestParams(command=command),
        )
        return request.model_dump()

    return create_request


@pytest.fixture
def rpc_call(client_for_agent, rpc_request_factory):
    """
    执行 RPC 调用的辅助函数。
    返回 (response_json, task_id, session_id) 元组。
    """

    def _call(
        agent_name: str,
        text: str,
        command: TaskCommandType = TaskCommandType.Start,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        request_data = rpc_request_factory(text, command, task_id, session_id)
        response = client_for_agent(agent_name).post("/rpc", json=request_data)

        # 从请求中提取 task_id 和 session_id (AIP v2: params.command)
        cmd = request_data["params"]["command"]
        return response.json(), cmd["taskId"], cmd["sessionId"]

    return _call


# --- 任务生命周期辅助 ---
@pytest.fixture
def wait_for_state(client_for_agent, rpc_request_factory):
    """
    等待任务达到指定状态。
    使用 Get 命令轮询任务状态。
    """
    import time

    def _wait(
        agent_name: str,
        task_id: str,
        session_id: str,
        target_states: list[TaskState],
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        start_time = time.time()
        target_state_values = [s.value for s in target_states]

        while time.time() - start_time < timeout:
            request_data = rpc_request_factory(
                text="",
                command_type=TaskCommandType.Get,
                task_id=task_id,
                session_id=session_id,
            )
            response = client_for_agent(agent_name).post("/rpc", json=request_data)
            result = response.json()

            if result.get("result"):
                state = result["result"].get("status", {}).get("state")
                if state in target_state_values:
                    return result

            time.sleep(poll_interval)

        raise TimeoutError(f"Task {task_id} did not reach state {target_states} within {timeout}s")

    return _wait


@pytest.fixture
def continue_task(client_for_agent, rpc_request_factory):
    """
    向处于 AwaitingInput 状态的任务发送 Continue 命令。
    用于补充信息继续任务流程。
    """

    def _continue(
        agent_name: str,
        task_id: str,
        session_id: str,
        text: str,
    ) -> dict[str, Any]:
        request_data = rpc_request_factory(
            text=text,
            command_type=TaskCommandType.Continue,
            task_id=task_id,
            session_id=session_id,
        )
        response = client_for_agent(agent_name).post("/rpc", json=request_data)
        return response.json()

    return _continue


@pytest.fixture
def complete_task_flow(rpc_call, wait_for_state):
    """
    执行完整的任务流程：Start -> 等待状态 -> (可选 Continue) -> Complete。
    返回最终的任务结果。
    """

    def _flow(
        agent_name: str,
        initial_text: str,
        continue_texts: list[str] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        # Step 1: Start
        result, task_id, session_id = rpc_call(agent_name, initial_text, TaskCommandType.Start)

        # Step 2: 等待处理结果
        result = wait_for_state(
            agent_name,
            task_id,
            session_id,
            [
                TaskState.AwaitingInput,
                TaskState.AwaitingCompletion,
                TaskState.Rejected,
                TaskState.Failed,
            ],
            timeout=timeout,
        )

        # Step 3: 如果需要补充信息，发送 Continue
        if continue_texts:
            for text in continue_texts:
                state = result["result"]["status"]["state"]
                if state == TaskState.AwaitingInput.value:
                    result, _, _ = rpc_call(agent_name, text, TaskCommandType.Continue, task_id, session_id)
                    result = wait_for_state(
                        agent_name,
                        task_id,
                        session_id,
                        [
                            TaskState.AwaitingInput,
                            TaskState.AwaitingCompletion,
                            TaskState.Rejected,
                            TaskState.Failed,
                        ],
                        timeout=timeout,
                    )

        # Step 4: 如果到达 AwaitingCompletion，发送 Complete
        final_state = result["result"]["status"]["state"]
        if final_state == TaskState.AwaitingCompletion.value:
            result, _, _ = rpc_call(agent_name, "确认完成", TaskCommandType.Complete, task_id, session_id)

        return result

    return _flow


# --- 状态验证辅助 ---
@pytest.fixture
def assert_rpc_success() -> Callable[[dict[str, Any], str], None]:
    """验证 RPC 响应成功"""

    def _assert(response: dict[str, Any], msg: str = "") -> None:
        assert "error" not in response or response["error"] is None, f"RPC error: {response.get('error')}. {msg}"
        assert "result" in response, f"Missing result in response. {msg}"

    return _assert


@pytest.fixture
def assert_task_state() -> Callable[[dict[str, Any], TaskState, str], None]:
    """验证任务状态"""

    def _assert(response: dict[str, Any], expected_state: TaskState, msg: str = "") -> None:
        actual_state = response.get("result", {}).get("status", {}).get("state")
        assert actual_state == expected_state.value, f"Expected state {expected_state.value}, got {actual_state}. {msg}"

    return _assert


@pytest.fixture
def assert_task_has_product() -> Callable[[dict[str, Any], str], None]:
    """验证任务有产出物"""

    def _assert(response: dict[str, Any], msg: str = "") -> None:
        products = response.get("result", {}).get("products", [])
        assert len(products) > 0, f"Task has no products. {msg}"

    return _assert


@pytest.fixture
def get_task_context(
    get_agent_runner: Callable[[str], Any],
) -> Callable[[str, str], Any]:
    """获取任务的内部上下文（用于验证内部状态）"""

    def _get(agent_name: str, task_id: str) -> Any:
        runner = get_agent_runner(agent_name)
        if runner and task_id in runner.tasks:
            return runner.tasks[task_id]
        return None

    return _get


# --- 测试数据 ---
@pytest.fixture
def test_scenarios():
    """提供各 Agent 的测试场景"""
    return {
        "beijing_urban": {
            "accept": [
                "我想去北京故宫和天坛玩两天",
                "请推荐一下北京城区的文化景点",
                "帮我规划一个北京城区亲子游行程",
            ],
            "reject": [
                "我想去上海迪士尼玩",
                "请推荐北京郊区的民宿",
                "帮我预订北京的酒店",
            ],
        },
        "beijing_rural": {
            "accept": [
                "我想去长城和十三陵玩",
                "请推荐北京郊区的自然风光景点",
                "帮我规划一个去慕田峪长城的行程",
            ],
            "reject": ["我想去北京故宫", "请推荐上海的景点"],
        },
        "beijing_food": {
            "accept": [
                "推荐北京好吃的烤鸭店",
                "我想吃正宗的北京小吃",
                "帮我找一家适合聚餐的餐厅",
            ],
            "reject": ["我想去故宫玩", "请推荐上海的美食"],
        },
        "china_hotel": {
            "accept": [
                "帮我在北京找一家五星级酒店",
                "我需要预订一间标准间",
                "推荐北京性价比高的酒店",
            ],
            "reject": ["我想去故宫玩", "帮我规划旅游路线"],
        },
        "china_transport": {
            "accept": [
                "帮我查一下北京到上海的高铁",
                "我想预订机票",
                "查询北京南站到上海虹桥的车次",
            ],
            "reject": ["我想去故宫玩", "推荐北京的美食"],
        },
    }


# --- 异步测试支持 ---
@pytest.fixture(scope="session")
def event_loop():
    """创建会话级别的事件循环"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
