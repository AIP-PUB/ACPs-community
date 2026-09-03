"""AMP alive-sync PostgreSQL 数据模型（SQLModel 表定义）。

两张表：
  - agent_alive_status       ：每个 AIC 的 alive 标记 + version（全量主体表）
  - alive_sync_shard_state   ：per-shard checkpoint（last_seen_seq / cutover_seq / kafka_next_offset）
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, String
from sqlmodel import Column, Field, SQLModel


class AgentAliveStatus(SQLModel, table=True):
    """AIC alive 状态表（全量主体表 + alive 标记，§7.5 第 4 条）。

    每行对应一个 AIC；alive=True 表示该 AIC 当前存活，alive=False 表示已 leave_alive。
    version 等于最近一次事件的 seq（数值），供引擎做幂等去重（§7.5 第 2 条）。
    """

    __tablename__ = "agent_alive_status"
    __table_args__ = (Index("idx_agent_alive_status_shard", "shard"),)

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    aic: str = Field(
        sa_column=Column(String(128), nullable=False, unique=True, index=True),
        description="Agent Identity Code（AIC）",
    )
    alive: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, default=False),
        description="当前是否存活（来自 AMP heartbeat alive-delta 同步）",
    )
    last_seen_at: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="最近一次心跳时间戳（ISO 8601 UTC），来自 heartbeat 事件",
    )
    version: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
        description="最近一次事件 seq（数值），供幂等去重",
    )
    shard: str = Field(
        sa_column=Column(String(32), nullable=False),
        description="该 AIC 所属 shard id（如 hb-000）",
    )


class AliveSyncShardState(SQLModel, table=True):
    """Per-shard 同步进度 checkpoint 表（§7.5 第 7 条）。

    每 shard 一行，记录：
      last_seen_seq     ：上一次已应用 seq（seq 闸门基准）
      cutover_seq       ：snapshot cutover 基线（C-SYNC-3）
      kafka_next_offset ：下一条待读 Kafka offset（随状态同事务持久化）
      snapshot_generated_at ：自举时间锚点（§7.5 第 8 条）
    """

    __tablename__ = "alive_sync_shard_state"

    id: int | None = Field(default=None, primary_key=True, description="自增主键")
    shard: str = Field(
        sa_column=Column(String(32), nullable=False, unique=True, index=True),
        description="shard id（如 hb-000）",
    )
    last_seen_seq: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
        description="上一次已应用 seq（seq 闸门基准，初始为 0）",
    )
    cutover_seq: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
        description="snapshot cutover 基线（C-SYNC-3）",
    )
    kafka_next_offset: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="下一条待读 Kafka offset（随状态同事务持久化）",
    )
    snapshot_generated_at: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="自举 snapshot 生成时间戳（ISO 8601 UTC）",
    )
