"""
Unit Test Fixtures - conftest.py

提供单元测试所需的 fixtures，包括：
- Mock LLM 客户端
- 测试用 Agent 配置
- 通用测试工具函数
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# TestClient / ASGITransport 不携带真实 TLS 证书；单元测试默认关闭身份绑定。
os.environ.setdefault("AIP_IDENTITY_BINDING_ENABLED", "false")

from acps_sdk.aip.aip_base_model import (  # noqa: E402
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_model import RpcRequest, RpcRequestParams  # noqa: E402

# --- 时区常量 ---
BEIJING_TZ = timezone(timedelta(hours=8))


# --- Mock LLM 响应工厂 ---
class MockLLMResponses:
    """预定义的 Mock LLM 响应"""

    @staticmethod
    def decision_accept() -> str:
        return json.dumps({"decision": "accept", "reason": "Request is within scope"})

    @staticmethod
    def decision_reject(reason: str = "Request is out of scope") -> str:
        return json.dumps({"decision": "reject", "reason": reason})

    @staticmethod
    def analysis_complete(requirements: dict[str, Any] | None = None) -> str:
        default_reqs = {
            "scope": "city-core-only",
            "theme": "cultural",
            "days": 2,
            "preferences": ["轻体力", "文化深度"],
            "budgetLevel": "medium",
            "mustSee": ["故宫", "天坛"],
            "avoid": [],
            "missingFields": [],
        }
        return json.dumps(
            {
                "decision": "accept",
                "reason": "All required information collected",
                "requirements": requirements or default_reqs,
            }
        )

    @staticmethod
    def analysis_missing_fields(missing: list[str], partial_reqs: dict[str, Any] | None = None) -> str:
        default_reqs = {
            "scope": "city-core-only",
            "theme": None,
            "days": 1,
            "preferences": [],
            "budgetLevel": None,
            "mustSee": [],
            "avoid": [],
            "missingFields": missing,
        }
        return json.dumps(
            {
                "decision": "reject",
                "reason": f"Please provide: {', '.join(missing)}",
                "requirements": partial_reqs or default_reqs,
            }
        )

    @staticmethod
    def production_output(content: str = "这是一份精心规划的行程方案...") -> str:
        return content


@pytest.fixture
def mock_llm_responses():
    """提供 Mock LLM 响应工厂"""
    return MockLLMResponses


# --- Mock OpenAI 客户端 ---
@pytest.fixture
def mock_openai_client():
    """创建 Mock OpenAI 客户端"""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = MockLLMResponses.decision_accept()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture
def mock_openai_client_factory():
    """提供可配置响应的 Mock OpenAI 客户端工厂"""

    def create_mock(response_content: str) -> AsyncMock:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = response_content
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        return mock_client

    return create_mock


# --- 测试用 Agent 配置文件 ---
@pytest.fixture
def test_agent_acs():
    """测试用 ACS 配置"""
    return {
        "aic": "test-agent-001",
        "active": True,
        "name": "测试智能体",
        "description": "用于单元测试的模拟智能体。职责：处理测试请求。",
        "version": "1.0.0",
        "skills": [
            {
                "id": "test.skill-one",
                "name": "测试技能一",
                "description": "用于测试的技能",
                "tags": ["test", "demo"],
            }
        ],
    }


@pytest.fixture
def test_agent_config():
    """测试用 config.toml 配置"""
    return {
        "llm": {
            "default": {
                "api_key": "test-api-key",
                "base_url": "https://api.test.com/v1/",
                "model": "test-model",
            },
            "fast": {
                "api_key": "test-api-key",
                "base_url": "https://api.test.com/v1/",
                "model": "test-model-fast",
            },
        },
        "concurrency": {"max_concurrent_tasks": 5},
        "log": {"level": "DEBUG"},
    }


@pytest.fixture
def test_agent_prompts():
    """测试用 prompts.toml 配置"""
    return {
        "decision": {
            "llm_profile": "fast",
            "output_schema": '{"decision": "accept | reject", "reason": "string"}',
            "system": "你是测试门卫。判断请求是否应当处理。输出: {{output_schema}}",
            "user": "{{input}}",
        },
        "analysis": {
            "llm_profile": "fast",
            "output_schema": '{"decision": "accept | reject", "reason": "string", "requirements": {}}',
            "system": "你是测试分析师。提取需求。输出: {{output_schema}}",
            "user": "{{input}}",
        },
        "production": {
            "llm_profile": "default",
            "system": "你是测试生成器。根据需求生成内容。",
            "user": "{{requirements}}",
        },
    }


@pytest.fixture
def test_agent_dir(test_agent_acs, test_agent_config, test_agent_prompts):
    """创建临时测试 Agent 目录，包含所有配置文件"""
    import tomli_w

    temp_dir = tempfile.mkdtemp(prefix="test_agent_")

    # 写入 acs.json
    temp_path = Path(temp_dir)
    with (temp_path / "acs.json").open("w", encoding="utf-8") as f:
        json.dump(test_agent_acs, f, ensure_ascii=False, indent=2)

    # 写入 config.toml
    with (temp_path / "config.toml").open("wb") as f:
        tomli_w.dump(test_agent_config, f)

    # 写入 prompts.toml
    with (temp_path / "prompts.toml").open("wb") as f:
        tomli_w.dump(test_agent_prompts, f)

    yield temp_dir

    # 清理
    shutil.rmtree(temp_dir, ignore_errors=True)


# --- 命令和请求工厂 ---
@pytest.fixture
def message_factory():
    """创建测试 TaskCommand 的工厂函数"""

    def create_command(
        text: str,
        command_type: TaskCommandType = TaskCommandType.Start,
        task_id: str | None = None,
        session_id: str = "test-session-001",
    ) -> TaskCommand:
        now = datetime.now(BEIJING_TZ)
        return TaskCommand(
            id=f"cmd-{now.timestamp()}",
            sentAt=now.isoformat(),
            senderRole="leader",
            senderId="test-leader-001",
            command=command_type,  # 使用 command 字段名
            taskId=task_id or f"task-{now.timestamp()}",
            sessionId=session_id,
            dataItems=[TextDataItem(text=text)],
        )

    return create_command


@pytest.fixture
def rpc_request_factory(message_factory):
    """创建测试 RpcRequest 的工厂函数"""

    def create_request(
        text: str,
        command_type: TaskCommandType = TaskCommandType.Start,
        task_id: str | None = None,
        session_id: str = "test-session-001",
    ) -> RpcRequest:
        command = message_factory(text, command_type, task_id, session_id)
        return RpcRequest(
            id=f"rpc-{datetime.now(BEIJING_TZ).timestamp()}",
            params=RpcRequestParams(command=command),
        )

    return create_request


# --- 异步测试支持 ---
@pytest.fixture
def event_loop():
    """创建事件循环"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# --- GenericRunner Mock ---
@pytest.fixture
def mock_generic_runner(test_agent_dir, mock_openai_client):
    """创建带有 Mock LLM 客户端的 GenericRunner"""
    from partners.generic_runner import GenericRunner

    with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_openai_client):
        runner = GenericRunner("test_agent", test_agent_dir)
        yield runner


# --- 状态验证辅助函数 ---
@pytest.fixture
def assert_task_state() -> Callable[[TaskResult, TaskState, str], None]:
    """验证任务状态的辅助函数"""

    def _assert(task: TaskResult, expected_state: TaskState, msg: str = "") -> None:
        assert task.status.state == expected_state, f"Expected state {expected_state}, got {task.status.state}. {msg}"

    return _assert


@pytest.fixture
def assert_task_has_data_item() -> Callable[[TaskResult, str], None]:
    """验证任务状态中包含特定数据项"""

    def _assert(task: TaskResult, text_contains: str) -> None:
        data_items = task.status.dataItems or []
        texts = [item.text for item in data_items if hasattr(item, "text")]
        assert any(text_contains in t for t in texts), f"Expected data item containing '{text_contains}', got {texts}"

    return _assert
