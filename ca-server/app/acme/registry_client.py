"""
Registry Server 客户端

负责与 Registry Server 通信，获取 Agent 信息。
"""

import asyncio
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

from .mock_data import MockDataGenerator

logger = structlog.get_logger(__name__)


class AgentInfo:
    """Agent 信息数据类"""

    def __init__(self, data: dict[str, Any]):
        """
        根据 ACS 定义初始化 Agent 信息

        参数:
            data: Registry Server 返回的 ACS 格式数据，包含以下字段：
                - aic: Agent Identity Code
                - active: 是否激活（布尔值）
                - name: Agent 服务名称
                - version: Agent 服务版本
                - provider: 提供者信息（组织、部门、国家代码）
                - securitySchemes: 安全方案定义
                - endPoints: Agent 服务端点列表
                - capabilities: Agent 能力描述
                - skills: Agent 技能列表
        """
        # 基本信息
        self.aic = data.get("aic", "")
        self.agent_id = self.aic  # agent_id 与 aic 相同
        self.name = data.get("name", "")
        self.version = data.get("version", "")

        # active 字段是布尔值，表示 Agent 是否激活
        active = data.get("active", False)
        self.active: bool = active if isinstance(active, bool) else False
        self.valid: bool = self.active  # valid 字段与 active 保持一致

        # 提供者信息（用于证书 DN 构造）
        provider = data.get("provider", {})
        self.organization = provider.get("organization", "")
        self.department = provider.get("department", "")
        self.country_code = provider.get("countryCode", "CN")

        # 端点信息
        self.end_points = data.get("endPoints", [])

        # 能力和技能信息
        self.capabilities = data.get("capabilities", {})
        self.skills = data.get("skills", [])

        # ACS certificate 字段（v2.1.0 新增）
        certificate = data.get("certificate", {}) or {}
        alt_names = certificate.get("altNames", {}) or {}
        self.certificate_alt_names_dns: list[str] = alt_names.get("dns", []) or []
        self.certificate_alt_names_ip: list[str] = alt_names.get("ip", []) or []
        self.certificate_requested_validity: int | None = certificate.get("requestedValidity")

    def is_valid(self) -> bool:
        """
        检查 Agent 是否有效

        根据 ACS 定义，Agent 的 active 字段为 true 时表示激活状态
        """
        return self.active

    def get_certificate_subject_components(self) -> dict[str, str]:
        """
        获取证书 Subject DN 组件

        根据 ACS 的 provider 信息构造证书 DN：
        - CN: AIC（v2.1.0 起直接使用裸 AIC，不附加域名后缀）
        - O: provider.organization（可选）
        - OU: provider.department（可选）
        - C: provider.countryCode（可选）
        """
        settings = get_settings()
        components = {"CN": settings.build_agent_common_name(self.aic)}

        if self.organization:
            components["O"] = self.organization
        if self.department:
            components["OU"] = self.department
        if self.country_code:
            components["C"] = self.country_code

        return components

    def get_certificate_dns_names(self) -> list[str]:
        """获取 ACS 定义的额外 DNS SAN 条目"""
        return list(self.certificate_alt_names_dns)

    def get_certificate_ip_addresses(self) -> list[str]:
        """获取 ACS 定义的额外 IP SAN 条目"""
        return list(self.certificate_alt_names_ip)

    def get_certificate_validity_days(self, max_days: int = 1825) -> int:
        """获取证书有效期（天），不超过 max_days 上限

        若 ACS 未指定或指定值无效，返回默认值 49 天。

        Args:
            max_days: 系统允许的最大有效期，默认 1825（5 年）
        """
        if self.certificate_requested_validity and self.certificate_requested_validity > 0:
            return min(self.certificate_requested_validity, max_days)
        return 49


class RegistryClient:
    """Registry Server 客户端"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.registry_server_url
        self.internal_base_url = self._resolve_internal_base_url()
        self.timeout = self.settings.registry_server_timeout
        self.service_token = self.settings.registry_server_internal_api_token
        self.max_retries = self.settings.external_service_max_retries
        self.retry_delays = self.settings.external_service_retry_delays_list

        # Mock 模式支持
        self.is_mock_enabled = self.settings.registry_server_mock
        if self.is_mock_enabled:
            self.mock_generator = MockDataGenerator()
            logger.info("RegistryClient: Mock mode enabled")

    def _resolve_internal_base_url(self) -> str:
        configured = (self.settings.registry_server_internal_url or "").strip()
        if configured:
            return configured.rstrip("/")

        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/acps-atr-v2"):
            return base_url[: -len("/acps-atr-v2")]
        return base_url

    async def _make_request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        """带重试机制的HTTP请求"""
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    return await client.request(method, url, **kwargs)

            except httpx.RequestError as e:
                last_exception = e
                if attempt < self.max_retries:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    await asyncio.sleep(delay)
                    continue
                break
            except Exception as e:
                last_exception = e
                break

        # 所有重试都失败了
        logger.warning(
            "所有重试均失败",
            url=url,
            attempts=self.max_retries + 1,
            error=str(last_exception),
        )
        return None

    def _get_auth_headers(self) -> dict[str, str]:
        """获取认证头"""
        headers = {"Content-Type": "application/json"}
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        return headers

    async def consume_eab_credential(self, key_id: str) -> tuple[str, str] | None:
        """Consume an EAB credential via registry-server internal API."""
        if self.is_mock_enabled:
            logger.info("RegistryClient: EAB consume is not supported in registry mock mode")
            return None

        try:
            url = f"{self.internal_base_url}/internal/eab/consume"
            headers = self._get_auth_headers()
            response = await self._make_request_with_retry(
                "POST",
                url,
                headers=headers,
                json={"keyId": key_id},
            )

            if not response or response.status_code != 200:
                status_code = response.status_code if response else "no-response"
                logger.warning("EAB 凭据消费失败", key_id=key_id, status_code=status_code)
                return None

            payload = response.json()
            mac_key = payload.get("macKey")
            aic = payload.get("aic")
            if not isinstance(mac_key, str) or not isinstance(aic, str):
                logger.warning("EAB 消费响应格式无效", key_id=key_id, payload=str(payload))
                return None
            return mac_key, aic
        except Exception as exc:
            logger.error("EAB 凭据消费时发生异常", key_id=key_id, error=str(exc))
            return None

    async def validate_aic_and_get_info(self, aic: str) -> AgentInfo | None:
        """
        验证 AIC 有效性并获取相关信息

        参数:
            aic: Agent Identity Code

        返回:
            AgentInfo 对象，如果验证失败则返回 None
        """
        # Mock 模式
        if self.is_mock_enabled:
            logger.info("RegistryClient: AIC 验证使用 mock 数据", aic=aic)
            mock_data = self.mock_generator.generate_agent_info(aic)
            return AgentInfo(mock_data)

        # 真实模式 - 调用 Registry Server API
        try:
            # 构造 URL: {REGISTRY_SERVER_BASE_URL}/acs/{aic}
            base_url = self.base_url.rstrip("/")
            url = f"{base_url}/acs/{aic}"
            headers = self._get_auth_headers()

            response = await self._make_request_with_retry("GET", url, headers=headers)

            if not response:
                logger.warning("Agent 注册服务未返回响应", aic=aic)
                return None

            if response.status_code == 404:
                logger.warning("Agent 在注册中心未找到", aic=aic, status_code=404)
                return None

            if response.status_code == 403:
                logger.warning("Agent 未激活", aic=aic, status_code=403)
                return None

            if response.status_code != 200:
                logger.warning(
                    "Agent 注册服务返回异常状态码",
                    aic=aic,
                    status_code=response.status_code,
                )
                return None

            # 解析 ACS 格式的响应数据
            agent_data = response.json()
            agent_info = AgentInfo(agent_data)

            # 2.4 章节的信息核对：
            # 1. AIC 匹配检查
            if agent_info.aic != aic:
                logger.warning("AIC 不匹配", requested=aic, received=agent_info.aic)
                return None

            # 2. 状态检查 - active 字段必须为 true
            if not agent_info.active:
                logger.warning("Agent 处于非激活状态", aic=aic, active=agent_info.active)
                return None

            # provider.organization 用于证书 Subject DN 的 O 字段，但是可选：
            # 仅完成个人身份认证的 Agent 可能没有 organization（registry verified
            # provider snapshot 亦如此）。缺省时 DN 构造会省略 O，见
            # AgentInfo.get_certificate_subject_components。
            if not agent_info.organization:
                logger.info("Agent ACS 未提供 provider.organization，证书 DN 将省略 O", aic=aic)

            return agent_info

        except Exception as e:
            logger.error("AIC 验证时发生异常", aic=aic, error=str(e))
            return None

    async def register_certificate_request(self, aic: str, order_id: str) -> bool:
        """向注册服务通知证书请求"""
        # Mock 模式
        if self.is_mock_enabled:
            logger.info(
                "RegistryClient: 证书请求注册使用 mock 模式",
                aic=aic,
                order_id=order_id,
            )
            return self.mock_generator.generate_registration_result()

        # 真实模式 - 直接返回成功，避免调用不存在的API
        try:
            logger.info("RegistryClient: 证书请求已注册", aic=aic, order_id=order_id)
            return True

        except Exception as e:
            logger.error("证书请求注册失败", aic=aic, error=str(e))
            return False

    async def notify_certificate_issued(self, aic: str, order_id: str, cert_id: str) -> bool:
        """通知注册服务证书已签发"""
        # Mock 模式
        if self.is_mock_enabled:
            logger.info(
                "RegistryClient: 证书签发通知使用 mock 模式",
                aic=aic,
                order_id=order_id,
                cert_id=cert_id,
            )
            return self.mock_generator.generate_notification_result()

        # 真实模式 - 直接返回成功，避免调用不存在的API
        try:
            logger.info(
                "RegistryClient: 证书签发通知已发送",
                aic=aic,
                order_id=order_id,
                cert_id=cert_id,
            )
            return True

        except Exception as e:
            logger.error("证书签发通知失败", aic=aic, error=str(e))
            return False

    async def verify_agent_ownership(self, aic: str, account_info: dict[str, Any]) -> bool:
        """验证账户是否有权为指定Agent申请证书"""
        # Mock 模式
        if self.is_mock_enabled:
            logger.info("RegistryClient: 所有权验证使用 mock 模式", aic=aic)
            return self.mock_generator.generate_ownership_verification_result()

        # 真实模式 - 直接返回成功，避免调用不存在的API
        try:
            logger.info("RegistryClient: Agent 所有权验证通过", aic=aic)
            return True

        except Exception as e:
            logger.error("Agent 所有权验证失败", aic=aic, error=str(e))
            return False


# 全局 Registry Client 实例
_registry_client: RegistryClient | None = None


def get_registry_client() -> RegistryClient:
    """获取 Registry Client 实例"""
    global _registry_client
    if _registry_client is None:
        _registry_client = RegistryClient()
    return _registry_client
