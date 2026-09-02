"""配置加载与管理。

使用 tomllib 读取 config/ 下的 TOML 文件，使用 pydantic-settings 加载环境变量中的敏感配置。
加载顺序：default.toml → {APP_ENV}.toml，后者覆盖前者中的同名项。

敏感/部署专用配置（数据库连接串、服务令牌等）通过环境变量或 .env 文件提供。
非敏感业务配置（日志格式、服务器参数、CA 参数、证书路径等）通过 TOML 文件管理。
"""

import ipaddress
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_SOURCE_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _resolve_config_dir() -> Path:
    """解析运行时 config 目录，兼容源码树与 wheel 安装目录。"""

    working_dir_config = Path.cwd() / "config"
    if working_dir_config.is_dir():
        return working_dir_config

    return _SOURCE_CONFIG_DIR


def _validate_absolute_http_url(setting_name: str, url: str) -> str:
    """校验 URL 为绝对 http(s) 地址，并返回标准化主机名"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{setting_name} must be an absolute http(s) URL")

    hostname = parsed.hostname
    if hostname is None:
        raise ValueError(f"{setting_name} must include a hostname")

    return hostname.lower()


def _is_loopback_host(hostname: str) -> bool:
    """判断主机名是否为本机回环地址"""
    if hostname == "localhost":
        return True

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    return address.is_loopback or address.is_unspecified


def _is_placeholder_host(hostname: str) -> bool:
    """判断主机名是否仍为文档占位域名"""
    return hostname == "example.com" or hostname.endswith(".example.com")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个字典，override 覆盖 base 中的同名项"""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_toml_config(env: str = "development") -> dict[str, Any]:
    """加载 TOML 配置文件：default.toml → {env}.toml"""
    config_dir = _resolve_config_dir()
    default_path = config_dir / "default.toml"
    env_path = config_dir / f"{env}.toml"

    config: dict[str, Any] = {}
    if default_path.exists():
        with default_path.open("rb") as f:
            config = tomllib.load(f)
    if env_path.exists():
        with env_path.open("rb") as f:
            env_config = tomllib.load(f)
        config = _deep_merge(config, env_config)
    return config


class Settings(BaseSettings):
    """应用设置。环境变量承载敏感数据（数据库、服务令牌等），TOML 承载非敏感业务配置（含证书路径）"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── 敏感/部署专用配置（从环境变量 / .env 加载） ──

    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    registry_server_internal_url: str = Field(default="", validation_alias="REGISTRY_SERVER_INTERNAL_URL")
    registry_server_internal_api_token: str = Field(
        default="",
        validation_alias="REGISTRY_SERVER_INTERNAL_API_TOKEN",
    )
    ca_server_internal_api_token: str = Field(default="", validation_alias="CA_SERVER_INTERNAL_API_TOKEN")
    ca_server_admin_api_token: str = Field(default="", validation_alias="CA_SERVER_ADMIN_API_TOKEN")
    app_env: str = Field(default="development", validation_alias="APP_ENV")

    # ── 非敏感业务配置的环境变量 override（默认仍以 TOML 为准） ──

    registry_server_url_override: str = Field(default="", validation_alias="REGISTRY_SERVER_URL")
    registry_server_timeout_override: int | None = Field(default=None, validation_alias="REGISTRY_SERVER_TIMEOUT")
    registry_server_mock_override: bool | None = Field(default=None, validation_alias="REGISTRY_SERVER_MOCK")
    acme_directory_url_override: str = Field(default="", validation_alias="ACME_DIRECTORY_URL")
    ocsp_responder_url_override: str = Field(default="", validation_alias="OCSP_RESPONDER_URL")
    crl_distribution_point_url_override: str = Field(default="", validation_alias="CRL_DISTRIBUTION_POINT_URL")

    # ── TOML 配置（运行时从文件加载） ──
    _toml: dict[str, Any] = {}

    def model_post_init(self, __context: Any) -> None:
        """加载并合并 TOML 配置"""
        object.__setattr__(self, "_toml", load_toml_config(self.app_env))
        self.validate_certificate_discovery_urls()

    # ── TOML 属性访问 ──

    @property
    def app_name(self) -> str:
        return str(self._toml.get("app", {}).get("name", "Agent CA API"))

    @property
    def app_version(self) -> str:
        return str(self._toml.get("app", {}).get("version", "2.2.0"))

    @property
    def docs_enabled(self) -> bool:
        return bool(self._toml.get("app", {}).get("docs_enabled", False))

    @property
    def log_level(self) -> str:
        return str(self._toml.get("logging", {}).get("level", "INFO"))

    @property
    def log_format(self) -> str:
        return str(self._toml.get("logging", {}).get("format", "json"))

    @property
    def uvicorn_host(self) -> str:
        return str(self._toml.get("server", {}).get("host", "0.0.0.0"))  # nosec B104

    @property
    def uvicorn_port(self) -> int:
        return int(self._toml.get("server", {}).get("port", 9003))

    @property
    def uvicorn_reload(self) -> bool:
        return bool(self._toml.get("server", {}).get("reload", False))

    @property
    def uvicorn_log_level(self) -> str:
        return str(self._toml.get("server", {}).get("uvicorn_log_level", "info"))

    @property
    def ca_cert_path(self) -> str:
        return str(self._toml.get("ca", {}).get("cert_path", "certs/ca.crt"))

    @property
    def ca_key_path(self) -> str:
        return str(self._toml.get("ca", {}).get("key_path", "certs/ca.key"))

    @property
    def ca_chain_path(self) -> str:
        return str(self._toml.get("ca", {}).get("chain_path", "certs/ca-chain.pem"))

    @property
    def trust_bundle_path(self) -> str:
        return str(self._toml.get("ca", {}).get("trust_bundle_path", "certs/trust-bundle.pem"))

    @property
    def max_certificate_validity_days(self) -> int:
        return int(self._toml.get("ca", {}).get("max_certificate_validity_days", 1825))

    @max_certificate_validity_days.setter
    def max_certificate_validity_days(self, value: int) -> None:
        ca_config = self._toml.setdefault("ca", {})
        ca_config["max_certificate_validity_days"] = int(value)

    @property
    def acme_directory_url(self) -> str:
        if self.acme_directory_url_override.strip():
            return self.acme_directory_url_override.strip()
        return str(self._toml.get("ca", {}).get("acme_directory_url", "http://localhost:9003/acps-atr-v2/acme"))

    @property
    def ocsp_responder_url(self) -> str:
        if self.ocsp_responder_url_override.strip():
            return self.ocsp_responder_url_override.strip()
        return str(self._toml.get("ca", {}).get("ocsp_responder_url", "http://localhost:9003/acps-atr-v2/ocsp"))

    @property
    def crl_distribution_point_url(self) -> str:
        if self.crl_distribution_point_url_override.strip():
            return self.crl_distribution_point_url_override.strip()
        return str(
            self._toml.get("ca", {}).get("crl_distribution_point_url", "http://localhost:9003/acps-atr-v2/crl/current")
        )

    @property
    def registry_server_url(self) -> str:
        if self.registry_server_url_override.strip():
            return self.registry_server_url_override.strip()
        return str(self._toml.get("registry_server", {}).get("url", "http://localhost:9001/acps-atr-v2"))

    @property
    def registry_server_timeout(self) -> int:
        if self.registry_server_timeout_override is not None:
            return self.registry_server_timeout_override
        return int(self._toml.get("registry_server", {}).get("timeout", 10))

    def validate_certificate_discovery_urls(self) -> None:
        """校验证书中的 OCSP / CRL 发现地址配置"""
        discovery_urls = {
            "ca.ocsp_responder_url": ("OCSP_RESPONDER_URL", self.ocsp_responder_url),
            "ca.crl_distribution_point_url": ("CRL_DISTRIBUTION_POINT_URL", self.crl_distribution_point_url),
        }

        for setting_name, (env_var, url) in discovery_urls.items():
            hostname = _validate_absolute_http_url(setting_name, url)

            if self.app_env == "production" and (_is_loopback_host(hostname) or _is_placeholder_host(hostname)):
                raise ValueError(
                    f"{setting_name} must be explicitly configured to an externally reachable hostname "
                    f"in production (inject {env_var})"
                )

    @property
    def registry_server_mock(self) -> bool:
        if self.registry_server_mock_override is not None:
            return self.registry_server_mock_override

        return bool(self._toml.get("registry_server", {}).get("mock", False))

    @property
    def external_service_max_retries(self) -> int:
        return int(self._toml.get("registry_server", {}).get("max_retries", 3))

    @property
    def external_service_retry_delays(self) -> str:
        return str(self._toml.get("registry_server", {}).get("retry_delays", "1,2,4"))

    @property
    def external_service_retry_delays_list(self) -> list[int]:
        """获取外部服务重试延迟列表"""
        raw = self.external_service_retry_delays
        return [int(d.strip()) for d in raw.split(",") if d.strip().isdigit()]

    @property
    def atr_mgmt_allow_ip_list(self) -> str:
        return str(self._toml.get("atr", {}).get("mgmt_allow_ip_list", "127.0.0.1,::1"))

    @property
    def atr_mgmt_allow_ip_list_parsed(self) -> list[str]:
        """获取 ATR 管理功能允许的 IP 地址列表"""
        return [ip.strip() for ip in self.atr_mgmt_allow_ip_list.split(",") if ip.strip()]

    @property
    def public_read_rate_limit_requests(self) -> int:
        return int(self._toml.get("public_api", {}).get("rate_limit_requests", 60))

    @property
    def public_read_rate_limit_window_seconds(self) -> int:
        return int(self._toml.get("public_api", {}).get("rate_limit_window_seconds", 60))

    @property
    def public_read_retry_after_seconds(self) -> int:
        return int(self._toml.get("public_api", {}).get("retry_after_seconds", 60))

    def build_agent_common_name(self, agent_id: str) -> str:
        """构造 Agent 证书的 CN（直接返回裸 AIC）"""
        return agent_id

    @property
    def database_url_computed(self) -> str:
        """计算数据库连接 URL"""
        url = (self.database_url or "").strip()
        if not url:
            raise ValueError("DATABASE_URL environment variable is not configured.")
        return url

    def build_database_url(self, drivername: str) -> str:
        """基于统一配置源构造指定驱动的 PostgreSQL URL"""
        url = make_url(self.database_url_computed)
        backend_name = url.get_backend_name()
        if backend_name not in {"postgresql", "postgres"}:
            raise ValueError(f"Unsupported database backend: {backend_name}")
        return url.set(drivername=drivername).render_as_string(hide_password=False)

    @property
    def database_url_sync(self) -> str:
        """获取同步补充路径使用的 psycopg URL"""
        return self.build_database_url("postgresql+psycopg")

    @property
    def database_url_async(self) -> str:
        """获取请求链路使用的 asyncpg URL"""
        return self.build_database_url("postgresql+asyncpg")


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取应用配置实例"""
    return settings


def get_db_url() -> str:
    """获取同步数据库 URL，用于 Alembic 等工具"""
    return settings.database_url_sync


def get_sync_db_url() -> str:
    """获取同步补充路径使用的数据库 URL"""
    return settings.database_url_sync


def get_async_db_url() -> str:
    """获取请求链路使用的异步数据库 URL"""
    return settings.database_url_async
