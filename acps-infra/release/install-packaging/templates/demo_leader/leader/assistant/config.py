import copy
import json
import logging
import os
import sys
import tomllib as toml
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

from leader.runtime_paths import (
    resolve_acs_path,
    resolve_config_path,
    resolve_leader_dir,
    resolve_project_env_file,
)

logger = logging.getLogger(__name__)

LLM_REQUIRED_FIELDS = ("api_key", "base_url", "model")
RABBITMQ_ENV_MAPPING = {
    "host": "RABBITMQ_HOST",
    "port": "RABBITMQ_PORT",
    "user": "RABBITMQ_USER",
    "password": "RABBITMQ_PASSWORD",
    "vhost": "RABBITMQ_VHOST",
    "auth_service_url": "MQ_AUTH_URL",
}
RABBITMQ_MANAGEMENT_ENV_MAPPING = {
    "host": "RABBITMQ_MANAGEMENT_HOST",
    "port": "RABBITMQ_MANAGEMENT_PORT",
}

# Default Configuration
DEFAULT_CONFIG = {
    "app": {
        "acs_json": "atr/acs.json",
    },
    "uvicorn": {
        "host": "0.0.0.0",
        "port": 9031,
        "reload": False,
    },
    "rabbitmq": {
        "host": "localhost",
        "port": 5671,
        "vhost": "acps",
        "auth_service_url": "https://localhost:9007",
    },
    "llm": {
        "default": {
            "api_type": "openai",
            "model": "gpt-4",
            # api_key and base_url are required in config.toml
        }
    },
    "discovery": {
        "server_base_url": "",
        "timeout": 30,
        "limit": 5,
    },
    "oidc": {
        "enabled": False,
        "issuer": "",
        "audience": "leader-api",
        "client_id": "leader-api",
        "allowed_azp": ["leader-web", "leader-install"],
        "algorithms": ["EdDSA"],
        "jwks_cache_ttl_seconds": 300,
        "discovery_cache_ttl_seconds": 300,
        "leeway_seconds": 60,
        "require_https": True,
        "role_source_client_id": "leader-api",
    },
    "authorization": {
        "user_roles": ["user", "operator", "admin"],
        "operator_roles": ["operator", "admin"],
        "admin_roles": ["admin"],
    },
}

OIDC_ENV_MAPPING = {
    "enabled": "LEADER_OIDC_ENABLED",
    "issuer": "LEADER_OIDC_ISSUER",
    "audience": "LEADER_OIDC_AUDIENCE",
    "allowed_azp": "LEADER_OIDC_ALLOWED_AZP",
    "client_id": "LEADER_OIDC_CLIENT_ID",
    "algorithms": "LEADER_OIDC_ALGORITHMS",
    "jwks_cache_ttl_seconds": "LEADER_OIDC_JWKS_CACHE_TTL_SECONDS",
    "discovery_cache_ttl_seconds": "LEADER_OIDC_DISCOVERY_CACHE_TTL_SECONDS",
    "leeway_seconds": "LEADER_OIDC_LEEWAY_SECONDS",
    "require_https": "LEADER_OIDC_REQUIRE_HTTPS",
    "role_source_client_id": "LEADER_OIDC_ROLE_SOURCE_CLIENT_ID",
}

OIDC_LIST_FIELDS = {"allowed_azp", "algorithms"}
OIDC_INT_FIELDS = {"jwks_cache_ttl_seconds", "discovery_cache_ttl_seconds", "leeway_seconds"}
OIDC_BOOL_FIELDS = {"enabled", "require_https"}


class ConfigManager:
    _instance: ConfigManager | None = None
    _config: dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _deep_update(self, target: dict, source: dict):
        for key, value in source.items():
            if isinstance(value, dict) and key in target and isinstance(target[key], dict):
                self._deep_update(target[key], value)
            else:
                target[key] = value

    @staticmethod
    def _parse_bool(value: str) -> bool | None:
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return None

    @staticmethod
    def _parse_string_list(value: str) -> list[str]:
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]

    def _load(self):
        # Start with defaults
        self._config = copy.deepcopy(DEFAULT_CONFIG)

        leader_dir = resolve_leader_dir()
        self._load_project_env()
        config_path = resolve_config_path()

        if not config_path.exists():
            logger.error(f"Config file not found at {config_path.absolute()}. Exiting.")
            sys.exit(1)

        try:
            with open(config_path, "rb") as f:
                toml_config = toml.load(f)
                self._deep_update(self._config, toml_config)
        except Exception as e:
            logger.error(f"Failed to load config.toml: {e}")
            sys.exit(1)

        self._resolve_llm_profiles()
        self._resolve_discovery_config()
        self._resolve_rabbitmq_config()
        self._resolve_oidc_config()

        # Load leader AIC from acs.json
        self._load_leader_aic(leader_dir)

        self._validate()

    def _load_project_env(self):
        env_path = resolve_project_env_file()
        if env_path.exists():
            load_dotenv(env_path, override=False)

    def _resolve_llm_value(
        self,
        profile_name: str,
        profile_data: dict[str, Any],
        field_name: str,
    ) -> str:
        env_key_name = f"{field_name}_env"
        env_var_name = profile_data.get(env_key_name)
        if isinstance(env_var_name, str) and env_var_name.strip():
            resolved_value = os.getenv(env_var_name.strip())
            if isinstance(resolved_value, str) and resolved_value.strip():
                return resolved_value

            logger.error(
                "Missing required environment variable for LLM profile [%s]: %s -> %s",
                profile_name,
                env_key_name,
                env_var_name,
            )
            sys.exit(1)

        literal_value = profile_data.get(field_name)
        if isinstance(literal_value, str) and literal_value.strip():
            return literal_value

        logger.error(
            "Missing required LLM config in profile [%s]: %s or %s",
            profile_name,
            field_name,
            env_key_name,
        )
        sys.exit(1)

    def _resolve_llm_profiles(self):
        llm_config = self._config.get("llm", {})
        if not isinstance(llm_config, dict):
            return

        for profile_name, profile_data in llm_config.items():
            if not isinstance(profile_data, dict):
                continue

            for field_name in LLM_REQUIRED_FIELDS:
                profile_data[field_name] = self._resolve_llm_value(
                    profile_name,
                    profile_data,
                    field_name,
                )

    def _resolve_discovery_config(self):
        discovery_config = self._config.get("discovery", {})
        if not isinstance(discovery_config, dict):
            return

        env_var_name = discovery_config.get("server_base_url_env")
        if not isinstance(env_var_name, str) or not env_var_name.strip():
            return

        resolved_value = os.getenv(env_var_name.strip())
        if isinstance(resolved_value, str) and resolved_value.strip():
            discovery_config["server_base_url"] = resolved_value.strip()

    def _resolve_rabbitmq_config(self):
        """从固定环境变量解析 RabbitMQ 配置并覆盖 config.toml 默认值。"""
        rabbitmq_config = self._config.get("rabbitmq", {})
        if not isinstance(rabbitmq_config, dict):
            return

        for field_name, env_var_name in RABBITMQ_ENV_MAPPING.items():
            resolved_value = os.getenv(env_var_name)
            if not isinstance(resolved_value, str) or not resolved_value.strip():
                continue

            if field_name == "port":
                try:
                    rabbitmq_config[field_name] = int(resolved_value.strip())
                except ValueError:
                    logger.warning(
                        "Invalid RabbitMQ port from environment: %s = %s, using default",
                        env_var_name,
                        resolved_value,
                    )
                continue

            rabbitmq_config[field_name] = resolved_value.strip()

        management_config = rabbitmq_config.get("management")
        if not isinstance(management_config, dict):
            management_config = {}
            rabbitmq_config["management"] = management_config

        for field_name, env_var_name in RABBITMQ_MANAGEMENT_ENV_MAPPING.items():
            resolved_value = os.getenv(env_var_name)
            if not isinstance(resolved_value, str) or not resolved_value.strip():
                continue

            if field_name == "port":
                try:
                    management_config[field_name] = int(resolved_value.strip())
                except ValueError:
                    logger.warning(
                        "Invalid RabbitMQ management port from environment: %s = %s, using default",
                        env_var_name,
                        resolved_value,
                    )
                continue

            management_config[field_name] = resolved_value.strip()

        app_env = os.getenv("APP_ENV", "development").strip().lower()
        host = rabbitmq_config.get("host")
        port = rabbitmq_config.get("port")
        user = rabbitmq_config.get("user")
        password = rabbitmq_config.get("password")

        if (
            app_env == "development"
            and host in {"localhost", "127.0.0.1"}
            and port == 5672
            and not user
            and not password
        ):
            rabbitmq_config["user"] = "admin"
            rabbitmq_config["password"] = "devpass"  # noqa: S105

    def _resolve_oidc_config(self):
        """从环境变量解析 OIDC 配置并覆盖 config.toml 默认值。"""
        oidc_config = self._config.get("oidc", {})
        if not isinstance(oidc_config, dict):
            return

        for field_name, env_var_name in OIDC_ENV_MAPPING.items():
            raw_value = os.getenv(env_var_name)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue

            if field_name in OIDC_BOOL_FIELDS:
                parsed_bool = self._parse_bool(raw_value)
                if parsed_bool is None:
                    logger.warning(
                        "Invalid OIDC boolean from environment: %s = %s, using config value",
                        env_var_name,
                        raw_value,
                    )
                    continue
                oidc_config[field_name] = parsed_bool
                continue

            if field_name in OIDC_INT_FIELDS:
                try:
                    oidc_config[field_name] = int(raw_value.strip())
                except ValueError:
                    logger.warning(
                        "Invalid OIDC integer from environment: %s = %s, using config value",
                        env_var_name,
                        raw_value,
                    )
                continue

            if field_name in OIDC_LIST_FIELDS:
                oidc_config[field_name] = self._parse_string_list(raw_value)
                continue

            oidc_config[field_name] = raw_value.strip()

    def _load_leader_aic(self, _leader_dir: object | None = None):
        """从 acs.json 文件中解析 leader_aic"""
        acs_json_rel = self._config.get("app", {}).get("acs_json", "atr/acs.json")
        acs_json_path = resolve_acs_path(str(acs_json_rel))

        if not acs_json_path.exists():
            logger.error(f"ACS file not found: {acs_json_path.absolute()}. Exiting.")
            sys.exit(1)

        try:
            with open(acs_json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to parse {acs_json_path}: {e}")
            sys.exit(1)

        aic = data.get("aic")
        if not aic or not isinstance(aic, str) or not aic.strip():
            logger.error(f"Missing or empty 'aic' field in {acs_json_path}. Exiting.")
            sys.exit(1)

        self._config["app"]["leader_aic"] = aic
        logger.info(f"Loaded leader AIC from {acs_json_rel}: {aic}")

    def _validate(self):
        """Validate configuration values"""
        # 1. Check for None or empty values in critical sections
        for section in ["app", "uvicorn", "rabbitmq"]:
            if section not in self._config:
                logger.error(f"Missing configuration section: [{section}]")
                sys.exit(1)
            for key, value in self._config[section].items():
                if value is None or (isinstance(value, str) and not value.strip()):
                    logger.error(f"Missing value for config: [{section}].{key}")
                    sys.exit(1)

        # 2. Validate LLM configuration
        llm_config = self._config.get("llm", {})
        if not llm_config:
            logger.error("Missing [llm] configuration section")
            sys.exit(1)

        required_llm_keys = ["api_type", *LLM_REQUIRED_FIELDS]

        # Iterate over all profiles (e.g., default, fast, pro)
        for profile_name, profile_data in llm_config.items():
            if not isinstance(profile_data, dict):
                continue  # Skip non-dict entries if any

            for key in required_llm_keys:
                value = profile_data.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    logger.error(f"Missing required LLM config in profile [{profile_name}]: {key}")
                    sys.exit(1)

        oidc_config = self._config.get("oidc", {})
        if not isinstance(oidc_config, dict):
            logger.error("Invalid [oidc] configuration section")
            sys.exit(1)

        enabled = bool(oidc_config.get("enabled", False))
        if enabled:
            for key in ("issuer", "audience"):
                value = oidc_config.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    logger.error(f"Missing required OIDC config when enabled: [oidc].{key}")
                    sys.exit(1)

            for key in ("allowed_azp", "algorithms"):
                value = oidc_config.get(key)
                if isinstance(value, str):
                    oidc_config[key] = self._parse_string_list(value)
                    value = oidc_config[key]
                if not isinstance(value, Iterable) or not list(value):
                    logger.error(f"Missing required OIDC list config when enabled: [oidc].{key}")
                    sys.exit(1)

    @property
    def config(self) -> dict[str, Any]:
        return self._config


# Singleton instance
settings = ConfigManager().config
