from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

WEBHOOK_URL_DESCRIPTION = "回调URL"
WEBHOOK_TYPES_DESCRIPTION = "关注的数据类型列表"
WEBHOOK_EVENTS_DESCRIPTION = "关注的事件类型列表"
WEBHOOK_DESCRIPTION = "WebHook描述"
WEBHOOK_EXAMPLE_TIMESTAMP = "2025-08-19T12:15:30Z"


class Envelope(BaseModel):
    """数据同步协议的信封格式"""

    seq: int = Field(..., description="全局递增序号")
    ts: datetime | None = Field(None, description="变更时间戳")
    op: str | None = Field("upsert", description="操作类型：upsert或delete，缺省为upsert")
    type: str = Field(..., description="对象类型")
    id: str = Field(..., description="对象全局唯一ID")
    version: int = Field(..., description="对象版本号")
    payload: dict[str, Any] | None = Field(None, description="实际数据")

    model_config = ConfigDict(from_attributes=True)


class ChangeLogResponse(BaseModel):
    """变更日志响应模型"""

    seq: int
    ts: datetime
    type: str
    id: str
    version: int
    payload: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class SnapshotResponse(BaseModel):
    """快照响应模型"""

    snapshot_id: str = Field(..., description="快照唯一标识符")
    snapshot_seq: int = Field(..., description="快照对应的序列号")
    chunk_index: int = Field(..., description="当前块索引")
    chunk_total: int = Field(..., description="总块数")
    object_count: int = Field(..., description="快照包含的总对象数量")

    model_config = ConfigDict(from_attributes=True)


class SnapshotInfo(BaseModel):
    """快照信息模型"""

    id: str = Field(..., description="快照唯一标识符")
    types: str = Field(..., description="数据类型")
    seq: int = Field(..., description="快照切点序列号")
    chunk_total: int = Field(..., description="总块数")
    object_count: int = Field(..., description="对象总数")
    from_seq: int | None = Field(None, description="增量快照起始序列号")
    is_deleted: bool = Field(..., description="是否已删除")
    created_at: datetime = Field(..., description="创建时间")
    last_access_at: datetime = Field(..., description="最后访问时间")
    expire_at: datetime = Field(..., description="过期时间")

    model_config = ConfigDict(from_attributes=True)


class ChangesRequest(BaseModel):
    """增量变更请求模型"""

    types: str | None = Field(None, description="数据类型，逗号分隔")
    seq: int | None = Field(None, description="起始序列号")
    limit: int = Field(1000, description="返回条数限制")
    wait: str | None = Field(None, description="长轮询等待时间")

    model_config = ConfigDict(from_attributes=True)


class SnapshotRequest(BaseModel):
    """快照请求模型"""

    types: str | None = Field(None, description="数据类型，逗号分隔")
    limit: int = Field(10000, description="每块最大对象数量")
    from_seq: int | None = Field(None, description="增量快照的起始序号")
    snapshot_id: str | None = Field(None, description="快照ID，用于获取后续块")
    chunk: int | None = Field(None, description="块索引")

    model_config = ConfigDict(from_attributes=True)


class InfoResponse(BaseModel):
    """系统信息响应模型 - 严格遵循 DSP 协议规范"""

    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="服务版本号")
    status: str = Field(..., description="服务健康状态")
    supported_types: list[str] = Field(..., description="支持的对象类型列表")
    retention: dict[str, Any] = Field(..., description="数据保留配置")
    snapshot: dict[str, Any] = Field(..., description="快照配置")
    changes: dict[str, Any] = Field(..., description="变更流配置")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "service": "agent-registry",
                "version": "1.0.0",
                "status": "healthy",
                "supported_types": ["acs"],
                "retention": {
                    "window_hours": 168,
                    "oldest_seq": 35000,
                    "newest_seq": 42789,
                },
                "snapshot": {
                    "access_timeout_hours": 2,
                    "max_lifetime_hours": 24,
                    "supports_incremental": True,
                    "supports_chunking": True,
                },
                "changes": {"supports_long_polling": False, "payload_type": "FULL_OBJ"},
            }
        },
    )


class ChangeLogListResponse(BaseModel):
    """变更日志分页响应模型"""

    items: list[ChangeLogResponse]
    total: int
    page_num: int
    page_size: int
    pages: int


class SnapshotListResponse(BaseModel):
    """快照分页响应模型"""

    items: list[SnapshotInfo]
    total: int
    page_num: int
    page_size: int
    pages: int


class SnapshotCleanupResponse(BaseModel):
    """快照清理响应模型"""

    cleaned_count: int


class RetentionConfigResponse(BaseModel):
    """ChangeLog 清理使用的保留策略配置。"""

    window_hours: int
    max_records: int


class ChangeLogCleanupResponse(BaseModel):
    """变更日志清理响应模型"""

    cleaned_count: int
    retention_config: RetentionConfigResponse


# WebHook 相关的 Schema


class WebHookCreate(BaseModel):
    """创建WebHook的请求模型"""

    url: str = Field(..., max_length=2000, description=WEBHOOK_URL_DESCRIPTION)
    secret: str = Field(..., max_length=500, description="签名密钥")
    types: list[str] = Field(..., description=WEBHOOK_TYPES_DESCRIPTION)
    events: list[str] = Field(..., description=WEBHOOK_EVENTS_DESCRIPTION)
    description: str | None = Field(None, max_length=500, description=WEBHOOK_DESCRIPTION)

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "url": "https://discovery.example.com/webhook/data-change",
                "secret": "<configured-secret>",  # nosec B105 - OpenAPI example placeholder
                "types": ["acs", "dataset"],
                "events": ["data_change", "retention_cleanup"],
                "description": "Discovery service webhook",
            }
        },
    )


class WebHookUpdate(BaseModel):
    """更新WebHook的请求模型"""

    url: str | None = Field(None, max_length=2000, description=WEBHOOK_URL_DESCRIPTION)
    secret: str | None = Field(None, max_length=500, description="签名密钥")
    types: list[str] | None = Field(None, description=WEBHOOK_TYPES_DESCRIPTION)
    events: list[str] | None = Field(None, description=WEBHOOK_EVENTS_DESCRIPTION)
    description: str | None = Field(None, max_length=500, description=WEBHOOK_DESCRIPTION)

    model_config = ConfigDict(from_attributes=True)


class WebHookResponse(BaseModel):
    """WebHook响应模型"""

    id: str = Field(..., description="WebHook唯一标识")
    url: str = Field(..., description=WEBHOOK_URL_DESCRIPTION)
    types: list[str] = Field(..., description=WEBHOOK_TYPES_DESCRIPTION)
    events: list[str] = Field(..., description=WEBHOOK_EVENTS_DESCRIPTION)
    description: str | None = Field(None, description=WEBHOOK_DESCRIPTION)
    status: str = Field(..., description="WebHook状态")
    failure_count: int = Field(..., description="失败计数")
    last_triggered_at: datetime | None = Field(None, description="最后触发时间")
    last_success_at: datetime | None = Field(None, description="最后成功时间")
    last_failure_at: datetime | None = Field(None, description="最后失败时间")
    next_retry_at: datetime | None = Field(None, description="下次重试时间")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "wh_abc123def456",
                "url": "https://discovery.example.com/webhook/data-change",
                "types": ["acs", "dataset"],
                "events": ["data_change", "retention_cleanup"],
                "description": "Discovery service webhook",
                "status": "active",
                "failure_count": 0,
                "last_triggered_at": WEBHOOK_EXAMPLE_TIMESTAMP,
                "last_success_at": WEBHOOK_EXAMPLE_TIMESTAMP,
                "last_failure_at": None,
                "next_retry_at": None,
                "created_at": "2025-08-19T10:30:00Z",
            }
        },
    )


class WebHookListResponse(BaseModel):
    """WebHook 分页响应模型"""

    items: list[WebHookResponse]
    total: int
    page_num: int
    page_size: int
    pages: int


class WebHookNotification(BaseModel):
    """WebHook回调通知的载荷模型"""

    id: str = Field(..., description="WebHook ID")
    event: str = Field(..., description="事件类型")
    timestamp: datetime = Field(..., description="事件时间戳")
    data: dict[str, Any] = Field(..., description="事件数据")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "wh_abc123def456",
                "event": "data_change",
                "timestamp": WEBHOOK_EXAMPLE_TIMESTAMP,
                "data": {"type": "acs", "current_seq": 42789},
            }
        },
    )


class WebHookCallbackResponse(BaseModel):
    """WebHook回调成功响应模型"""

    status: str = Field(..., description="处理状态")
    processed_at: datetime = Field(..., description="处理时间")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "status": "acknowledged",
                "processed_at": "2025-08-19T12:15:35Z",
            }
        },
    )
