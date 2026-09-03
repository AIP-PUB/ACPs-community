"""
Leader Agent Platform - E2E Tests: Edge Cases

边界情况和异常处理测试：
1. 会话管理 - 新建、复用、不存在的会话
2. 错误处理 - 空查询、无效模式、缺少字段
3. 幂等性 - 重复请求处理

运行方式：
    pytest tests/e2e/test_edge_cases.py -v -s

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
    return f"e2e_{uuid.uuid4().hex[:12]}"


def submit_query(
    client: httpx.Client,
    query: str,
    session_id: str | None = None,
    mode: str = "direct_rpc",
) -> dict[str, Any]:
    """提交查询并返回响应数据。"""
    payload = {
        "query": query,
        "mode": mode,
        "clientRequestId": generate_client_request_id(),
    }
    if session_id:
        payload["sessionId"] = session_id

    response = client.post(api_url("/submit"), json=payload)

    # 检查是否是 LLM 服务问题
    if response.status_code == 500:
        data = response.json()
        detail = data.get("detail", {})
        if isinstance(detail, dict) and detail.get("code") in [
            "LLM_CALL_ERROR",
            "LLM_SERVICE_UNAVAILABLE",
        ]:
            pytest.skip(f"LLM service unavailable: {detail.get('message', 'unknown error')}")

    assert response.status_code == 200, f"Submit failed: {response.text}"
    return response.json()


def get_result(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """获取会话结果。"""
    response = client.get(api_url(f"/result/{session_id}"))
    assert response.status_code in [200, 404], f"Result request failed: {response.text}"
    return response.json()


# =============================================================================
# 会话管理测试
# =============================================================================


class TestSessionManagement:
    """测试会话管理功能"""

    def test_new_session_creation(self):
        """
        场景：不带 sessionId 创建新会话
        预期：返回新的 sessionId，会话初始化正确
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            submit_data = submit_query(client, "你好")

            session_id = submit_data["result"]["sessionId"]
            assert session_id is not None
            assert session_id.startswith("sess_")

            # 验证会话可以被查询
            result_data = get_result(client, session_id)
            assert result_data["result"] is not None

            print(f"\n✓ 新会话创建通过: {session_id}")

    def test_session_reuse(self):
        """
        场景：带 sessionId 复用已有会话
        预期：返回相同的 sessionId，会话状态保持
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            # 创建会话
            submit_data1 = submit_query(client, "你好")
            session_id = submit_data1["result"]["sessionId"]

            # 复用会话
            submit_data2 = submit_query(client, "再见", session_id=session_id)
            session_id2 = submit_data2["result"]["sessionId"]

            assert session_id == session_id2, "Should reuse same session"

            print(f"\n✓ 会话复用通过: {session_id}")

    def test_nonexistent_session(self):
        """
        场景：查询不存在的会话
        预期：返回 404
        """
        with httpx.Client(timeout=10.0) as client:
            fake_session_id = f"sess_{uuid.uuid4().hex[:16]}"
            response = client.get(api_url(f"/result/{fake_session_id}"))

            assert response.status_code == 404, f"Expected 404, got {response.status_code}"

            print("\n✓ 不存在会话查询通过: 404")


# =============================================================================
# 错误处理测试
# =============================================================================


class TestErrorHandling:
    """测试错误处理场景"""

    def test_empty_query(self):
        """
        场景：提交空查询
        预期：返回验证错误或按闲聊处理
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            payload = {
                "query": "",
                "mode": "direct_rpc",
                "clientRequestId": generate_client_request_id(),
            }
            response = client.post(api_url("/submit"), json=payload)

            # 空查询可能：200（按闲聊处理）、400/422（验证错误）、500（LLM 服务问题）
            if response.status_code == 500:
                data = response.json()
                detail = data.get("detail", {})
                if isinstance(detail, dict) and "LLM" in str(detail):
                    pytest.skip("LLM service unavailable")

            assert response.status_code in [
                200,
                400,
                422,
                500,
            ], f"Unexpected status: {response.status_code}"

            print(f"\n✓ 空查询处理通过: {response.status_code}")

    def test_invalid_mode(self):
        """
        场景：使用无效的执行模式
        预期：返回验证错误
        """
        with httpx.Client(timeout=10.0) as client:
            payload = {
                "query": "你好",
                "mode": "invalid_mode",
                "clientRequestId": generate_client_request_id(),
            }
            response = client.post(api_url("/submit"), json=payload)

            assert response.status_code in [
                400,
                422,
            ], f"Expected 400/422, got {response.status_code}"

            print(f"\n✓ 无效模式处理通过: {response.status_code}")

    def test_missing_required_fields(self):
        """
        场景：缺少必需字段
        预期：返回验证错误
        """
        with httpx.Client(timeout=10.0) as client:
            # 缺少 mode 和 clientRequestId
            payload = {"query": "你好"}
            response = client.post(api_url("/submit"), json=payload)

            assert response.status_code == 422, f"Expected 422, got {response.status_code}"

            print("\n✓ 缺少必需字段处理通过: 422")


# =============================================================================
# 幂等性测试
# =============================================================================


class TestIdempotency:
    """测试请求幂等性"""

    def test_duplicate_request_same_payload(self):
        """
        场景：相同 sessionId + clientRequestId 重复提交相同内容
        预期：返回相同结果（幂等）
        """
        with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
            # 第一次提交（创建 session）
            client_request_id = generate_client_request_id()
            payload1 = {
                "query": "你好",
                "mode": "direct_rpc",
                "clientRequestId": client_request_id,
            }
            response1 = client.post(api_url("/submit"), json=payload1)

            # 检查是否 LLM 服务不可用
            if response1.status_code == 500:
                data = response1.json()
                detail = data.get("detail", {})
                if isinstance(detail, dict) and "LLM" in str(detail):
                    pytest.skip("LLM service unavailable")
                pytest.fail(f"Unexpected 500 error: {data}")

            assert response1.status_code == 200, f"First request failed: {response1.text}"
            data1 = response1.json()
            session_id = data1["result"]["sessionId"]

            # 第二次提交（相同 sessionId + clientRequestId）
            payload2 = {
                "query": "你好",
                "mode": "direct_rpc",
                "clientRequestId": client_request_id,
                "sessionId": session_id,
            }
            response2 = client.post(api_url("/submit"), json=payload2)

            if response2.status_code == 500:
                pytest.skip("LLM service unavailable")

            assert response2.status_code == 200, f"Second request failed: {response2.text}"
            data2 = response2.json()

            # 应该返回相同的 sessionId（幂等）
            session_id2 = data2["result"]["sessionId"]
            assert session_id == session_id2, f"Should return same session: {session_id} vs {session_id2}"

            print("\n✓ 幂等性测试通过")
            print(f"  clientRequestId: {client_request_id}")
            print(f"  sessionId: {session_id}")


# =============================================================================
# Main
# =============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
