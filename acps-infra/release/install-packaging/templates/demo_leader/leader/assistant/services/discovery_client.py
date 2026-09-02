"""
Leader Agent Platform - Discovery Client

本模块封装 ADP（Agent Discovery Protocol）SDK，对外提供发现服务客户端。
用于根据维度描述动态查询发现服务器，返回匹配的 Partner 候选列表。
"""

import logging

import httpx
from acps_sdk.adp import (
    ADPError,
    DiscoveryFilter,
    DiscoveryRequest,
    DiscoveryResponse,
    validate_discovery_request,
)

from ..config import settings

logger = logging.getLogger(__name__)


class DiscoveryClient:
 """
    ADP 发现服务客户端。

    通过 HTTP POST 调用发现服务器的 /discover 接口，
    返回匹配维度能力需求的 Partner 候选列表。
 """

    def __init__(
        self,
        server_base_url: str | None = None,
        timeout: float | None = None,
        default_limit: int | None = None,
    ):
        discovery_config = settings.get("discovery", {})
        raw_url = server_base_url if server_base_url is not None else discovery_config.get("server_base_url", "")
        self._server_base_url = raw_url.rstrip("/")
        self._timeout = timeout if timeout is not None else discovery_config.get("timeout", 10)
        self._default_limit = default_limit if default_limit is not None else discovery_config.get("limit", 5)

    @property
    def is_configured(self) -> bool:
 """发现服务是否已配置（有有效的 server_base_url）。"""
        return bool(self._server_base_url)

    async def discover(
        self,
        query: str,
        limit: int | None = None,
        discovery_filter: DiscoveryFilter | None = None,
    ) -> DiscoveryResponse:
 """
        发送发现请求。

        Args:
            query: 自然语言能力查询
            limit: 最大返回数量
            discovery_filter: 结构化过滤条件

        Returns:
            DiscoveryResponse

        Raises:
            DiscoveryClientError: 网络或协议错误
 """
        if not self.is_configured:
            raise DiscoveryClientError("发现服务未配置 server_base_url")

        request = DiscoveryRequest(
            type="explicit",
            query=query,
            limit=limit or self._default_limit,
            filter=discovery_filter,
        )

 # SDK 提供的请求参数校验（提前捕获 query 为空、filter 嵌套过深等问题）
        try:
            validate_discovery_request(request)
        except ADPError as e:
            raise DiscoveryClientError(f"请求参数校验失败: {e.message}") from e

 # 按照 ADP 规范：请求 URL = {ADP_BASE_URL}/discover
        url = self._server_base_url + "/discover"
        payload = request.to_dict

        logger.info(f"[ADP] POST {url}, query={query!r}, limit={limit}")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status
                raw_json = resp.json
                logger.debug(
                    f"[ADP] Response: status={resp.status_code}, agents={len(raw_json.get('result', {}).get('agents', []))} group(s)"
                )
                response = DiscoveryResponse.from_dict(raw_json)

 # 解析协议级错误（HTTP 200 但 body 含 error）
                if response.is_error:
                    adp_error = response.get_adp_error
                    raise DiscoveryClientError(
                        f"发现服务返回协议错误: {response.error.message}",
                        adp_error=adp_error,
                    )

                return response
        except httpx.HTTPStatusError as e:
            logger.error(f"[ADP] HTTP error {e.response.status_code}: {e}")
            raise DiscoveryClientError(f"发现服务返回错误 {e.response.status_code}") from e
        except httpx.TimeoutException as e:
            logger.error(f"[ADP] Request timeout: {e}")
            raise DiscoveryClientError("发现服务请求超时") from e
        except Exception as e:
            logger.error(f"[ADP] Unexpected error: {e}")
            raise DiscoveryClientError(f"发现服务调用失败: {e}") from e

    async def discover_for_dimension(
        self,
        dimension_name: str,
        dimension_description: str,
        limit: int | None = None,
        discovery_filter: DiscoveryFilter | None = None,
    ) -> DiscoveryResponse:
 """
        为指定维度发起发现查询。

        自动将维度名称和描述组装为自然语言查询。

        Args:
            dimension_name: 维度名称
            dimension_description: 维度描述
            limit: 最大返回数量
            discovery_filter: 额外过滤条件

        Returns:
            DiscoveryResponse
 """
        query = f"我需要一个能提供「{dimension_name}」服务的智能体。{dimension_description}"
        return await self.discover(
            query=query,
            limit=limit,
            discovery_filter=discovery_filter,
        )


class DiscoveryClientError(Exception):
 """发现服务客户端异常。

    Attributes:
        adp_error: 关联的 ADP 协议错误（如有）。可用于判断错误类型：
                   adp_error.is_retryable — 限流类错误，可稍后重试
                   adp_error.is_redirect — 重定向，应切换服务器
                   adp_error.is_client_error — 客户端参数错误
                   adp_error.is_forward_error — 转发链路错误
 """

    def __init__(self, message: str, adp_error=None):
        super.__init__(message)
        self.adp_error = adp_error


# =============================================================================
# 单例
# =============================================================================

_discovery_client_instance: DiscoveryClient | None = None


def get_discovery_client -> DiscoveryClient:
 """获取 DiscoveryClient 单例。"""
    global _discovery_client_instance
    if _discovery_client_instance is None:
        _discovery_client_instance = DiscoveryClient
    return _discovery_client_instance
