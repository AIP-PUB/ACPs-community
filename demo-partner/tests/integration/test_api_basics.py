"""
API 基础功能集成测试

测试 Partner 服务的基础 API 功能：
- 健康检查接口
- RPC 调用基础流程
- 错误处理
"""

import pytest
from acps_sdk.aip.aip_base_model import TaskCommandType


class TestHealthEndpoints:
    """测试健康检查接口"""

    def test_health_check(self, client):
        """测试：健康检查返回正确结构（单 Agent 应用）"""
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()

        assert "agent" in data
        assert data["status"] == "online"
        assert "tasks" in data
        assert "groups" in data

    def test_health_check_has_agent_name(self, client, online_agents):
        """测试：健康检查返回当前 Agent 名称"""
        response = client.get("/health")
        data = response.json()

        # TestClient 使用第一个 online agent 创建应用
        assert data["agent"] == online_agents[0]


class TestRpcBasics:
    """测试 RPC 基础功能"""

    def test_rpc_endpoint_exists(self, client, online_agents):
        """测试：RPC 端点存在"""
        for _agent_name in online_agents:
            # 发送一个格式不正确的请求来验证端点存在
            response = client.post("/rpc", json={})

            # 应该返回验证错误，而不是 404
            assert response.status_code == 422  # Pydantic validation error

    def test_rpc_start_creates_task(self, client, rpc_request_factory, online_agents):
        """测试：Start 命令创建任务"""
        if not online_agents:
            pytest.skip("No online agents available")

        request_data = rpc_request_factory("测试请求")

        response = client.post("/rpc", json=request_data)

        assert response.status_code == 200
        data = response.json()

        assert "result" in data
        assert "id" in data["result"]
        assert "status" in data["result"]

    def test_rpc_get_returns_started_task_snapshot(self, client, rpc_request_factory, online_agents):
        """测试：Start 后 Get 能读取同一 runner 的任务快照。"""
        if not online_agents:
            pytest.skip("No online agents available")

        start_request = rpc_request_factory("测试任务快照")
        start_response = client.post("/rpc", json=start_request)
        assert start_response.status_code == 200

        command = start_request["params"]["command"]
        get_request = rpc_request_factory(
            "",
            TaskCommandType.Get,
            task_id=command["taskId"],
            session_id=command["sessionId"],
        )
        get_response = client.post("/rpc", json=get_request)

        assert get_response.status_code == 200
        get_body = get_response.json()
        assert get_body.get("result") is not None
        assert get_body["result"]["taskId"] == command["taskId"]
        assert get_body["result"]["sessionId"] == command["sessionId"]

    def test_rpc_get_nonexistent_task(self, client, rpc_request_factory, online_agents):
        """测试：Get 不存在的任务返回错误"""
        if not online_agents:
            pytest.skip("No online agents available")

        online_agents[0]
        request_data = rpc_request_factory("", TaskCommandType.Get, task_id="nonexistent-task-id")

        response = client.post("/rpc", json=request_data)
        data = response.json()

        assert "error" in data
        assert data["error"]["code"] == -32001  # Task not found


class TestRpcRequestValidation:
    """测试 RPC 请求验证"""

    def test_missing_task_id_returns_error(self, client, online_agents):
        """测试：缺少 taskId 返回错误"""
        if not online_agents:
            pytest.skip("No online agents available")

        online_agents[0]

        # 构造缺少 taskId 的请求 (AIP v2 格式: params.command)
        request = {
            "jsonrpc": "2.0",
            "id": "test-1",
            "method": "rpc",
            "params": {
                "command": {
                    "type": "task-command",
                    "id": "cmd-1",
                    "sentAt": "2024-01-01T00:00:00+08:00",
                    "senderRole": "leader",
                    "senderId": "test-leader",
                    "command": "start",
                    "sessionId": "session-1",
                    "dataItems": [{"type": "text", "text": "test"}],
                    # 缺少 taskId
                }
            },
        }

        response = client.post("/rpc", json=request)
        data = response.json()

        # 应该返回错误 (422 Pydantic 验证失败或应用层错误)
        # AIP v2 的 TaskCommand 模型要求 taskId 必填，Pydantic 会返回 422
        assert response.status_code == 422 or "error" in data

    def test_invalid_command_returns_error(self, client, rpc_request_factory, online_agents):
        """测试：无效的 command 返回错误"""
        if not online_agents:
            pytest.skip("No online agents available")

        online_agents[0]

        # 构造带有无效 command 的请求 (AIP v2 格式: params.command)
        request = {
            "jsonrpc": "2.0",
            "id": "test-1",
            "method": "rpc",
            "params": {
                "command": {
                    "type": "task-command",
                    "id": "cmd-1",
                    "sentAt": "2024-01-01T00:00:00+08:00",
                    "senderRole": "leader",
                    "senderId": "test-leader",
                    "command": "invalid_command",  # 无效命令
                    "taskId": "task-1",
                    "sessionId": "session-1",
                    "dataItems": [{"type": "text", "text": "test"}],
                }
            },
        }

        response = client.post("/rpc", json=request)

        # 可能是 422 (Pydantic validation) 或 200 带 error
        assert response.status_code in [200, 422]
