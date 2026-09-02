"""Audit 数据库层 SQLModel 模型。

对应 AMP-API-Design-Audit.md §4 定义的八张表。

注意：
- `AuditRecord.audit_id` 在 Python ORM 层作为 mapper 主键，
  但实际 PostgreSQL 表无全局 PRIMARY KEY 约束（分区表限制）；
  全局唯一性由 `audit_record_identity` 保证。
- `audit_records` 按 `committed_at` 分区（PARTITION BY RANGE (committed_at)），
  不按事件时间 `timestamp` 分区，原因见 §4.3。
- Alembic 迁移使用原始 SQL，不依赖 SQLModel autogenerate。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, cast

from pydantic import ConfigDict
from sqlalchemy import SMALLINT, BigInteger, Boolean, Column, Index, Text
from sqlalchemy import TIMESTAMP as SA_TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel
from sqlmodel._compat import SQLModelConfig


class AuditRecordIdentity(SQLModel, table=True):
    """全局身份与幂等表：所有 Audit Writer 的去重 locator。

    `audit_id` 全局唯一、`log_id` 幂等唯一、`(chain_id, chain_seq)` 全局唯一。
    Writer 写入主事务前必须先写本表（INSERT ... ON CONFLICT 幂等判断）。

    `committed_at` 与 `audit_records.committed_at` 相同（同一 `clock_timestamp()` 值），
    供 `GET /records/{auditId}` 按提交时间分区直接定位 `audit_records` 目标分区，
    避免全分区扫描（§4.1、§6.2）。
    """

    __tablename__ = "audit_record_identity"
    __table_args__: ClassVar[tuple[Any, ...]] = (
        Index("idx_audit_identity_ts", "timestamp", postgresql_using="btree"),
        Index("idx_audit_identity_committed", "committed_at", postgresql_using="btree"),
        Index("idx_audit_identity_chain_seq", "chain_id", "chain_seq", postgresql_using="btree"),
        {"schema": None},
    )

    audit_id: uuid.UUID = Field(
        default_factory=uuid.uuid7,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )
    log_id: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    timestamp: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    committed_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    chain_id: str = Field(sa_column=Column(Text, nullable=False))
    chain_seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    created_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditChainHead(SQLModel, table=True):
    """子链头表：跟踪每条逻辑子链的最新状态。

    Writer 追加记录前必须 `SELECT ... FOR UPDATE` 锁住本行，防止并发 chain_seq 冲突。
    初始化时需为全部 logical_chain_count 条子链预创建行（last_chain_seq = -1）。
    """

    __tablename__ = "audit_chain_head"

    chain_id: str = Field(sa_column=Column(Text, primary_key=True))
    last_audit_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(PG_UUID(as_uuid=True), nullable=True),
    )
    last_chain_seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    last_current_hash: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditRecord(SQLModel, table=True):
    """主事实表：按 committed_at 月范围分区（PARTITION BY RANGE (committed_at)）。

    此表刻意不声明全局 PRIMARY KEY 或 UNIQUE 约束（PostgreSQL 分区表限制）。
    全局唯一性由 `audit_record_identity` 表保证。
    `audit_id` 在 Python ORM 层作为 mapper 主键，实际 DDL 由 Alembic 手动管理。

    `committed_at` 必须取服务端 `clock_timestamp()`（在持有链头行锁后于 INSERT 时求值），
    不得用 Python `datetime.now()` 或事务开始时间 `now()`，原因见设计文档 §4.3。
    """

    __tablename__ = "audit_records"
    __table_args__: ClassVar[tuple[Any, ...]] = (
        # 所有索引在 Alembic 迁移中通过原始 SQL 创建，此处仅供参考
        Index("idx_audit_id", "audit_id"),
        Index("idx_audit_log_id", "log_id"),
        Index("idx_audit_timestamp", "timestamp", postgresql_using="btree"),
        Index("idx_audit_aic_ts", "aic", "timestamp"),
        Index("idx_audit_tenant_ts", "tenant_id", "timestamp"),
        Index("idx_audit_actor", "actor_id", "timestamp"),
        Index("idx_audit_target", "target_type", "target_id", "timestamp"),
        Index("idx_audit_action_type", "action_type", "action_name"),
        Index("idx_audit_chain_seq", "chain_id", "chain_seq"),
        # PostgreSQL 分区表声明，告知 SQLAlchemy autogenerate 忽略 PK 自动生成
        {"info": {"is_partition": True}},
    )

    # ORM mapper 主键（不产生 DB PRIMARY KEY 约束，由 Alembic 手动 DDL 控制）
    audit_id: uuid.UUID = Field(sa_column=Column(PG_UUID(as_uuid=True), nullable=False, primary_key=True))
    log_id: str = Field(sa_column=Column(Text, nullable=False))
    timestamp: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    committed_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    aic: str = Field(sa_column=Column(Text, nullable=False))
    tenant_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    trace_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    correlation_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    chain_id: str = Field(sa_column=Column(Text, nullable=False))
    chain_seq: int = Field(sa_column=Column(BigInteger, nullable=False))

    actor_id: str = Field(sa_column=Column(Text, nullable=False))
    actor_type: str = Field(sa_column=Column(Text, nullable=False))
    actor_name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    actor_role: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    actor_ip: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    actor_user_agent: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    action_name: str = Field(sa_column=Column(Text, nullable=False))
    action_type: str = Field(sa_column=Column(Text, nullable=False))
    action_method: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    target_type: str = Field(sa_column=Column(Text, nullable=False))
    target_id: str = Field(sa_column=Column(Text, nullable=False))
    target_name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    target_before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    target_after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    result_status: str = Field(sa_column=Column(Text, nullable=False))
    result_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    result_error_code: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    signature_alg: str = Field(sa_column=Column(Text, nullable=False))
    signature_kid: str = Field(sa_column=Column(Text, nullable=False))
    signature_value: str = Field(sa_column=Column(Text, nullable=False))
    signature_verified: bool = Field(sa_column=Column(Boolean, nullable=False))
    signature_checked_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    verification_failure_type: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    hash_version: int = Field(sa_column=Column(SMALLINT, nullable=False))
    raw_log_hash: str = Field(sa_column=Column(Text, nullable=False))
    previous_hash: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    current_hash: str = Field(sa_column=Column(Text, nullable=False))
    chain_verified: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    chain_checked_at: datetime | None = Field(
        default=None, sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=True)
    )
    anchor_id: uuid.UUID | None = Field(default=None, sa_column=Column(PG_UUID(as_uuid=True), nullable=True))

    raw_log: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditChainAnchor(SQLModel, table=True):
    """链锚定证据表：记录各子链外部锚定的证明材料。"""

    __tablename__ = "audit_chain_anchor"
    __table_args__: ClassVar[tuple[Any, ...]] = (
        Index(
            "idx_audit_anchor_chain_time",
            "chain_id",
            "anchored_at",
            postgresql_using="btree",
        ),
        {"schema": None},
    )

    anchor_id: uuid.UUID = Field(
        default_factory=uuid.uuid7,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )
    chain_id: str = Field(sa_column=Column(Text, nullable=False))
    anchored_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    last_audit_id: uuid.UUID = Field(sa_column=Column(PG_UUID(as_uuid=True), nullable=False))
    last_chain_seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    last_current_hash: str = Field(sa_column=Column(Text, nullable=False))
    anchor_method: str = Field(sa_column=Column(Text, nullable=False))
    anchor_proof: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditReadModelWatermark(SQLModel, table=True):
    """读模型事件时间水位表：按 Kafka 分区跟踪已处理到的事件时间水位。

    每个分区对应一行；Writer 以 UPSERT 推进自己负责的分区行。
    全局 dataFreshnessAt = MIN(partition_watermark) over all partitions of a stream。
    取 MIN 而非 MAX：任一滞后分区都会拉回全局水位，不会被快分区掩盖（§2.4, C-AUDIT-QUERY-7）。
    """

    __tablename__ = "audit_read_model_watermark"
    __table_args__: ClassVar[tuple[Any, ...]] = ({"schema": None},)

    stream_name: str = Field(sa_column=Column(Text, nullable=False, primary_key=True))
    partition_key: int = Field(sa_column=Column(BigInteger, nullable=False, primary_key=True))
    partition_watermark: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    last_offset: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    updated_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditIntegrityTask(SQLModel, table=True):
    """异步完整性校验任务状态表。"""

    __tablename__ = "audit_integrity_tasks"

    task_id: uuid.UUID = Field(
        default_factory=uuid.uuid7,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )
    created_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    status: str = Field(sa_column=Column(Text, nullable=False))
    request_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    verify_anchor: bool = Field(sa_column=Column(Boolean, nullable=False))
    stop_on_first_failure: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="false")
    )
    checked_count: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    failed_count: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    anchored_until: datetime | None = Field(default=None, sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=True))
    failures_json: list[Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditChainCheckpoint(SQLModel, table=True):
    """保留边界检查点表（§4.9）。

    在归档 Job 执行 DROP TABLE 之前，为每条子链写入被删分区范围内最后一条记录的
    `(chain_seq, current_hash)` 作为连续性检查点，供在线链校验跨越保留边界时使用。

    本表行数极少（每月 × 逻辑子链数），永久保留，不随在线分区收缩删除。
    """

    __tablename__ = "audit_chain_checkpoint"

    chain_id: str = Field(sa_column=Column(Text, nullable=False, primary_key=True))
    boundary_seq: int = Field(sa_column=Column(BigInteger, nullable=False, primary_key=True))
    boundary_hash: str = Field(sa_column=Column(Text, nullable=False))
    archived_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))


class AuditExportTask(SQLModel, table=True):
    """异步导出任务状态表。

    `kind = 'public'`：对外 Export API 提交；`kind = 'internal'`：内部冷归档 Job 创建。
    公开查询端点仅暴露 `public` 任务，对 `internal` 任务一律返回 404。
    """

    __tablename__ = "audit_export_tasks"

    task_id: uuid.UUID = Field(
        default_factory=uuid.uuid7,
        sa_column=Column(PG_UUID(as_uuid=True), primary_key=True),
    )
    created_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=False))
    status: str = Field(sa_column=Column(Text, nullable=False))
    kind: str = Field(
        default="public",
        sa_column=Column(Text, nullable=False, server_default="public"),
    )
    request_snapshot: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    export_format: str = Field(sa_column=Column(Text, nullable=False))
    include_raw: bool = Field(sa_column=Column(Boolean, nullable=False))
    signature_alg: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    record_count: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    manifest_hash: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    artifact_uri: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    artifact_sha256: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    artifact_size_bytes: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(SA_TIMESTAMP(timezone=True), nullable=True))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    model_config: ClassVar[SQLModelConfig] = cast("SQLModelConfig", ConfigDict(from_attributes=True))
