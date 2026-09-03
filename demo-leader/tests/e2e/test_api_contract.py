"""
Leader Agent Platform - E2E Tests: API Contract Tests

这些测试验证 API 的输入输出格式是否符合定义的规范。
仅依赖 HTTP 响应进行断言，不依赖内部数据结构。

运行方式：
    pytest tests/e2e/test_api_contract.py -v

注意：这些测试需要后端服务运行在 localhost:9011
"""

import os
import uuid
from typing import Any

import httpx
import pytest

# =============================================================================
# 配置
# =============================================================================

BASE_URL = "https://localhost:9011"
API_PREFIX = "/api/v1"
E2E_REQUEST_TIMEOUT = float(os.getenv("LEADER_E2E_REQUEST_TIMEOUT", "120"))


# =============================================================================
# Helper Functions
# =============================================================================


def api_url(path: str) -> str:
    """构建完整的 API URL"""
    return f"{BASE_URL}{API_PREFIX}{path}"


def generate_client_request_id() -> str:
    """生成唯一的客户端请求 ID"""
    return f"test_{uuid.uuid4().hex[:12]}"


def make_submit_request(
    query: str,
    session_id: str | None = None,
    mode: str = "direct_rpc",
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """构建 /submit 请求体"""
    payload = {
        "query": query,
        "mode": mode,
        "clientRequestId": client_request_id or generate_client_request_id(),
    }
    if session_id:
        payload["sessionId"] = session_id
    return payload


# =============================================================================
# Test: /submit Endpoint Response Format
# =============================================================================


class TestSubmitEndpoint:
    """测试 /submit 端点的响应格式"""

    def test_submit_returns_common_response_structure(self):
        """
        验证 /submit 返回 CommonResponse 格式：
        {
            "result": {...} | null,
            "error": {...} | null
        }
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            payload = make_submit_request(query="你好")
            response = client.post(api_url("/submit"), json=payload)

            # 应该成功（2xx）
            assert response.status_code in [
                200,
                201,
            ], f"Status: {response.status_code}, Body: {response.text}"

            data = response.json()

            # 必须有 result 或 error 字段
            assert "result" in data or "error" in data, f"Response missing result/error: {data}"

            # 成功时 result 应该存在
            if response.status_code == 200:
                assert data.get("result") is not None, f"Success response should have result: {data}"
                assert data.get("error") is None, f"Success response should not have error: {data}"

    def test_submit_result_has_required_fields(self):
        """
        验证 SubmitResult 包含必需字段：
        - sessionId: 会话 ID
        - mode: 执行模式
        - activeTaskId: 活跃任务 ID
        - acceptedAt: 受理时间
        - externalStatus: 对外状态
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            payload = make_submit_request(query="北京有哪些好吃的？")
            response = client.post(api_url("/submit"), json=payload)

            assert response.status_code == 200, f"Status: {response.status_code}, Body: {response.text}"

            data = response.json()
            result = data.get("result", {})

            # 检查必需字段（使用 camelCase）
            required_fields = [
                "sessionId",
                "mode",
                "activeTaskId",
                "acceptedAt",
                "externalStatus",
            ]
            for field in required_fields:
                assert field in result, f"SubmitResult missing required field '{field}': {result}"

            # 验证字段类型
            assert isinstance(result["sessionId"], str), f"sessionId should be string: {result['sessionId']}"
            assert result["mode"] in [
                "direct_rpc",
                "group",
            ], f"Invalid mode: {result['mode']}"
            assert isinstance(result["activeTaskId"], str), f"activeTaskId should be string: {result['activeTaskId']}"
            assert isinstance(result["externalStatus"], str), (
                f"externalStatus should be string: {result['externalStatus']}"
            )

            # externalStatus 应该是有效值之一
            valid_statuses = [
                "pending",
                "running",
                "awaiting_input",
                "completed",
                "failed",
                "cancelled",
            ]
            assert result["externalStatus"] in valid_statuses, f"Invalid externalStatus: {result['externalStatus']}"

            print("\n✓ Submit successful:")
            print(f"  sessionId: {result['sessionId']}")
            print(f"  activeTaskId: {result['activeTaskId']}")
            print(f"  externalStatus: {result['externalStatus']}")


# =============================================================================
# Test: /result Endpoint Response Format
# =============================================================================


class TestResultEndpoint:
    """测试 /result 端点的响应格式"""

    def test_result_returns_common_response_structure(self):
        """
        验证 /result 返回 CommonResponse 格式：
        {
            "result": LeaderResult | null,
            "error": CommonError | null
        }
        """
        # 先创建一个 session
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            submit_payload = make_submit_request(query="测试查询")
            submit_response = client.post(api_url("/submit"), json=submit_payload)

            assert submit_response.status_code == 200, f"Submit failed: {submit_response.text}"

            submit_data = submit_response.json()
            session_id = submit_data.get("result", {}).get("sessionId")

            assert session_id, f"No sessionId in submit response: {submit_data}"

            # 获取 result
            result_response = client.get(api_url(f"/result/{session_id}"))

            assert result_response.status_code == 200, (
                f"Result status: {result_response.status_code}, Body: {result_response.text}"
            )

            data = result_response.json()

            # 必须有 result 或 error 字段
            assert "result" in data or "error" in data, f"Response missing result/error: {data}"

            print(f"\n✓ Result response structure: {list(data.keys())}")
            print(f"  result is None: {data.get('result') is None}")
            print(f"  error is None: {data.get('error') is None}")

    def test_result_has_leader_result_fields(self):
        """
        验证成功时 result 包含 LeaderResult 必需字段：
        - sessionId, mode, createdAt, updatedAt, touchedAt
        - ttlSeconds, expiresAt
        - baseScenario
        - userResult
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            # 先创建 session
            submit_payload = make_submit_request(query="推荐北京的餐厅")
            submit_response = client.post(api_url("/submit"), json=submit_payload)

            assert submit_response.status_code == 200, f"Submit failed: {submit_response.text}"

            submit_data = submit_response.json()
            session_id = submit_data.get("result", {}).get("sessionId")

            # 获取 result
            result_response = client.get(api_url(f"/result/{session_id}"))

            assert result_response.status_code == 200, f"Result failed: {result_response.text}"

            data = result_response.json()

            # 关键断言：result 不应该是 null
            result = data.get("result")
            if result is None:
                print("\n❌ BUG DETECTED: result is null!")
                print(f"  Full response: {data}")
                pytest.fail(f"/result endpoint returns null result: {data}")

            # 检查 LeaderResult 必需字段
            required_fields = [
                "sessionId",
                "mode",
                "createdAt",
                "updatedAt",
                "touchedAt",
                "ttlSeconds",
                "expiresAt",
                "baseScenario",
                "userResult",
            ]

            missing_fields = [f for f in required_fields if f not in result]
            if missing_fields:
                print(f"\n❌ Missing fields in LeaderResult: {missing_fields}")
                print(f"  Available fields: {list(result.keys())}")
                pytest.fail(f"LeaderResult missing required fields: {missing_fields}")

            print("\n✓ LeaderResult has all required fields")
            print(f"  sessionId: {result.get('sessionId')}")
            print(f"  mode: {result.get('mode')}")
            print(f"  activeTask: {result.get('activeTask')}")
            print(f"  userResult type: {result.get('userResult', {}).get('type')}")


# =============================================================================
# Test: Full Submit -> Result Flow
# =============================================================================


class TestSubmitResultFlow:
    """测试完整的 submit -> result 流程"""

    def test_submit_then_poll_result(self):
        """
        验证完整流程：
        1. POST /submit 创建会话和任务
        2. GET /result/{session_id} 获取结果
        3. result 应该包含有效的 LeaderResult
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            print("\n" + "=" * 60)
            print("Step 1: Submit query")
            print("=" * 60)

            submit_payload = make_submit_request(query="帮我查找北京的酒店")
            submit_response = client.post(api_url("/submit"), json=submit_payload)

            print(f"  Status: {submit_response.status_code}")
            submit_data = submit_response.json()
            print(f"  Response keys: {list(submit_data.keys())}")

            assert submit_response.status_code == 200, f"Submit failed: {submit_response.text}"

            submit_result = submit_data.get("result")
            assert submit_result is not None, f"Submit result is null: {submit_data}"

            session_id = submit_result.get("sessionId")
            active_task_id = submit_result.get("activeTaskId")
            external_status = submit_result.get("externalStatus")

            print(f"  sessionId: {session_id}")
            print(f"  activeTaskId: {active_task_id}")
            print(f"  externalStatus: {external_status}")

            print("\n" + "=" * 60)
            print("Step 2: Get result")
            print("=" * 60)

            result_response = client.get(api_url(f"/result/{session_id}"))

            print(f"  Status: {result_response.status_code}")
            result_data = result_response.json()
            print(f"  Response keys: {list(result_data.keys())}")
            print(f"  result is None: {result_data.get('result') is None}")
            print(f"  error is None: {result_data.get('error') is None}")

            assert result_response.status_code == 200, f"Result failed: {result_response.text}"

            # 关键断言
            leader_result = result_data.get("result")
            if leader_result is None:
                print("\n" + "=" * 60)
                print("❌ BUG: /result returns {result: null, error: null}")
                print("=" * 60)
                print(f"Full response: {result_data}")
                pytest.fail(
                    f"/result endpoint returns null result. "
                    f"This indicates the backend is not properly constructing LeaderResult. "
                    f"Response: {result_data}"
                )

            # 验证 LeaderResult 内容
            print(f"\n  LeaderResult fields: {list(leader_result.keys())}")

            assert leader_result.get("sessionId") == session_id, "sessionId mismatch"

            # activeTask 应该存在（因为刚刚创建了任务）
            active_task = leader_result.get("activeTask")
            if active_task:
                print(f"  activeTask.id: {active_task.get('id')}")
                print(f"  activeTask.status: {active_task.get('status')}")

            # userResult 应该存在
            user_result = leader_result.get("userResult")
            if user_result:
                print(f"  userResult.type: {user_result.get('type')}")
                print(f"  userResult.dataItems count: {len(user_result.get('dataItems', []))}")

            print("\n✓ Full flow completed successfully")


# =============================================================================
# Test: Error Responses
# =============================================================================


class TestErrorResponses:
    """测试错误响应格式"""

    def test_result_not_found_returns_error(self):
        """验证查询不存在的 session 返回正确的错误格式"""
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            fake_session_id = f"nonexistent_{uuid.uuid4().hex[:8]}"
            response = client.get(api_url(f"/result/{fake_session_id}"))

            # 应该是 404
            assert response.status_code == 404, f"Expected 404, got {response.status_code}"

            # 检查响应格式（可能是 HTTPException detail 格式）
            data = response.json()
            print(f"\n404 Response: {data}")

            # FastAPI HTTPException 返回 {"detail": {...}}
            # 或者应该返回 {"result": null, "error": {...}}
            if "detail" in data:
                # FastAPI 默认格式
                assert "code" in data["detail"] or "message" in data["detail"]
            elif "error" in data:
                # CommonResponse 格式
                assert data.get("error") is not None
                assert data.get("result") is None

    def test_submit_invalid_request(self):
        """验证无效请求返回正确的错误格式"""
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            # 缺少必需字段
            invalid_payload = {"query": "test"}  # 缺少 mode 和 clientRequestId

            response = client.post(api_url("/submit"), json=invalid_payload)

            print(f"\nInvalid request response status: {response.status_code}")
            print(f"Response: {response.json()}")

            # 应该是 422 (Validation Error) 或 400
            assert response.status_code in [
                400,
                422,
            ], f"Expected 400/422, got {response.status_code}"


# =============================================================================
# Main
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
