"""配置加载与管理。

使用 tomllib 读取 config/ 下的 TOML 文件，使用 pydantic-settings 加载环境变量中的敏感配置。
加载顺序：default.toml → {APP_ENV}.toml，后者覆盖前者中的同名项。

敏感配置（DATABASE_URL 等）通过环境变量或 .env 文件提供；非敏感运行时配置通过 TOML 管理。
"""

from __future__ import annotations

import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SOURCE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _env_string(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_bool(name: str) -> bool | None:
    value = _env_string(name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment variable {name}={value!r}")


def _env_int(name: str) -> int | None:
    value = _env_string(name)
    if value is None:
        return None
    return int(value)


def _env_string_list(name: str) -> list[str] | None:
    value = _env_string(name)
    if value is None:
        return None
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"Environment variable {name} must be a JSON list or comma-separated string")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


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
    """加载 TOML 配置文件：default.toml → {env}.toml。

    Args:
        env: 当前环境名称（development / testing / production）。

    Returns:
        dict[str, Any]: 合并后的配置字典。
    """
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


def _validate_timezone_name(value: str) -> str:
    """校验 IANA 时区名称。"""
    normalized = value.strip()
    if not normalized:
        raise ValueError("database.session_timezone must not be empty")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid database.session_timezone: {normalized}") from exc
    return normalized


class Settings(BaseSettings):
    """应用设置。环境变量承载敏感数据与少量部署覆写，TOML 承载非敏感业务配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── 敏感配置（从环境变量 / .env 加载） ──

    database_url: str = Field(validation_alias="DATABASE_URL")
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    # Shared with discovery-server alive-sync (Bearer for /sync/* when OIDC is on).
    heartbeat_sync_internal_token: str = Field(
        default="",
        validation_alias="HEARTBEAT_SYNC_INTERNAL_TOKEN",
    )

    # ── TOML 配置（运行时从文件加载） ──
    _toml: dict[str, Any] = {}

    def model_post_init(self, __context: Any) -> None:
        """加载并合并 TOML 配置。"""
        object.__setattr__(self, "_toml", load_toml_config(self.app_env))

    # ── Server ──

    @property
    def uvicorn_host(self) -> str:
        """服务监听地址。"""
        return str(self._toml.get("server", {}).get("host", "0.0.0.0"))  # noqa: S104  # nosec B104

    @property
    def uvicorn_port(self) -> int:
        """服务监听端口。"""
        return int(self._toml.get("server", {}).get("port", 9009))

    @property
    def uvicorn_reload(self) -> bool:
        """是否启用热重载（开发环境）。"""
        return bool(self._toml.get("server", {}).get("reload", False))

    @property
    def uvicorn_log_level(self) -> str:
        """Uvicorn 日志级别。"""
        return str(self._toml.get("server", {}).get("uvicorn_log_level", "info"))

    # ── API ──

    @property
    def api_v1_str(self) -> str:
        """AMP API 路由前缀（/api/v1 保留用于内部管理端点）。"""
        return str(self._toml.get("api", {}).get("v1_str", "/api/v1"))

    @property
    def api_title(self) -> str:
        """OpenAPI 文档标题。"""
        return str(self._toml.get("api", {}).get("title", "AMP Monitor Server API"))

    # ── Database ──

    @property
    def database_pool_size(self) -> int:
        """数据库连接池大小。"""
        return int(self._toml.get("database", {}).get("pool_size", 5))

    @property
    def database_max_overflow(self) -> int:
        """数据库连接池最大溢出数。"""
        return int(self._toml.get("database", {}).get("max_overflow", 10))

    @property
    def database_pool_recycle(self) -> int:
        """数据库连接回收时间（秒）。"""
        return int(self._toml.get("database", {}).get("pool_recycle", 1800))

    @property
    def database_pool_timeout(self) -> int:
        """数据库连接超时（秒）。"""
        return int(self._toml.get("database", {}).get("pool_timeout", 30))

    @property
    def database_session_timezone(self) -> str:
        """数据库 session 时区。"""
        raw = str(self._toml.get("database", {}).get("session_timezone", "Asia/Shanghai"))
        return _validate_timezone_name(raw)

    # ── Kafka ──

    @property
    def kafka_bootstrap_servers(self) -> str:
        """Kafka bootstrap servers（支持部署/测试环境变量覆盖）。"""

        import os

        env_val = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if env_val:
            return env_val
        return str(self._toml.get("kafka", {}).get("bootstrap_servers", "localhost:19092"))

    @property
    def kafka_security_protocol(self) -> str:
        """Kafka 安全协议（PLAINTEXT / SSL / SASL_SSL）。"""
        return str(self._toml.get("kafka", {}).get("security_protocol", "PLAINTEXT"))

    @property
    def kafka_auto_offset_reset(self) -> str:
        """Consumer 初次消费或 offset 丢失时的偏移量策略（earliest / latest）。"""
        return str(self._toml.get("kafka", {}).get("auto_offset_reset", "earliest"))

    @property
    def kafka_max_poll_records(self) -> int:
        """单次 poll 最大消息条数。"""
        return int(self._toml.get("kafka", {}).get("max_poll_records", 100))

    @property
    def kafka_session_timeout_ms(self) -> int:
        """Consumer 会话超时（毫秒）。"""
        return int(self._toml.get("kafka", {}).get("session_timeout_ms", 30000))

    @property
    def kafka_heartbeat_interval_ms(self) -> int:
        """Consumer 心跳间隔（毫秒）。"""
        return int(self._toml.get("kafka", {}).get("heartbeat_interval_ms", 10000))

    # ── Audit ──

    @property
    def audit_topic(self) -> str:
        """Audit 日志 Kafka 主题。"""
        return str(self._toml.get("audit", {}).get("topic", "amp.audit"))

    @property
    def audit_dlq_topic(self) -> str:
        """Audit 日志死信队列主题。"""
        return str(self._toml.get("audit", {}).get("dlq_topic", "amp.audit.dlq"))

    @property
    def audit_consumer_group(self) -> str:
        """Audit Writer Kafka consumer group ID。"""
        return str(self._toml.get("audit", {}).get("consumer_group", "amp.audit.writer"))

    @property
    def audit_logical_chain_count(self) -> int:
        """逻辑子链数（log_id 哈希取模后路由到子链）。"""
        count = int(self._toml.get("audit", {}).get("logical_chain_count", 256))
        if count <= 0:
            raise ValueError("audit.logical_chain_count must be > 0")
        return count

    @property
    def audit_anchor_interval_minutes(self) -> int:
        """链锚定触发间隔（分钟）。"""
        minutes = int(self._toml.get("audit", {}).get("anchor_interval_minutes", 60))
        if minutes <= 0:
            raise ValueError("audit.anchor_interval_minutes must be > 0")
        return minutes

    @property
    def audit_online_retention_months(self) -> int:
        """Audit 记录在线保留期（月）。"""
        months = int(self._toml.get("audit", {}).get("online_retention_months", 12))
        if months <= 0:
            raise ValueError("audit.online_retention_months must be > 0")
        return months

    @property
    def audit_archive_retention_years(self) -> int:
        """Audit 记录归档保留期（年）。"""
        return int(self._toml.get("audit", {}).get("archive_retention_years", 7))

    @property
    def audit_max_event_lag_hours(self) -> int:
        """committed_at 围栏谓词扩展窗口（小时）。

        Query Planner 据此构造 `committed_at` 围栏谓词：
          committed_at >= timeRange.startAt - max_event_lag_hours
          committed_at <= timeRange.endAt   + max_event_lag_hours

        这是正确性不变量（非纯性能项）：任何在线记录的实际滞后
        `committed_at - timestamp` 一旦超过此值，就会被按事件时间的 timeRange
        查询静默漏掉，故必须 ≥ 真实最大滞后（默认 48 小时，见 §5.3、§7）。
        """
        return int(self._toml.get("audit", {}).get("max_event_lag_hours", 48))

    @property
    def audit_lagging_threshold_ms(self) -> int:
        """watermark 滞后告警阈值（毫秒）。"""
        return int(self._toml.get("audit", {}).get("lagging_threshold_ms", 60000))

    @property
    def audit_export_max_records(self) -> int:
        """单次导出最大条数。"""
        return int(self._toml.get("audit", {}).get("export_max_records", 1000))

    @property
    def audit_export_url_ttl_minutes(self) -> int:
        """导出 URL 有效期（分钟）。"""
        return int(self._toml.get("audit", {}).get("export_url_ttl_minutes", 10))

    @property
    def audit_verify_sync_max_records(self) -> int:
        """同步完整性校验批次大小。"""
        return int(self._toml.get("audit", {}).get("verify_sync_max_records", 500))

    @property
    def audit_query_timeout_seconds(self) -> int:
        """Query API 超时（秒）。"""
        return int(self._toml.get("audit", {}).get("query_timeout_seconds", 30))

    # ── Redis ──

    @property
    def redis_url(self) -> str:
        """Redis 连接串（优先读 REDIS_URL 环境变量，回退 TOML 配置；TOML 按 APP_ENV 切换，测试环境用 DB 1）。"""
        import os

        env_val = os.environ.get("REDIS_URL", "").strip()
        if env_val:
            return env_val
        return str(self._toml.get("redis", {}).get("url", "redis://localhost:6379/2"))

    @property
    def redis_max_connections(self) -> int:
        """Redis 连接池最大连接数。"""
        return int(self._toml.get("redis", {}).get("max_connections", 50))

    @property
    def redis_socket_timeout_seconds(self) -> float:
        """Redis socket 超时（秒）。"""
        return float(self._toml.get("redis", {}).get("socket_timeout_seconds", 5))

    # ── Heartbeat ──

    @property
    def heartbeat_topic(self) -> str:
        """Heartbeat 日志 Kafka 主题。"""
        return str(self._toml.get("heartbeat", {}).get("topic", "amp.heartbeat"))

    @property
    def heartbeat_dlq_topic(self) -> str:
        """Heartbeat 日志死信队列主题。"""
        return str(self._toml.get("heartbeat", {}).get("dlq_topic", "amp.heartbeat.dlq"))

    @property
    def heartbeat_consumer_group(self) -> str:
        """Heartbeat Writer Kafka consumer group ID。"""
        return str(self._toml.get("heartbeat", {}).get("consumer_group", "monitor-server.heartbeat.writer.v1"))

    @property
    def heartbeat_delta_topic(self) -> str:
        """Heartbeat alive-delta Kafka 主题。"""
        return str(self._toml.get("heartbeat", {}).get("delta_topic", "amp.heartbeat.alive-delta"))

    @property
    def heartbeat_sync_enabled(self) -> bool:
        """是否启用 Sync Profile（/sync/* 端点）。"""
        return bool(self._toml.get("heartbeat", {}).get("sync_enabled", True))

    @property
    def heartbeat_analytics_enabled(self) -> bool:
        """是否启用 Analytics Profile（/silence/top 端点）。"""
        return bool(self._toml.get("heartbeat", {}).get("analytics_enabled", True))

    @property
    def heartbeat_heartbeat_shard_count(self) -> int:
        """Heartbeat Redis 分片数（必须等于 delta topic 分区数，生命周期内不可变）。"""
        return int(self._toml.get("heartbeat", {}).get("heartbeat_shard_count", 1))

    @property
    def heartbeat_silence_threshold_seconds(self) -> int:
        """静默阈值（秒）：超过此时间无心跳则标记为 silent。"""
        return int(self._toml.get("heartbeat", {}).get("silence_threshold_seconds", 90))

    @property
    def heartbeat_evict_after_seconds(self) -> int:
        """驱逐阈值（秒）：超过此时间无心跳则从 Redis 删除记录。"""
        return int(self._toml.get("heartbeat", {}).get("evict_after_seconds", 3600))

    @property
    def heartbeat_refresh_emit_interval_seconds(self) -> int:
        """refresh_alive delta 最小发射间隔（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("refresh_emit_interval_seconds", 30))

    @property
    def heartbeat_silent_scan_interval_seconds(self) -> int:
        """Reconciler silent phase 扫描间隔（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("silent_scan_interval_seconds", 5))

    @property
    def heartbeat_evict_scan_interval_seconds(self) -> int:
        """Reconciler evict phase 扫描间隔（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("evict_scan_interval_seconds", 30))

    @property
    def heartbeat_scan_batch_size(self) -> int:
        """Reconciler 单轮扫描候选数上限。"""
        return int(self._toml.get("heartbeat", {}).get("scan_batch_size", 1000))

    @property
    def heartbeat_scan_lock_ttl_seconds(self) -> int:
        """Reconciler 扫描锁 TTL（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("scan_lock_ttl_seconds", 60))

    @property
    def heartbeat_in_list_max(self) -> int:
        """liveness/query aic in 列表最大长度。"""
        return int(self._toml.get("heartbeat", {}).get("in_list_max", 1000))

    @property
    def heartbeat_silence_top_default_n(self) -> int:
        """silence/top 默认返回条数。"""
        return int(self._toml.get("heartbeat", {}).get("silence_top_default_n", 50))

    @property
    def heartbeat_silence_top_max_n(self) -> int:
        """silence/top 最大返回条数。"""
        return int(self._toml.get("heartbeat", {}).get("silence_top_max_n", 500))

    @property
    def heartbeat_silence_top_shard_fetch_size(self) -> int:
        """silence/top 单 shard 拉取条数（必须 >= silence_top_max_n）。"""
        return int(self._toml.get("heartbeat", {}).get("silence_top_shard_fetch_size", 500))

    @property
    def heartbeat_summary_buckets_seconds(self) -> list[int]:
        """summary 静默时长分桶边界（秒，严格递增）。"""
        raw = self._toml.get("heartbeat", {}).get("summary_buckets_seconds", [30, 60, 120, 300, 900, 3600])
        if not isinstance(raw, list):
            return [30, 60, 120, 300, 900, 3600]
        return [int(v) for v in raw]

    @property
    def heartbeat_summary_cache_ttl_seconds(self) -> int:
        """summary 缓存 TTL（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("summary_cache_ttl_seconds", 5))

    @property
    def heartbeat_silence_top_cache_ttl_seconds(self) -> int:
        """silence/top 缓存 TTL（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("silence_top_cache_ttl_seconds", 5))

    @property
    def heartbeat_delta_retention_hours(self) -> int:
        """alive-delta Kafka topic 保留时长（小时）。"""
        return int(self._toml.get("heartbeat", {}).get("delta_retention_hours", 168))

    @property
    def heartbeat_outbox_max_len(self) -> int:
        """outbox Stream 近似最大长度（0 表示不限制，§7.6 硬上限）。"""
        return int(self._toml.get("heartbeat", {}).get("outbox_max_len", 1000000))

    @property
    def heartbeat_relay_max_publish_lag_seconds(self) -> int:
        """Relay 最大允许发布延迟（秒），超过则 snapshot 端点返回 503。"""
        return int(self._toml.get("heartbeat", {}).get("relay_max_publish_lag_seconds", 30))

    @property
    def heartbeat_relay_published_seq_batch_size(self) -> int:
        """Relay 每批推进 published_seq 的条数。"""
        return int(self._toml.get("heartbeat", {}).get("relay_published_seq_batch_size", 100))

    @property
    def heartbeat_snapshot_max_alive_rows_per_s(self) -> int:
        """Snapshot 输出限速（行/秒）。"""
        return int(self._toml.get("heartbeat", {}).get("snapshot_max_alive_rows_per_s", 50000))

    @property
    def heartbeat_snapshot_chunk_size(self) -> int:
        """Snapshot 枚举单块大小（行）。"""
        return int(self._toml.get("heartbeat", {}).get("snapshot_chunk_size", 1000))

    @property
    def heartbeat_snapshot_max_enumeration_seconds(self) -> int:
        """Snapshot 枚举最大时间（秒），超时截断并记录指标。"""
        return int(self._toml.get("heartbeat", {}).get("snapshot_max_enumeration_seconds", 60))

    @property
    def heartbeat_input_partition_count(self) -> int:
        """amp.heartbeat Kafka topic 分区数（必须与实际分区数一致）。"""
        return int(self._toml.get("heartbeat", {}).get("input_partition_count", 1))

    @property
    def heartbeat_read_model_max_lag_ms(self) -> int:
        """读模型最大允许延迟（毫秒）。"""
        return int(self._toml.get("heartbeat", {}).get("read_model_max_lag_ms", 5000))

    @property
    def heartbeat_writer_watermark_flush_interval_ms(self) -> int:
        """Writer 水位刷新间隔（毫秒）。"""
        return int(self._toml.get("heartbeat", {}).get("writer_watermark_flush_interval_ms", 1000))

    @property
    def heartbeat_writer_watermark_stale_after_ms(self) -> int:
        """Writer 水位过期阈值（毫秒）；必须 > writer_watermark_flush_interval_ms。"""
        return int(self._toml.get("heartbeat", {}).get("writer_watermark_stale_after_ms", 5000))

    @property
    def heartbeat_lagging_response_mode(self) -> str:
        """读模型滞后时的响应模式："503" | "partial"。"""
        return str(self._toml.get("heartbeat", {}).get("lagging_response_mode", "503"))

    @property
    def heartbeat_freshness_point_lookup_localized(self) -> bool:
        """是否启用点查新鲜度局部化（需保证 Producer 用默认 Kafka partitioner）。"""
        return bool(self._toml.get("heartbeat", {}).get("freshness_point_lookup_localized", True))

    @property
    def heartbeat_liveness_query_shard_scan_budget(self) -> int:
        """liveness/query cursor 模式单请求单 shard 扫描预算。"""
        return int(self._toml.get("heartbeat", {}).get("liveness_query_shard_scan_budget", 2000))

    @property
    def heartbeat_snapshot_share_window_seconds(self) -> int:
        """Snapshot 物化共享窗口（秒）；多并发请求共享同一物化结果。"""
        return int(self._toml.get("heartbeat", {}).get("snapshot_share_window_seconds", 5))

    @property
    def heartbeat_writer_poll_timeout_ms(self) -> int:
        """Writer getmany poll 超时（毫秒）。"""
        return int(self._toml.get("heartbeat", {}).get("writer_poll_timeout_ms", 1000))

    @property
    def heartbeat_metrics_log_interval_seconds(self) -> int:
        """metrics_log_loop 输出周期（秒）。"""
        return int(self._toml.get("heartbeat", {}).get("metrics_log_interval_seconds", 60))

    # ── Metrics ──

    @property
    def metrics_topic(self) -> str:
        """Metrics 日志 Kafka 主题。"""
        return str(self._toml.get("metrics", {}).get("topic", "amp.metrics"))

    @property
    def metrics_dlq_topic(self) -> str:
        """Metrics 日志死信队列主题。"""
        return str(self._toml.get("metrics", {}).get("dlq_topic", "amp.metrics.dlq"))

    @property
    def metrics_consumer_group(self) -> str:
        """Metrics Writer Kafka consumer group ID。"""
        return str(self._toml.get("metrics", {}).get("consumer_group", "monitor-server.metrics.writer.v1"))

    @property
    def metrics_writer_poll_timeout_ms(self) -> int:
        """Writer getmany poll 超时（毫秒）。"""
        return int(self._toml.get("metrics", {}).get("writer_poll_timeout_ms", 1000))

    @property
    def metrics_remote_write_batch_interval_seconds(self) -> int:
        """Remote Write 攒批最大等待时间（秒）。"""
        return int(self._toml.get("metrics", {}).get("remote_write_batch_interval_seconds", 5))

    @property
    def metrics_remote_write_batch_max_samples(self) -> int:
        """Remote Write 单批最大样本数。"""
        return int(self._toml.get("metrics", {}).get("remote_write_batch_max_samples", 10000))

    @property
    def metrics_dedupe_ttl_seconds(self) -> int:
        """log_id 去重窗口 TTL（秒）。"""
        return int(self._toml.get("metrics", {}).get("dedupe_ttl_seconds", 86400))

    @property
    def metrics_snapshot_ttl_seconds(self) -> int:
        """Redis 快照 Hash TTL（秒，与设计 §4.3 一致，默认 600=10min）。"""
        return int(self._toml.get("metrics", {}).get("snapshot_ttl_seconds", 600))

    @property
    def metrics_snapshot_index_scan_batch_size(self) -> int:
        """Redis ZSet 索引单批扫描条数。"""
        return int(self._toml.get("metrics", {}).get("snapshot_index_scan_batch_size", 500))

    @property
    def metrics_snapshot_fallback_lookback(self) -> str:
        """TSDB 修复回退窗口（ISO 8601 Duration，默认 PT10M）。"""
        return str(self._toml.get("metrics", {}).get("snapshot_fallback_lookback", "PT10M"))

    @property
    def metrics_raw_retention_days(self) -> int:
        """原始数据 TSDB 保留天数。"""
        return int(self._toml.get("metrics", {}).get("raw_retention_days", 30))

    @property
    def metrics_downsample_retention_days(self) -> int:
        """降采样数据保留天数。"""
        return int(self._toml.get("metrics", {}).get("downsample_retention_days", 90))

    @property
    def metrics_max_points_per_series(self) -> int:
        """单条 series 最大返回点数。"""
        return int(self._toml.get("metrics", {}).get("max_points_per_series", 10000))

    @property
    def metrics_ranking_max_top_n(self) -> int:
        """rankings topN 上限。"""
        return int(self._toml.get("metrics", {}).get("ranking_max_top_n", 200))

    @property
    def metrics_slo_max_rules(self) -> int:
        """SLO 评估最大规则数。"""
        return int(self._toml.get("metrics", {}).get("slo_max_rules", 20))

    @property
    def metrics_capacity_default_lookback(self) -> str:
        """capacity/saturation 默认回看窗口（ISO 8601 Duration）。"""
        return str(self._toml.get("metrics", {}).get("capacity_default_lookback", "PT10M"))

    @property
    def metrics_capacity_default_active_ratio_threshold(self) -> float:
        """capacity 候选剪枝 activeRatio 阈值。"""
        value = float(self._toml.get("metrics", {}).get("capacity_default_active_ratio_threshold", 0.8))
        if not (0 < value <= 1):
            raise ValueError("metrics.capacity_default_active_ratio_threshold 必须在 (0, 1] 范围内")
        return value

    @property
    def metrics_capacity_default_queue_ratio_threshold(self) -> float:
        """capacity 候选剪枝 queueRatio 阈值。"""
        value = float(self._toml.get("metrics", {}).get("capacity_default_queue_ratio_threshold", 0.8))
        if not (0 < value <= 1):
            raise ValueError("metrics.capacity_default_queue_ratio_threshold 必须在 (0, 1] 范围内")
        return value

    @property
    def metrics_query_timeout_seconds(self) -> int:
        """TSDB 查询超时（秒）。"""
        return int(self._toml.get("metrics", {}).get("query_timeout_seconds", 30))

    @property
    def metrics_lagging_threshold_ms(self) -> int:
        """读模型滞后告警阈值（毫秒，spec §6.1.4 metrics 阈值默认 150000）。"""
        return int(self._toml.get("metrics", {}).get("lagging_threshold_ms", 150000))

    @property
    def metrics_lagging_response_mode(self) -> str:
        """读模型滞后时的响应模式："503" | "partial"。"""
        value = str(self._toml.get("metrics", {}).get("lagging_response_mode", "503"))
        if value not in {"503", "partial"}:
            raise ValueError(f"metrics.lagging_response_mode 必须为 '503' 或 'partial'，当前值：{value!r}")
        return value

    @property
    def metrics_analytics_enabled(self) -> bool:
        """是否启用 Analytics Profile（rankings 端点）。"""
        return bool(self._toml.get("metrics", {}).get("analytics_enabled", True))

    @property
    def metrics_governance_enabled(self) -> bool:
        """是否启用 Governance Profile（slo/capacity 端点）。"""
        return bool(self._toml.get("metrics", {}).get("governance_enabled", True))

    @property
    def metrics_metrics_log_interval_seconds(self) -> int:
        """metrics_log_loop 输出周期（秒）。"""
        return int(self._toml.get("metrics", {}).get("metrics_log_interval_seconds", 60))

    @property
    def vm_query_url(self) -> str:
        """VictoriaMetrics 查询端点基址（优先读 VM_QUERY_URL 环境变量，回退 TOML 配置）。

        tsdb.py 在此基址追加 /api/v1/query 等后缀（设计 §7.2）。
        """
        import os

        env_val = os.environ.get("VM_QUERY_URL", "").strip()
        if env_val:
            return env_val
        return str(self._toml.get("metrics", {}).get("vm_query_url", "http://localhost:8428"))

    @property
    def vm_remote_write_url(self) -> str:
        """VictoriaMetrics Remote Write 端点基址（优先读 VM_REMOTE_WRITE_URL 环境变量，回退 TOML 配置）。

        tsdb.py 在此基址追加 /api/v1/write 后缀（设计 §7.2）。
        """
        import os

        env_val = os.environ.get("VM_REMOTE_WRITE_URL", "").strip()
        if env_val:
            return env_val
        return str(self._toml.get("metrics", {}).get("vm_remote_write_url", "http://localhost:8428"))

    # ── ClickHouse（敏感/部署相关，环境变量优先） ──

    clickhouse_host: str = Field(default="localhost", validation_alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8123, validation_alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="default", validation_alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(default="", validation_alias="CLICKHOUSE_PASSWORD")
    clickhouse_database: str = Field(default="amp", validation_alias="CLICKHOUSE_DATABASE")

    # ── MinIO（敏感/部署相关，环境变量优先；archive.py 使用） ──

    minio_access_key: str = Field(default="admin", validation_alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="devpass", validation_alias="MINIO_SECRET_KEY")

    # ── OpenSearch（敏感/部署相关，环境变量优先；system 模块使用） ──

    opensearch_hosts: str = Field(default="http://localhost:9200", validation_alias="OPENSEARCH_HOSTS")
    opensearch_user: str = Field(default="", validation_alias="OPENSEARCH_USER")
    opensearch_password: str = Field(default="", validation_alias="OPENSEARCH_PASSWORD")
    opensearch_verify_certs: bool = Field(default=False, validation_alias="OPENSEARCH_VERIFY_CERTS")

    # ── Access ──

    @property
    def access_topic(self) -> str:
        """Access 日志 Kafka 主题。"""
        return str(self._toml.get("access", {}).get("topic", "amp.access"))

    @property
    def access_dlq_topic(self) -> str:
        """Access 日志死信队列主题。"""
        return str(self._toml.get("access", {}).get("dlq_topic", "amp.access.dlq"))

    @property
    def access_consumer_group(self) -> str:
        """Access Writer Kafka consumer group ID。"""
        return str(self._toml.get("access", {}).get("consumer_group", "monitor-server.access.writer.v1"))

    @property
    def access_writer_poll_timeout_ms(self) -> int:
        """Writer getmany poll 超时（毫秒）。"""
        return int(self._toml.get("access", {}).get("writer_poll_timeout_ms", 1000))

    @property
    def access_insert_batch_interval_seconds(self) -> int:
        """攒批写 ClickHouse 时间窗口（秒）。"""
        value = int(self._toml.get("access", {}).get("insert_batch_interval_seconds", 5))
        if value <= 0:
            raise ValueError("access.insert_batch_interval_seconds must be > 0")
        return value

    @property
    def access_insert_batch_max_rows(self) -> int:
        """单批最大行数。"""
        value = int(self._toml.get("access", {}).get("insert_batch_max_rows", 5000))
        if value <= 0:
            raise ValueError("access.insert_batch_max_rows must be > 0")
        return value

    @property
    def access_dedup_window_hours(self) -> int:
        """持久化去重窗口（小时，C-ACCESS-WRITE-3）。"""
        value = int(self._toml.get("access", {}).get("dedup_window_hours", 24))
        if value < 1:
            raise ValueError("access.dedup_window_hours must be >= 1")
        return value

    @property
    def access_raw_retention_days(self) -> int:
        """access_events 在线保留天数（查询保留窗口）。"""
        value = int(self._toml.get("access", {}).get("raw_retention_days", 30))
        if value <= 0:
            raise ValueError("access.raw_retention_days must be > 0")
        return value

    @property
    def access_archive_retention_days(self) -> int:
        """冷归档保留天数（须 >= raw_retention_days，跨键校验在 runtime）。"""
        value = int(self._toml.get("access", {}).get("archive_retention_days", 90))
        if value <= 0:
            raise ValueError("access.archive_retention_days must be > 0")
        return value

    @property
    def access_topology_retention_days(self) -> int:
        """access_topology_edge_5m 保留天数（须 >= raw_retention_days，跨键校验在 runtime）。"""
        value = int(self._toml.get("access", {}).get("topology_retention_days", 90))
        if value <= 0:
            raise ValueError("access.topology_retention_days must be > 0")
        return value

    @property
    def access_lagging_threshold_ms(self) -> int:
        """读模型滞后告警阈值（毫秒，spec §6.1.4 access=300000）。"""
        value = int(self._toml.get("access", {}).get("lagging_threshold_ms", 300000))
        if value <= 0:
            raise ValueError("access.lagging_threshold_ms must be > 0")
        return value

    @property
    def access_lagging_response_mode(self) -> str:
        """读模型滞后时的响应模式："503" | "partial"。"""
        value = str(self._toml.get("access", {}).get("lagging_response_mode", "503"))
        if value not in {"503", "partial"}:
            raise ValueError(f"access.lagging_response_mode 必须为 '503' 或 'partial'，当前值：{value!r}")
        return value

    @property
    def access_query_timeout_seconds(self) -> int:
        """ClickHouse 查询/连接超时（秒）。"""
        value = int(self._toml.get("access", {}).get("query_timeout_seconds", 30))
        if value <= 0:
            raise ValueError("access.query_timeout_seconds must be > 0")
        return value

    @property
    def access_trace_max_spans(self) -> int:
        """单 trace 最大返回 span 数（C-ACCESS-QUERY-13）。"""
        value = int(self._toml.get("access", {}).get("trace_max_spans", 10000))
        if value <= 0:
            raise ValueError("access.trace_max_spans must be > 0")
        return value

    @property
    def access_trace_max_duration_hours(self) -> int:
        """traces/query 分区裁剪双向外扩量（小时，须 >= 1）。"""
        value = int(self._toml.get("access", {}).get("trace_max_duration_hours", 1))
        if value < 1:
            raise ValueError("access.trace_max_duration_hours must be >= 1")
        return value

    @property
    def access_slow_top_max_n(self) -> int:
        """慢请求 TopN 上限。"""
        value = int(self._toml.get("access", {}).get("slow_top_max_n", 200))
        if value <= 0:
            raise ValueError("access.slow_top_max_n must be > 0")
        return value

    @property
    def access_error_attribution_max_n(self) -> int:
        """错误归因 TopN 上限。"""
        value = int(self._toml.get("access", {}).get("error_attribution_max_n", 200))
        if value <= 0:
            raise ValueError("access.error_attribution_max_n must be > 0")
        return value

    @property
    def access_error_status_threshold(self) -> int:
        """错误判定状态码下界（400 ≤ v ≤ 599，C-ACCESS-QUERY-15）。

        改变此值需同步重建 access_topology_edge_5m_mv（validate_access_config 中会发出告警）。
        """
        value = int(self._toml.get("access", {}).get("error_status_threshold", 500))
        if not (400 <= value <= 599):
            raise ValueError(f"access.error_status_threshold 必须在 [400, 599] 范围内，当前值：{value}")
        return value

    @property
    def access_redacted_header_allowlist(self) -> str:
        """header 白名单（逗号分隔，C-ACCESS-WRITE-2）。"""
        return str(self._toml.get("access", {}).get("redacted_header_allowlist", "content-type,x-request-id"))

    @property
    def access_raw_log_enabled(self) -> bool:
        """是否存储 raw_log 列（C-ACCESS-QUERY-8）。"""
        return bool(self._toml.get("access", {}).get("raw_log_enabled", False))

    @property
    def access_trace_seen_hint_enabled(self) -> bool:
        """是否启用 trace hint cache（Redis Set）。"""
        return bool(self._toml.get("access", {}).get("trace_seen_hint_enabled", False))

    @property
    def access_archive_enabled(self) -> bool:
        """是否启用 Parquet 冷归档后台任务（依赖 acps-infra 对象存储）。"""
        return bool(self._toml.get("access", {}).get("archive_enabled", False))

    @property
    def access_archive_interval_seconds(self) -> int:
        """冷归档任务执行周期（秒）。"""
        value = int(self._toml.get("access", {}).get("archive_interval_seconds", 3600))
        if value <= 0:
            raise ValueError("access.archive_interval_seconds must be > 0")
        return value

    @property
    def access_minio_endpoint(self) -> str:
        """MinIO（S3 兼容）服务端点 URL（含协议和端口）。"""

        import os

        env_val = os.environ.get("MINIO_ENDPOINT", "").strip()
        if env_val:
            return env_val
        return str(self._toml.get("access", {}).get("minio_endpoint", "http://localhost:19000"))

    @property
    def access_minio_bucket(self) -> str:
        """归档 Parquet 文件存放的 MinIO 存储桶名。"""
        return str(self._toml.get("access", {}).get("minio_bucket", "amp-access-archive"))

    @property
    def access_minio_secure(self) -> bool:
        """MinIO 连接是否使用 TLS（生产环境设 true）。"""
        return bool(self._toml.get("access", {}).get("minio_secure", False))

    @property
    def access_analytics_enabled(self) -> bool:
        """是否暴露 Analytics Profile 端点（errors/attribution、slow-requests/top）。"""
        return bool(self._toml.get("access", {}).get("analytics_enabled", True))

    @property
    def access_apm_enabled(self) -> bool:
        """是否暴露 APM Profile 端点（traces/query、traces/{traceId}、topology/query）。"""
        return bool(self._toml.get("access", {}).get("apm_enabled", True))

    @property
    def access_metrics_log_interval_seconds(self) -> int:
        """进程内指标 metrics_log_loop 输出周期（秒）。"""
        value = int(self._toml.get("access", {}).get("metrics_log_interval_seconds", 60))
        if value <= 0:
            raise ValueError("access.metrics_log_interval_seconds must be > 0")
        return value

    # ── Message ──

    @property
    def message_topic(self) -> str:
        """Message 日志 Kafka 主题。"""
        return str(self._toml.get("message", {}).get("topic", "amp.message"))

    @property
    def message_dlq_topic(self) -> str:
        """Message 日志死信队列主题。"""
        return str(self._toml.get("message", {}).get("dlq_topic", "amp.message.dlq"))

    @property
    def message_consumer_group(self) -> str:
        """Message Writer Kafka consumer group ID。"""
        return str(self._toml.get("message", {}).get("consumer_group", "monitor-server.message.writer.v1"))

    @property
    def message_writer_poll_timeout_ms(self) -> int:
        """Writer getmany poll 超时（毫秒）。"""
        return int(self._toml.get("message", {}).get("writer_poll_timeout_ms", 1000))

    @property
    def message_insert_batch_interval_seconds(self) -> int:
        """攒批写 ClickHouse 时间窗口（秒）。"""
        value = int(self._toml.get("message", {}).get("insert_batch_interval_seconds", 5))
        if value <= 0:
            raise ValueError("message.insert_batch_interval_seconds must be > 0")
        return value

    @property
    def message_insert_batch_max_rows(self) -> int:
        """单批最大行数。"""
        value = int(self._toml.get("message", {}).get("insert_batch_max_rows", 5000))
        if value <= 0:
            raise ValueError("message.insert_batch_max_rows must be > 0")
        return value

    @property
    def message_kafka_retention_seconds(self) -> int:
        """amp.message Kafka topic 的 retention（秒）；去重窗口须 >= 此值（C-MESSAGE-WRITE-2）。"""
        value = int(self._toml.get("message", {}).get("kafka_retention_seconds", 21600))
        if value <= 0:
            raise ValueError("message.kafka_retention_seconds must be > 0")
        return value

    @property
    def message_dedup_window_seconds(self) -> int:
        """持久化去重窗口（秒，必须 >= kafka_retention_seconds，C-MESSAGE-WRITE-2）。"""
        value = int(self._toml.get("message", {}).get("dedup_window_seconds", 21600))
        if value <= 0:
            raise ValueError("message.dedup_window_seconds must be > 0")
        return value

    @property
    def message_lifecycle_compact_interval_seconds(self) -> int:
        """Lifecycle Compactor 运行间隔（秒）。"""
        value = int(self._toml.get("message", {}).get("lifecycle_compact_interval_seconds", 60))
        if value <= 0:
            raise ValueError("message.lifecycle_compact_interval_seconds must be > 0")
        return value

    @property
    def message_destination_stats_compact_interval_seconds(self) -> int:
        """Throughput Compactor 运行间隔（秒）。"""
        value = int(self._toml.get("message", {}).get("destination_stats_compact_interval_seconds", 60))
        if value <= 0:
            raise ValueError("message.destination_stats_compact_interval_seconds must be > 0")
        return value

    @property
    def message_compaction_overlap_seconds(self) -> int:
        """Compactor overlap 回看秒数（两 compactor 共用，C-MESSAGE-MODEL-7）。"""
        value = int(self._toml.get("message", {}).get("compaction_overlap_seconds", 300))
        if value < 0:
            raise ValueError("message.compaction_overlap_seconds must be >= 0")
        return value

    @property
    def message_raw_retention_days(self) -> int:
        """message_events 在线保留天数。"""
        value = int(self._toml.get("message", {}).get("raw_retention_days", 7))
        if value <= 0:
            raise ValueError("message.raw_retention_days must be > 0")
        return value

    @property
    def message_raw_archive_retention_days(self) -> int:
        """冷归档保留天数（须 >= lifecycle_retention_days，跨键校验在 runtime）。"""
        value = int(self._toml.get("message", {}).get("raw_archive_retention_days", 30))
        if value <= 0:
            raise ValueError("message.raw_archive_retention_days must be > 0")
        return value

    @property
    def message_lifecycle_retention_days(self) -> int:
        """message_lifecycle 保留天数（须 >= raw_retention_days，C-MESSAGE-RETENTION-2）。"""
        value = int(self._toml.get("message", {}).get("lifecycle_retention_days", 30))
        if value <= 0:
            raise ValueError("message.lifecycle_retention_days must be > 0")
        return value

    @property
    def message_destination_state_retention_days(self) -> int:
        """message_destination_state_snapshot 保留天数。"""
        value = int(self._toml.get("message", {}).get("destination_state_retention_days", 30))
        if value <= 0:
            raise ValueError("message.destination_state_retention_days must be > 0")
        return value

    @property
    def message_destination_stats_retention_days(self) -> int:
        """message_destination_stats_5m 保留天数。"""
        value = int(self._toml.get("message", {}).get("destination_stats_retention_days", 30))
        if value <= 0:
            raise ValueError("message.destination_stats_retention_days must be > 0")
        return value

    @property
    def message_state_collect_interval_seconds(self) -> int:
        """State Collector 采集间隔（秒）。"""
        value = int(self._toml.get("message", {}).get("state_collect_interval_seconds", 60))
        if value <= 0:
            raise ValueError("message.state_collect_interval_seconds must be > 0")
        return value

    @property
    def message_lagging_threshold_ms(self) -> int:
        """读模型滞后告警阈值（毫秒）。"""
        value = int(self._toml.get("message", {}).get("lagging_threshold_ms", 300000))
        if value <= 0:
            raise ValueError("message.lagging_threshold_ms must be > 0")
        return value

    @property
    def message_query_timeout_seconds(self) -> int:
        """ClickHouse 查询超时（秒）。"""
        value = int(self._toml.get("message", {}).get("query_timeout_seconds", 30))
        if value <= 0:
            raise ValueError("message.query_timeout_seconds must be > 0")
        return value

    @property
    def message_destination_query_max_groups(self) -> int:
        """destinations/query 最大分组数。"""
        value = int(self._toml.get("message", {}).get("destination_query_max_groups", 200))
        if value <= 0:
            raise ValueError("message.destination_query_max_groups must be > 0")
        return value

    @property
    def message_deadletter_query_max_n(self) -> int:
        """deadletters/query 结果上限（clamp_deadletter_n 用，设计 §7）。"""
        value = int(self._toml.get("message", {}).get("deadletter_query_max_n", 200))
        if value <= 0:
            raise ValueError("message.deadletter_query_max_n must be > 0")
        return value

    @property
    def message_raw_log_enabled(self) -> bool:
        """是否存储 raw_log 列并允许 includeRawLog（C-MESSAGE-WRITE-4 / C-MESSAGE-QUERY-9）。"""
        return bool(self._toml.get("message", {}).get("raw_log_enabled", False))

    @property
    def message_lagging_response_mode(self) -> str:
        """读模型滞后时的响应模式："503" | "partial"。"""
        value = str(self._toml.get("message", {}).get("lagging_response_mode", "partial"))
        if value not in {"503", "partial"}:
            raise ValueError(f"message.lagging_response_mode 必须为 '503' 或 'partial'，当前值：{value!r}")
        return value

    @property
    def message_correlation_id_stable_unique(self) -> bool:
        """是否声明 correlationId 稳定唯一（门控 lifecycle_key 的 cid: 路径，设计 §2.3）。"""
        return bool(self._toml.get("message", {}).get("correlation_id_stable_unique", False))

    @property
    def message_writer_enabled(self) -> bool:
        """是否启动 MessageWriter 后台任务。"""
        return bool(self._toml.get("message", {}).get("writer_enabled", True))

    @property
    def message_reliability_enabled(self) -> bool:
        """是否启用 Reliability Profile（lifecycles/* + deadletters 端点 + Lifecycle Compactor）。"""
        return bool(self._toml.get("message", {}).get("reliability_enabled", True))

    @property
    def message_destination_enabled(self) -> bool:
        """是否启用 Destination Profile（destinations/throughput 端点 + Throughput Compactor）。"""
        return bool(self._toml.get("message", {}).get("destination_enabled", True))

    @property
    def message_state_collector_enabled(self) -> bool:
        """是否启动 State Collector 及挂 destinations/query 端点（默认关，无 broker 源时不误挂）。"""
        return bool(self._toml.get("message", {}).get("state_collector_enabled", False))

    @property
    def message_destination_source_kind(self) -> str:
        """DestinationStateSource 实现选择（'null' | 'rabbitmq' | ...，设计 §6.16 工厂）。"""
        return str(self._toml.get("message", {}).get("destination_source_kind", "null"))

    @property
    def message_archive_enabled(self) -> bool:
        """是否启动冷归档后台任务（§6.21）。"""
        return bool(self._toml.get("message", {}).get("archive_enabled", False))

    @property
    def message_archive_interval_seconds(self) -> int:
        """冷归档任务执行周期（秒）。"""
        value = int(self._toml.get("message", {}).get("archive_interval_seconds", 3600))
        if value <= 0:
            raise ValueError("message.archive_interval_seconds must be > 0")
        return value

    @property
    def message_metrics_log_interval_seconds(self) -> int:
        """进程内指标 metrics_log_loop 输出周期（秒）。"""
        value = int(self._toml.get("message", {}).get("metrics_log_interval_seconds", 60))
        if value <= 0:
            raise ValueError("message.metrics_log_interval_seconds must be > 0")
        return value

    # ── System ──

    @property
    def system_topic(self) -> str:
        """System 日志 Kafka 主题。"""
        return str(self._toml.get("system", {}).get("topic", "amp.system"))

    @property
    def system_dlq_topic(self) -> str:
        """System 日志死信队列主题。"""
        return str(self._toml.get("system", {}).get("dlq_topic", "amp.system.dlq"))

    @property
    def system_consumer_group(self) -> str:
        """System Writer Kafka consumer group ID。"""
        return str(self._toml.get("system", {}).get("consumer_group", "monitor-server.system.writer.v1"))

    @property
    def system_writer_poll_timeout_ms(self) -> int:
        """Writer getmany poll 超时（毫秒）。"""
        return int(self._toml.get("system", {}).get("writer_poll_timeout_ms", 1000))

    @property
    def system_bulk_index_batch_interval_seconds(self) -> int:
        """攒批写 OpenSearch 时间窗口（秒）。"""
        value = int(self._toml.get("system", {}).get("bulk_index_batch_interval_seconds", 5))
        if value <= 0:
            raise ValueError("system.bulk_index_batch_interval_seconds must be > 0")
        return value

    @property
    def system_bulk_index_batch_max_docs(self) -> int:
        """单批最大文档数。"""
        value = int(self._toml.get("system", {}).get("bulk_index_batch_max_docs", 5000))
        if value <= 0:
            raise ValueError("system.bulk_index_batch_max_docs must be > 0")
        return value

    @property
    def system_event_hot_retention_days(self) -> int:
        """Hot 段保留天数（ISM hot 阶段）。"""
        value = int(self._toml.get("system", {}).get("event_hot_retention_days", 3))
        if value <= 0:
            raise ValueError("system.event_hot_retention_days must be > 0")
        return value

    @property
    def system_event_warm_retention_days(self) -> int:
        """Warm 段保留天数（须 >= event_hot_retention_days，跨键校验在 runtime）。"""
        value = int(self._toml.get("system", {}).get("event_warm_retention_days", 14))
        if value <= 0:
            raise ValueError("system.event_warm_retention_days must be > 0")
        return value

    @property
    def system_archive_retention_days(self) -> int:
        """归档保留天数（须 >= event_warm_retention_days，跨键校验在 runtime）。"""
        value = int(self._toml.get("system", {}).get("archive_retention_days", 30))
        if value <= 0:
            raise ValueError("system.archive_retention_days must be > 0")
        return value

    @property
    def system_lagging_threshold_ms(self) -> int:
        """读模型滞后告警阈值（毫秒）。"""
        value = int(self._toml.get("system", {}).get("lagging_threshold_ms", 300000))
        if value <= 0:
            raise ValueError("system.lagging_threshold_ms must be > 0")
        return value

    @property
    def system_query_timeout_seconds(self) -> int:
        """OpenSearch 查询超时（秒）。"""
        value = int(self._toml.get("system", {}).get("query_timeout_seconds", 30))
        if value <= 0:
            raise ValueError("system.query_timeout_seconds must be > 0")
        return value

    @property
    def system_keyword_min_length(self) -> int:
        """关键字最小长度（C-SYSTEM-QUERY-4）。"""
        value = int(self._toml.get("system", {}).get("keyword_min_length", 3))
        if value <= 0:
            raise ValueError("system.keyword_min_length must be > 0")
        return value

    @property
    def system_search_text_max_length(self) -> int:
        """search_text 文本投影最大字节数（C-SYSTEM-WRITE-5）。"""
        value = int(self._toml.get("system", {}).get("search_text_max_length", 8192))
        if value <= 0:
            raise ValueError("system.search_text_max_length must be > 0")
        return value

    @property
    def system_lagging_response_mode(self) -> str:
        """读模型滞后时的响应模式："503" | "partial"。"""
        value = str(self._toml.get("system", {}).get("lagging_response_mode", "partial"))
        if value not in {"503", "partial"}:
            raise ValueError(f"system.lagging_response_mode 必须为 '503' 或 'partial'，当前值：{value!r}")
        return value

    @property
    def system_freshness_reorder_margin_ms(self) -> int:
        """保守水位的迟到 + refresh 裕量（毫秒，须 >= 0，设计 §2.4）。"""
        value = int(self._toml.get("system", {}).get("freshness_reorder_margin_ms", 30000))
        if value < 0:
            raise ValueError("system.freshness_reorder_margin_ms must be >= 0")
        return value

    @property
    def system_keyword_only_max_window_seconds(self) -> int:
        """keyword-only 查询允许的最大时间窗（秒，C-SYSTEM-QUERY-4）。"""
        value = int(self._toml.get("system", {}).get("keyword_only_max_window_seconds", 3600))
        if value <= 0:
            raise ValueError("system.keyword_only_max_window_seconds must be > 0")
        return value

    @property
    def system_pit_keep_alive(self) -> str:
        """PIT 存活时间（如 "5m"，设计 §3.2 步骤 5）。"""
        value = str(self._toml.get("system", {}).get("pit_keep_alive", "5m"))
        if not value.strip():
            raise ValueError("system.pit_keep_alive must not be empty")
        return value

    @property
    def system_index_number_of_shards(self) -> int:
        """index template number_of_shards（设计 §4.1）。"""
        value = int(self._toml.get("system", {}).get("index_number_of_shards", 3))
        if value <= 0:
            raise ValueError("system.index_number_of_shards must be > 0")
        return value

    @property
    def system_index_number_of_replicas(self) -> int:
        """index template number_of_replicas（设计 §4.1）。"""
        value = int(self._toml.get("system", {}).get("index_number_of_replicas", 1))
        if value < 0:
            raise ValueError("system.index_number_of_replicas must be >= 0")
        return value

    @property
    def system_writer_enabled(self) -> bool:
        """是否启动 SystemWriter 后台任务。"""
        return bool(self._toml.get("system", {}).get("writer_enabled", True))

    @property
    def system_query_enabled(self) -> bool:
        """是否注册 events/query 端点（只写部署可关）。"""
        return bool(self._toml.get("system", {}).get("query_enabled", True))

    @property
    def system_archive_enabled(self) -> bool:
        """是否启动 ISM 补挂 + 冷归档后台任务。"""
        return bool(self._toml.get("system", {}).get("archive_enabled", False))

    @property
    def system_archive_interval_seconds(self) -> int:
        """维护任务执行周期（秒）。"""
        value = int(self._toml.get("system", {}).get("archive_interval_seconds", 3600))
        if value <= 0:
            raise ValueError("system.archive_interval_seconds must be > 0")
        return value

    @property
    def system_metrics_log_interval_seconds(self) -> int:
        """进程内指标 metrics_log_loop 输出周期（秒）。"""
        value = int(self._toml.get("system", {}).get("metrics_log_interval_seconds", 60))
        if value <= 0:
            raise ValueError("system.metrics_log_interval_seconds must be > 0")
        return value

    # ── ATR / CA ──

    @property
    def atr_ca_base_url(self) -> str:
        """CA 服务地址，用于按证书序列号查询公钥（GET /acps-atr-v2/ca/keys/{serial}）。"""
        return str(self._toml.get("atr", {}).get("ca_base_url", "http://localhost:9003"))

    @property
    def atr_mock_mode(self) -> bool:
        """是否 mock ATR 响应（开发/测试环境使用）。"""
        return bool(self._toml.get("atr", {}).get("mock_mode", True))

    @property
    def atr_key_cache_ttl_seconds(self) -> int:
        """验签公钥内存缓存 TTL（秒），按 (aic, kid) 绑定。"""
        value = int(self._toml.get("atr", {}).get("key_cache_ttl_seconds", 300))
        if value <= 0:
            raise ValueError("atr.key_cache_ttl_seconds must be > 0")
        return value

    # ── Logging ──

    @property
    def log_level(self) -> str:
        """日志级别。"""
        return str(self._toml.get("logging", {}).get("level", "INFO"))

    @property
    def log_format(self) -> str:
        """日志格式：json（生产）/ console（开发）。"""
        return str(self._toml.get("logging", {}).get("format", "json"))

    # ── CORS ──

    @property
    def cors_enabled(self) -> bool:
        """是否启用 CORS。"""
        return bool(self._toml.get("cors", {}).get("enabled", False))

    @property
    def cors_origins(self) -> list[str]:
        """CORS 允许的 origin 列表。"""
        raw = self._toml.get("cors", {}).get("origins", [])
        if not isinstance(raw, list):
            return []
        return [str(o) for o in raw]

    @property
    def cors_allow_credentials(self) -> bool:
        """是否允许 CORS credentials。"""
        return bool(self._toml.get("cors", {}).get("allow_credentials", False))

    @property
    def cors_allow_methods(self) -> list[str]:
        """CORS 允许的 HTTP 方法。"""
        raw = self._toml.get("cors", {}).get("allow_methods", ["*"])
        if not isinstance(raw, list):
            return ["*"]
        return [str(m) for m in raw]

    @property
    def cors_allow_headers(self) -> list[str]:
        """CORS 允许的请求头。"""
        raw = self._toml.get("cors", {}).get("allow_headers", ["*"])
        if not isinstance(raw, list):
            return ["*"]
        return [str(h) for h in raw]

    @property
    def cors_expose_headers(self) -> list[str]:
        """CORS 暴露给客户端的响应头。"""
        raw = self._toml.get("cors", {}).get("expose_headers", [])
        if not isinstance(raw, list):
            return []
        return [str(h) for h in raw]

    @property
    def cors_max_age(self) -> int:
        """CORS 预检结果缓存时间（秒）。"""
        return int(self._toml.get("cors", {}).get("max_age", 600))

    # ── OIDC / Authorization ──

    @property
    def oidc_enabled(self) -> bool:
        """是否启用 OIDC access token 鉴权。"""
        env_value = _env_bool("MONITOR_OIDC_ENABLED")
        if env_value is not None:
            return env_value
        return bool(self._toml.get("oidc", {}).get("enabled", False))

    @property
    def oidc_issuer(self) -> str:
        """OIDC issuer URL。"""
        return _env_string("MONITOR_OIDC_ISSUER") or str(self._toml.get("oidc", {}).get("issuer", ""))

    @property
    def oidc_audience(self) -> str:
        """Monitor API audience。"""
        return _env_string("MONITOR_OIDC_AUDIENCE") or str(self._toml.get("oidc", {}).get("audience", "monitor-api"))

    @property
    def oidc_allowed_azp(self) -> list[str]:
        """允许的 authorized party 列表。"""
        return _env_string_list("MONITOR_OIDC_ALLOWED_AZP") or list(
            self._toml.get("oidc", {}).get("allowed_azp", ["monitor-web"])
        )

    @property
    def oidc_client_id(self) -> str:
        """本地资源服务器标识。"""
        return _env_string("MONITOR_OIDC_CLIENT_ID") or str(self._toml.get("oidc", {}).get("client_id", "monitor-api"))

    @property
    def oidc_algorithms(self) -> list[str]:
        """允许的 JWT 签名算法。"""
        return _env_string_list("MONITOR_OIDC_ALGORITHMS") or list(
            self._toml.get("oidc", {}).get("algorithms", ["EdDSA"])
        )

    @property
    def oidc_jwks_cache_ttl_seconds(self) -> int:
        """JWKS 缓存 TTL。"""
        env_value = _env_int("MONITOR_OIDC_JWKS_CACHE_TTL_SECONDS")
        if env_value is not None:
            return env_value
        return int(self._toml.get("oidc", {}).get("jwks_cache_ttl_seconds", 300))

    @property
    def oidc_discovery_cache_ttl_seconds(self) -> int:
        """OIDC discovery 缓存 TTL。"""
        env_value = _env_int("MONITOR_OIDC_DISCOVERY_CACHE_TTL_SECONDS")
        if env_value is not None:
            return env_value
        return int(self._toml.get("oidc", {}).get("discovery_cache_ttl_seconds", 300))

    @property
    def oidc_leeway_seconds(self) -> int:
        """JWT 时间类 claim 的 leeway。"""
        env_value = _env_int("MONITOR_OIDC_LEEWAY_SECONDS")
        if env_value is not None:
            return env_value
        return int(self._toml.get("oidc", {}).get("leeway_seconds", 60))

    @property
    def oidc_require_https(self) -> bool:
        """是否强制 OIDC 元数据和 JWKS 使用 HTTPS。"""
        env_value = _env_bool("MONITOR_OIDC_REQUIRE_HTTPS")
        if env_value is not None:
            return env_value
        return bool(self._toml.get("oidc", {}).get("require_https", True))

    @property
    def oidc_role_source_client_id(self) -> str:
        """从 Keycloak resource_access 读取角色时使用的 client id。"""
        return _env_string("MONITOR_OIDC_ROLE_SOURCE_CLIENT_ID") or str(
            self._toml.get("oidc", {}).get("role_source_client_id", self.oidc_client_id)
        )

    @property
    def authorization_global_admin_roles(self) -> list[str]:
        """全局 admin 角色集合。"""
        raw = self._toml.get("authorization", {}).get("global_admin_roles", ["admin"])
        return [str(item) for item in raw] if isinstance(raw, list) else ["admin"]

    @property
    def authorization_global_operator_roles(self) -> list[str]:
        """全局 operator 角色集合。"""
        raw = self._toml.get("authorization", {}).get("global_operator_roles", ["operator", "admin"])
        return [str(item) for item in raw] if isinstance(raw, list) else ["operator", "admin"]

    @property
    def authorization_global_auditor_roles(self) -> list[str]:
        """全局 auditor 角色集合。"""
        raw = self._toml.get("authorization", {}).get("global_auditor_roles", ["auditor", "admin"])
        return [str(item) for item in raw] if isinstance(raw, list) else ["auditor", "admin"]

    @property
    def authorization_default_read_roles(self) -> list[str]:
        """默认可读角色集合。"""
        raw = self._toml.get("authorization", {}).get("default_read_roles", ["viewer", "auditor", "operator", "admin"])
        return [str(item) for item in raw] if isinstance(raw, list) else ["viewer", "auditor", "operator", "admin"]

    # ── Project ──

    @property
    def project_name(self) -> str:
        """项目名称。"""
        return str(self._toml.get("project", {}).get("name", "monitor-server"))

    @property
    def project_version(self) -> str:
        """项目版本。"""
        return str(self._toml.get("project", {}).get("version", "2.2.0"))


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。

    Returns:
        Settings: 全局配置对象。
    """
    return Settings()  # pyright: ignore[reportCallIssue]


# 模块级单例，通过 `from app.core.config import settings` 访问
settings = get_settings()
