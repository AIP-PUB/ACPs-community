"""配置加载与管理。

使用 tomllib 读取 config/ 下的 TOML 文件，使用 pydantic-settings 加载环境变量中的敏感配置。
加载顺序：default.toml → {APP_ENV}.toml，后者覆盖前者中的同名项。
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger()

_SOURCE_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _resolve_config_dir() -> Path:
    """解析运行时 config 目录，兼容源码树与 wheel 安装目录。"""

    working_dir_config = Path.cwd() / "config"
    if working_dir_config.is_dir():
        return working_dir_config

    return _SOURCE_CONFIG_DIR


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个字典，override 覆盖 base 中的同名项。"""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_toml_config(env: str = "development") -> dict[str, Any]:
    """加载 TOML 配置文件：default.toml → {env}.toml。"""
    config_dir = _resolve_config_dir()
    default_path = config_dir / "default.toml"
    env_path = config_dir / f"{env}.toml"

    config: dict[str, Any] = {}
    if default_path.exists():
        with default_path.open("rb") as f:
            config = tomllib.load(f)
        logger.debug("已加载默认配置", path=str(default_path))
    if env_path.exists():
        with env_path.open("rb") as f:
            env_config = tomllib.load(f)
        config = _deep_merge(config, env_config)
        logger.debug("已加载环境配置", path=str(env_path))
    logger.info("配置加载完成", env=env)
    return config


class Settings(BaseSettings):
    """应用设置，环境变量承载敏感数据，TOML 承载非敏感配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 环境选择（决定加载哪个 TOML 配置文件）
    app_env: str = Field(default="development", alias="APP_ENV")

    # 敏感配置（从环境变量加载）
    tls_cert_file: Path = Field(
        default=Path("certs/server.pem"),
        alias="TLS_CERT_FILE",
    )
    tls_key_file: Path = Field(
        default=Path("certs/server.key"),
        alias="TLS_KEY_FILE",
    )
    tls_ca_cert_file: Path = Field(
        default=Path("certs/acps-root-ca.pem"),
        alias="TLS_CA_CERT_FILE",
    )
    redis_url: SecretStr = Field(default=SecretStr("redis://localhost:6379/0"), alias="REDIS_URL")
    redis_tls_ca_cert: Path | None = Field(
        default=Path("certs/acps-root-ca.pem"),
        alias="REDIS_TLS_CA_CERT",
    )
    redis_tls_check_hostname: bool = Field(
        default=False,
        alias="REDIS_TLS_CHECK_HOSTNAME",
    )
    rabbitmq_mgmt_url: str = Field(
        default="http://localhost:15672",
        alias="RABBITMQ_MGMT_URL",
    )
    rabbitmq_mgmt_pass: SecretStr = Field(alias="RABBITMQ_MGMT_PASS")

    # TOML 配置（运行时从文件加载并合并）
    toml: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """加载并合并 TOML 配置。"""
        self.toml = load_toml_config(self.app_env)

    # 便捷访问方法 — server
    @property
    def host(self) -> str:
        # 默认监听全部接口是服务预期行为（与 default.toml / ruff S104 豁免一致）。
        return str(self.toml.get("server", {}).get("host", "0.0.0.0"))  # nosec B104

    @property
    def group_api_port(self) -> int:
        override = os.getenv("MQ_AUTH_GROUP_API_PORT", "").strip()
        if override:
            return int(override)
        return int(self.toml.get("server", {}).get("group_api_port", 9007))

    @property
    def auth_api_port(self) -> int:
        override = os.getenv("MQ_AUTH_AUTH_API_PORT", "").strip()
        if override:
            return int(override)
        return int(self.toml.get("server", {}).get("auth_api_port", 9008))

    # 便捷访问方法 — cache
    @property
    def local_cache_ttl_seconds(self) -> int:
        return int(self.toml.get("cache", {}).get("local_ttl_seconds", 30))

    @property
    def group_acl_key_ttl_seconds(self) -> int:
        return int(self.toml.get("cache", {}).get("group_acl_key_ttl_seconds", 604800))

    # 便捷访问方法 — rabbitmq
    @property
    def rabbitmq_mgmt_user(self) -> str:
        return str(self.toml.get("rabbitmq", {}).get("mgmt_user", "mq-auth-svc"))

    @property
    def rabbitmq_mgmt_password(self) -> str:
        return self.rabbitmq_mgmt_pass.get_secret_value()

    # 便捷访问方法 — redis
    @property
    def redis_url_value(self) -> str:
        """Redis 连接地址（明文字符串，个别传递给 redis-py）。"""
        return self.redis_url.get_secret_value()

    # 便捷访问方法 — logging
    @property
    def log_level(self) -> str:
        return str(self.toml.get("logging", {}).get("level", "INFO"))

    @property
    def log_format(self) -> str:
        return str(self.toml.get("logging", {}).get("format", "json"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()  # type: ignore[call-arg]
