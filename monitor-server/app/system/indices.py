"""app/system/indices.py — 索引名/模板/ISM 常量（schema 单一真相源，纯函数）。

amp-system-events-* 的 mapping、ISM 策略、字段常量集中此处。
bootstrap 执行（PUT template / PUT ISM policy）在 store.ensure_system_schema()。
所有直接字符串字面量通过 FIELD_* 常量引用，杜绝散落字符串。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

INDEX_PREFIX: Final = "amp-system-events"
INDEX_PATTERN: Final = "amp-system-events-*"
INDEX_TEMPLATE_NAME: Final = "amp-system-events-template"
ISM_POLICY_NAME: Final = "amp-system-events-ism"

# ── 文档字段常量（dsl/normalizer/store 引用，杜绝散落字符串字面量） ──────────────

FIELD_LOG_ID: Final = "log_id"
FIELD_TIMESTAMP: Final = "timestamp"
FIELD_INDEXED_AT: Final = "indexed_at"
FIELD_AIC: Final = "aic"
FIELD_TRACE_ID: Final = "trace_id"
FIELD_CORRELATION_ID: Final = "correlation_id"
FIELD_SEVERITY_NUMBER: Final = "severity_number"
FIELD_SEVERITY_TEXT: Final = "severity_text"
FIELD_MESSAGE: Final = "message"
FIELD_MESSAGE_KEYWORD: Final = "message.keyword"
FIELD_CATEGORY: Final = "category"
FIELD_COMPONENT: Final = "component"
FIELD_MODULE: Final = "module"
FIELD_TAGS: Final = "tags"
FIELD_SEARCH_TEXT: Final = "search_text"
FIELD_RAW_BODY: Final = "raw_body"

# 文档 source 投影列（SystemEventView 映射用，不含 search_text/indexed_at/raw_body）
# C-SYSTEM-QUERY-3：search_text/indexed_at 为内部字段，永不出参；raw_body 由 includeRawLog 门控
EVENT_SOURCE_FIELDS: Final[tuple[str, ...]] = (
    FIELD_LOG_ID,
    FIELD_TIMESTAMP,
    FIELD_AIC,
    FIELD_TRACE_ID,
    FIELD_CORRELATION_ID,
    FIELD_SEVERITY_NUMBER,
    FIELD_SEVERITY_TEXT,
    FIELD_MESSAGE,
    FIELD_CATEGORY,
    FIELD_COMPONENT,
    FIELD_MODULE,
    FIELD_TAGS,
)


def index_for_timestamp(ts_ms: int) -> str:
    """事件时间（UTC 毫秒）→ 按日索引名 amp-system-events-YYYYMMDD。

    用事件 timestamp 而非 datetime.now()：迟到事件落到其业务日索引，
    与 ISM 索引年龄语义一致（设计 §3.1 步骤 7）。
    """
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
    return f"{INDEX_PREFIX}-{dt.strftime('%Y%m%d')}"


def build_index_template(*, number_of_shards: int, number_of_replicas: int) -> dict[str, Any]:
    """返回 PUT _index_template/{name} 的完整 body（设计 §4.1 mapping）。

    OpenSearch index template 结构：顶层含 index_patterns / template / priority。
    ISM 靠 ism_template 自动挂载新索引，settings **不含**废弃的 policy_id（设计 §3.3）。
    """
    return {
        "index_patterns": [INDEX_PATTERN],
        "priority": 100,
        "template": {
            "settings": {
                "number_of_shards": number_of_shards,
                "number_of_replicas": number_of_replicas,
            },
            "mappings": {
                "properties": {
                    FIELD_LOG_ID: {"type": "keyword"},
                    FIELD_TIMESTAMP: {"type": "date"},
                    FIELD_INDEXED_AT: {"type": "date"},
                    FIELD_AIC: {"type": "keyword"},
                    FIELD_TRACE_ID: {"type": "keyword"},
                    FIELD_CORRELATION_ID: {"type": "keyword"},
                    FIELD_SEVERITY_NUMBER: {"type": "short"},
                    FIELD_SEVERITY_TEXT: {"type": "keyword"},
                    FIELD_MESSAGE: {
                        "type": "text",
                        "fields": {
                            "keyword": {
                                "type": "keyword",
                                "ignore_above": 256,
                            }
                        },
                    },
                    FIELD_CATEGORY: {"type": "keyword"},
                    FIELD_COMPONENT: {"type": "keyword"},
                    FIELD_MODULE: {"type": "keyword"},
                    FIELD_TAGS: {"type": "flat_object"},
                    FIELD_SEARCH_TEXT: {"type": "text"},
                    FIELD_RAW_BODY: {"type": "object", "enabled": False},
                }
            },
        },
    }


def build_ism_policy(*, hot_days: int, warm_days: int, archive_days: int) -> dict[str, Any]:
    """返回 PUT _plugins/_ism/policies/{name} 的完整 body（设计 §3.3）。

    states: hot →(min_index_age=hot_days)→ warm →(warm_days)→ cold(read_only) →(archive_days)→ delete。
    ism_template.index_patterns 实现自动挂载（非废弃 policy_id）。
    """
    return {
        "policy": {
            "description": "System events index lifecycle: hot → warm → cold → delete",
            "default_state": "hot",
            "states": [
                {
                    "name": "hot",
                    "actions": [],
                    "transitions": [
                        {
                            "state_name": "warm",
                            "conditions": {
                                "min_index_age": f"{hot_days}d",
                            },
                        }
                    ],
                },
                {
                    "name": "warm",
                    "actions": [{"read_only": {}}],
                    "transitions": [
                        {
                            "state_name": "cold",
                            "conditions": {
                                "min_index_age": f"{warm_days}d",
                            },
                        }
                    ],
                },
                {
                    "name": "cold",
                    "actions": [{"read_only": {}}],
                    "transitions": [
                        {
                            "state_name": "delete",
                            "conditions": {
                                "min_index_age": f"{archive_days}d",
                            },
                        }
                    ],
                },
                {
                    "name": "delete",
                    "actions": [{"delete": {}}],
                    "transitions": [],
                },
            ],
            "ism_template": [
                {
                    "index_patterns": [INDEX_PATTERN],
                    "priority": 100,
                }
            ],
        }
    }


def query_index_target() -> str:
    """查询目标：events/query 只读 amp-system-events-*（C-SYSTEM-QUERY-1）。"""
    return INDEX_PATTERN
