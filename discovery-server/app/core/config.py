"""Agent Discovery Server 的配置管理。

加载顺序：`config/default.toml` -> `config/{APP_ENV}.toml` -> `.env` / 环境变量。
非敏感运行时配置通过 TOML 管理，敏感或部署专用配置通过 `.env` / 环境变量覆写。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_ENV_FILE = _PROJECT_ROOT / ".env"


def _resolve_config_dir() -> Path:
    """优先使用运行包当前工作目录下的 config/，否则回退到源码树配置目录。"""

    cwd_config_dir = Path.cwd() / "config"
    if cwd_config_dir.is_dir():
        return cwd_config_dir

    return _CONFIG_DIR


def _resolve_env_file() -> Path:
    """优先使用运行包当前工作目录下的 .env，否则回退到源码树 .env。"""

    cwd_env_file = Path.cwd() / ".env"
    if cwd_env_file.is_file():
        return cwd_env_file

    return _ENV_FILE


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个字典，override 覆盖 base 中的同名项。"""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_toml_config(app_env: str) -> dict[str, Any]:
    """按 `default.toml` -> `{app_env}.toml` 顺序加载配置。"""
    config: dict[str, Any] = {}
    config_dir = _resolve_config_dir()
    default_path = config_dir / "default.toml"
    env_path = config_dir / f"{app_env}.toml"

    if default_path.exists():
        with default_path.open("rb") as file_obj:
            config = tomllib.load(file_obj)

    if env_path.exists():
        with env_path.open("rb") as file_obj:
            env_config = tomllib.load(file_obj)
        config = _deep_merge(config, env_config)

    return config


def _validate_absolute_http_url(setting_name: str, url: str) -> None:
    """校验 URL 为绝对 http(s) 地址。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{setting_name} must be an absolute http(s) URL")

    if parsed.hostname is None:
        raise ValueError(f"{setting_name} must include a hostname")


def _resolve_app_env() -> str:
    """解析当前 APP_ENV，优先环境变量，其次 .env，默认 development。"""
    app_env = os.getenv("APP_ENV", "").strip()
    if app_env:
        return app_env

    env_file = _resolve_env_file()
    if env_file.exists():
        dotenv_app_env = dotenv_values(env_file).get("APP_ENV")
        if isinstance(dotenv_app_env, str) and dotenv_app_env.strip():
            return dotenv_app_env.strip()

    return "development"


def _flatten_toml_settings(config: dict[str, Any]) -> dict[str, Any]:
    """将分层 TOML 配置映射为当前 Settings 使用的扁平字段。"""
    app_config = config.get("app", {})
    server_config = config.get("server", {})
    logging_config = config.get("logging", {})
    database_config = config.get("database", {})
    discovery_config = config.get("discovery", {})
    embedding_config = config.get("embedding", {})
    embedding_cpu_config = embedding_config.get("cpu", {})
    embedding_gpu_config = embedding_config.get("gpu", {})
    dsp_config = config.get("dsp", {})
    dsp_webhook_config = dsp_config.get("webhook", {})
    prompt_config = config.get("prompt", {})
    llm_config = config.get("llm", {})
    discovery_llm_config = llm_config.get("discovery", {})
    polling_config = config.get("polling", {})
    forwarder_config = config.get("forwarder", {})
    alive_sync_config = config.get("alive_sync", {})

    flattened = {
        "APP_NAME": app_config.get("name"),
        "APP_VERSION": app_config.get("version"),
        "APP_DESC": app_config.get("description"),
        "APP_LOG_LEVEL": logging_config.get("level", app_config.get("log_level")),
        "LOG_FORMAT": logging_config.get("format"),
        "APP_ROOT_PATH": server_config.get("root_path", app_config.get("root_path")),
        "UVICORN_HOST": server_config.get("host"),
        "UVICORN_PORT": server_config.get("port"),
        "UVICORN_RELOAD": server_config.get("reload"),
        "UVICORN_LOG_LEVEL": server_config.get("uvicorn_log_level", server_config.get("log_level")),
        "DATABASE_OUTPUT_SQL": database_config.get("output_sql"),
        "DATABASE_POOL_SIZE": database_config.get("pool_size"),
        "DATABASE_MAX_OVERFLOW": database_config.get("max_overflow"),
        "DATABASE_POOL_TIMEOUT": database_config.get("pool_timeout"),
        "DATABASE_POOL_RECYCLE": database_config.get("pool_recycle"),
        "DATABASE_POOL_PRE_PING": database_config.get("pool_pre_ping"),
        "DISCOVERY_MODE": discovery_config.get("mode"),
        "DISCOVERY_LLM_MODEL_NAME": discovery_llm_config.get("model_name"),
        "DISCOVERY_LLM_BASE_URL": discovery_llm_config.get("base_url"),
        "PROMPT_FILE_PATH": prompt_config.get("planner_file_path"),
        "CLUSTER_PROMPT_FILE_PATH": prompt_config.get("cluster_file_path"),
        "EMBEDDING_DIM": embedding_config.get("dimension"),
        "BGE_BATCH_SIZE": embedding_config.get("batch_size"),
        "BGE_MAX_WAIT_TIME": embedding_config.get("max_wait_time"),
        "EMBEDDING_MODEL_NAME": embedding_cpu_config.get("model_name"),
        "EMBEDDING_BASE_URL": embedding_cpu_config.get("base_url"),
        "EMBEDDING_MODEL_PATH": embedding_gpu_config.get("model_path"),
        "EMBEDDING_DEVICES": embedding_gpu_config.get("devices"),
        "RERANKER_URL": embedding_gpu_config.get("reranker_url"),
        "DSP_BASE_URL": dsp_config.get("base_url"),
        "DSP_CHANGES_PULL_INTERVAL": dsp_config.get("changes_pull_interval"),
        "DSP_SNAPSHOT_CHUNK_SIZE": dsp_config.get("snapshot_chunk_size"),
        "DSP_CHANGES_CHUNK_SIZE": dsp_config.get("changes_chunk_size"),
        "DSP_SEMANTIC_INDEX_CONCURRENCY": dsp_config.get("semantic_index_concurrency"),
        "DSP_WEBHOOK_RECEIVE_URL": dsp_webhook_config.get("receive_url"),
        "POLLING_SERVER_URL": polling_config.get("server_url"),
        "POLLING_INTERVAL": polling_config.get("interval"),
        "FORWARDER_SERVER_URL": forwarder_config.get("server_url"),
        "FORWARDER_SERVER_TIMEOUT": forwarder_config.get("server_timeout"),
        "FORWARDER_SERVER_ENABLED": forwarder_config.get("server_enabled"),
        "FORWARDER_HEALTH_CHECK_INTERVAL": forwarder_config.get("health_check_interval"),
        "FORWARDER_REQUEST_RETRIES": forwarder_config.get("request_retries"),
        "FORWARDER_FALLBACK_TO_LOCAL": forwarder_config.get("fallback_to_local"),
        "ALIVE_SYNC_ENABLED": alive_sync_config.get("enabled"),
        "ALIVE_SYNC_AUTO_START": alive_sync_config.get("auto_start"),
        "ALIVE_SYNC_PROVIDER_BASE_URL": alive_sync_config.get("provider_base_url"),
        "ALIVE_SYNC_HTTP_TIMEOUT": alive_sync_config.get("http_timeout"),
        "ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS": alive_sync_config.get("kafka_bootstrap_servers"),
        "ALIVE_SYNC_KAFKA_GROUP_ID": alive_sync_config.get("kafka_group_id"),
        "ALIVE_SYNC_KAFKA_TOPIC": alive_sync_config.get("kafka_topic"),
        "ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS": alive_sync_config.get("bootstrap_lookback_seconds"),
        "ALIVE_SYNC_BOOTSTRAP_MAX_LOOKBACK_SECONDS": alive_sync_config.get("bootstrap_max_lookback_seconds"),
        "ALIVE_SYNC_RESYNC_BACKOFF_SECONDS": alive_sync_config.get("resync_backoff_seconds"),
        "ALIVE_SYNC_RETRY_INTERVAL_SECONDS": alive_sync_config.get("retry_interval_seconds"),
    }

    return {key: value for key, value in flattened.items() if value is not None}


class TomlSettingsSource(PydanticBaseSettingsSource):
    """基于 `APP_ENV` 读取 `config/*.toml` 的自定义 settings source。"""

    def __init__(self, settings_cls: type[BaseSettings]):
        super().__init__(settings_cls)
        self._data = _flatten_toml_settings(_load_toml_config(_resolve_app_env()))

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, value_is_complex = self.get_field_value(field, field_name)
            if isinstance(value, list | dict):
                values[key] = value
                continue
            prepared = self.prepare_field_value(field_name, field, value, value_is_complex)
            if prepared is not None:
                values[key] = prepared
        return values


class Settings(BaseSettings):
    """从 TOML、环境变量和 `.env` 文件加载的应用配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlSettingsSource(settings_cls),
            file_secret_settings,
        )

    APP_ENV: str = Field(default="development")

    # Basic app settings
    APP_NAME: str = Field(default="Agent Discovery Server")
    APP_VERSION: str = Field(default="2.2.0")
    APP_DESC: str = Field(default="Agent 发现 API")
    APP_LOG_LEVEL: str = Field(default="info")
    LOG_FORMAT: str = Field(default="")
    APP_ROOT_PATH: str = Field(default="")

    # Server settings
    UVICORN_HOST: str = Field(default="0.0.0.0")  # nosec B104  # noqa: S104
    UVICORN_PORT: int = Field(default=9005)
    UVICORN_RELOAD: bool = Field(default=False)
    UVICORN_LOG_LEVEL: str = Field(default="info")

    # 数据库配置
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://user:password@localhost:5432/agent_discovery",
    )
    DATABASE_OUTPUT_SQL: bool = Field(default=False)
    DATABASE_POOL_SIZE: int = Field(default=10)
    DATABASE_MAX_OVERFLOW: int = Field(default=20)
    DATABASE_POOL_TIMEOUT: float = Field(default=30.0)
    DATABASE_POOL_RECYCLE: int = Field(default=1800)
    DATABASE_POOL_PRE_PING: bool = Field(default=True)

    # 运行模式：cpu / gpu；默认 cpu（外部 Embedding/LLM API），避免 base 环境在缺少
    # GPU 本地模型依赖（gpu extra 未安装）时被隐式导向 GPU 模式。
    DISCOVERY_MODE: str = Field(default="cpu")

    # Discovery LLM 配置（与 CPU 版本保持一致）
    DISCOVERY_LLM_API_KEY: str = Field(default="")
    DISCOVERY_LLM_BASE_URL: str = Field(default="")
    DISCOVERY_LLM_MODEL_NAME: str = Field(default="")

    # Embedding 服务配置
    EMBEDDING_MODEL_PATH: str = Field(default="")
    EMBEDDING_DEVICES: str = Field(default="cuda:0")
    EMBEDDING_API_KEY: str = Field(default="")
    EMBEDDING_BASE_URL: str = Field(default="")
    EMBEDDING_MODEL_NAME: str = Field(default="")
    EMBEDDING_DIM: int = Field(default=1024)
    EMBEDDING_TIMEOUT: float = Field(default=180.0)
    BGE_BATCH_SIZE: int = 64
    BGE_MAX_WAIT_TIME: float = 0.01
    # Reranker 配置
    RERANKER_URL: str = Field(default="")

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def normalize_app_env(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized or "development"
        return value

    @field_validator("DISCOVERY_MODE", mode="before")
    @classmethod
    def normalize_discovery_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized or "cpu"
        return value

    @property
    def embedding_devices_list(self) -> list[str]:
        return [d.strip() for d in self.EMBEDDING_DEVICES.split(",") if d.strip()]

    # 业务配置
    DSP_BASE_URL: str = Field(default="http://localhost:9001/acps-dsp-v2")
    DSP_AUTO_START: bool = Field(default=True)
    DSP_CHANGES_PULL_INTERVAL: int = Field(default=600)
    DSP_SNAPSHOT_CHUNK_SIZE: int = Field(default=10000)
    DSP_CHANGES_CHUNK_SIZE: int = Field(default=1000)
    DSP_SEMANTIC_INDEX_CONCURRENCY: int = Field(default=4)
    PROMPT_FILE_PATH: str = Field(default="")
    CLUSTER_PROMPT_FILE_PATH: str = Field(default="")

    # 可用性轮询服务配置
    POLLING_SERVER_URL: str = Field(default="http://localhost:8006")
    POLLING_INTERVAL: int = Field(default=300)  # 秒，默认5分钟

    # DSP Webhook 配置
    DSP_WEBHOOK_SECRET: str = Field(default="test_123")
    DSP_WEBHOOK_RECEIVE_URL: str = Field(
        default="http://localhost:9005/admin/dsp/webhooks/receive",
    )

    # 转发服务器配置
    FORWARDER_SERVER_URL: str = Field(default="")
    FORWARDER_SERVER_TIMEOUT: float = Field(default=30.0)
    FORWARDER_SERVER_ENABLED: bool = Field(default=False)
    FORWARDER_HEALTH_CHECK_INTERVAL: int = Field(default=600)
    FORWARDER_REQUEST_RETRIES: int = Field(default=0)
    FORWARDER_FALLBACK_TO_LOCAL: bool = Field(default=True)

    # AMP Heartbeat alive sync（Consumer）
    ALIVE_SYNC_ENABLED: bool = Field(default=False)
    ALIVE_SYNC_AUTO_START: bool = Field(default=True)
    ALIVE_SYNC_PROVIDER_BASE_URL: str = Field(
        default="",
        description="monitor-server 基址，含 /sync/* 路径，如 http://localhost:9009/acps-amp-v1/heartbeat",
    )
    ALIVE_SYNC_PROVIDER_BEARER_TOKEN: str = Field(
        default="",
        description="Monitor /sync/* 服务间 Bearer（与 HEARTBEAT_SYNC_INTERNAL_TOKEN 对齐；OIDC on 必填）",
    )
    ALIVE_SYNC_HTTP_TIMEOUT: float = Field(default=30.0)
    ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS: str = Field(default="")
    ALIVE_SYNC_KAFKA_GROUP_ID: str = Field(default="discovery-server.alive-sync.v1")
    ALIVE_SYNC_KAFKA_TOPIC: str = Field(
        default="",
        description="留空则用 /sync/info 返回的 kafkaTopic",
    )
    ALIVE_SYNC_BOOTSTRAP_LOOKBACK_SECONDS: int = Field(
        default=300,
        description="§7.5 第 8 条回看裕量（秒），默认 5 分钟",
    )
    ALIVE_SYNC_BOOTSTRAP_MAX_LOOKBACK_SECONDS: int = Field(
        default=86400,
        description="免误报倍增上限（秒），达顶退 earliest",
    )
    ALIVE_SYNC_RESYNC_BACKOFF_SECONDS: int = Field(
        default=10,
        description="缺口/降级后重 snapshot 退避间隔（秒）",
    )
    ALIVE_SYNC_RETRY_INTERVAL_SECONDS: int = Field(
        default=30,
        description="503 降级（snapshot/delta unhealthy）轮询重试间隔（秒）",
    )

    def model_post_init(self, __context: Any) -> None:
        """补充运行环境相关校验。"""

        self.validate_external_service_urls()

    def validate_external_service_urls(self) -> None:
        """校验已配置的运行时 URL。"""

        discovery_urls = {
            "dsp.base_url": self.DSP_BASE_URL,
            "dsp.webhook.receive_url": self.DSP_WEBHOOK_RECEIVE_URL,
            "polling.server_url": self.POLLING_SERVER_URL,
        }

        if self.FORWARDER_SERVER_URL.strip():
            discovery_urls["forwarder.server_url"] = self.FORWARDER_SERVER_URL

        if self.ALIVE_SYNC_PROVIDER_BASE_URL.strip():
            discovery_urls["alive_sync.provider_base_url"] = self.ALIVE_SYNC_PROVIDER_BASE_URL

        for setting_name, url in discovery_urls.items():
            if url.strip():
                _validate_absolute_http_url(setting_name, url)


# 创建全局的 settings 实例（模块级别）
settings = Settings()


def get_settings() -> Settings:
    """获取全局的 settings 实例。"""
    return settings
