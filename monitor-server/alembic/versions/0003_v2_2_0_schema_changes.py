"""v2.2.0 schema: identity.committed_at、audit_chain_checkpoint、export.kind、修正分区键

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11

变更内容（对应 v2.2.0 代码改动，修复上线后 writer DLQ 根因）：

1. audit_record_identity
   - 新增 committed_at TIMESTAMPTZ NOT NULL
     Writer 幂等检查的 SELECT 包含此列（ORM 自动展开），列缺失导致 UndefinedColumnError。
   - 回填：从关联 audit_records 行取值；孤立行以 created_at 兜底。
   - 新增 idx_audit_identity_committed 索引。

2. audit_records — 修正分区键 timestamp → committed_at
   0001 误将分区键写为 timestamp，service.py _committed_at_fence() 依赖 committed_at
   进行分区裁剪（§5.3 §6.1），分区键错误导致裁剪失效（全分区扫描）。
   开发环境数据量极少，采用 "建新表 → 迁移数据 → 删旧表 → 改名" 方式重建。
   ⚠️  生产环境此操作需在维护窗口执行，并配合 pg_partman 在线重建。

3. 新增 audit_chain_checkpoint 表（§4.9 保留边界检查点）
   model.py 已定义但 0001 未创建，SQLAlchemy metadata 扫描时会警告，集成测试也依赖它存在。

4. audit_export_tasks
   - 新增 kind TEXT NOT NULL DEFAULT 'public'
     service.py submit_export 写 kind='public'、get_export_task 对 kind='internal' 返回 404。
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# audit_records 全列列表（含 0002 追加的 tenant_id），用于数据迁移的显式 SELECT/INSERT
_AUDIT_RECORDS_COLUMNS = """
    audit_id, log_id, timestamp, committed_at, aic,
    trace_id, correlation_id,
    chain_id, chain_seq,
    actor_id, actor_type, actor_name, actor_role, actor_ip, actor_user_agent,
    action_name, action_type, action_method,
    target_type, target_id, target_name, target_before, target_after,
    result_status, result_reason, result_error_code,
    signature_alg, signature_kid, signature_value, signature_verified,
    signature_checked_at, verification_failure_type,
    hash_version, raw_log_hash, previous_hash, current_hash,
    chain_verified, chain_checked_at, anchor_id,
    raw_log, tenant_id
"""

_AUDIT_RECORDS_DDL = """
    audit_id                UUID        NOT NULL,
    log_id                  TEXT        NOT NULL,
    timestamp               TIMESTAMPTZ NOT NULL,
    committed_at            TIMESTAMPTZ NOT NULL,
    aic                     TEXT        NOT NULL,
    trace_id                TEXT,
    correlation_id          TEXT,
    chain_id                TEXT        NOT NULL,
    chain_seq               BIGINT      NOT NULL,
    actor_id                TEXT        NOT NULL,
    actor_type              TEXT        NOT NULL,
    actor_name              TEXT,
    actor_role              TEXT,
    actor_ip                TEXT,
    actor_user_agent        TEXT,
    action_name             TEXT        NOT NULL,
    action_type             TEXT        NOT NULL,
    action_method           TEXT,
    target_type             TEXT        NOT NULL,
    target_id               TEXT        NOT NULL,
    target_name             TEXT,
    target_before           JSONB,
    target_after            JSONB,
    result_status           TEXT        NOT NULL,
    result_reason           TEXT,
    result_error_code       TEXT,
    signature_alg           TEXT        NOT NULL,
    signature_kid           TEXT        NOT NULL,
    signature_value         TEXT        NOT NULL,
    signature_verified      BOOLEAN     NOT NULL,
    signature_checked_at    TIMESTAMPTZ NOT NULL,
    verification_failure_type TEXT,
    hash_version            SMALLINT    NOT NULL,
    raw_log_hash            TEXT        NOT NULL,
    previous_hash           TEXT,
    current_hash            TEXT        NOT NULL,
    chain_verified          BOOLEAN,
    chain_checked_at        TIMESTAMPTZ,
    anchor_id               UUID,
    raw_log                 JSONB       NOT NULL,
    tenant_id               TEXT
"""


def _recreate_audit_records_indexes() -> None:
    """重建 audit_records 全部索引（0001 + 0002 原始定义）。

    DROP TABLE CASCADE 会删除所有子分区及其索引，重命名后需重建。
    """
    op.execute("CREATE INDEX idx_audit_id           ON audit_records (audit_id)")
    op.execute("CREATE INDEX idx_audit_log_id       ON audit_records (log_id)")
    op.execute("CREATE INDEX idx_audit_timestamp    ON audit_records (timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_aic_ts       ON audit_records (aic, timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_actor        ON audit_records (actor_id, timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_chain_seq    ON audit_records (chain_id, chain_seq DESC)")
    op.execute("CREATE INDEX idx_audit_action_type  ON audit_records (action_type, action_name)")
    op.execute("CREATE INDEX idx_audit_target ON audit_records (target_type, target_id, timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_tenant_ts    ON audit_records (tenant_id, timestamp DESC)")


def upgrade() -> None:
    # ── 1. audit_record_identity: 新增 committed_at ───────────────────────────
    # 先加 nullable 列，回填，再收紧为 NOT NULL（避免现有行因缺值而报错）。
    op.execute("ALTER TABLE audit_record_identity ADD COLUMN committed_at TIMESTAMPTZ")

    # 从关联 audit_records 行取 committed_at 值回填
    op.execute("""
        UPDATE audit_record_identity i
           SET committed_at = r.committed_at
          FROM audit_records r
         WHERE r.audit_id = i.audit_id
    """)

    # 孤立行（无对应 audit_records，理论上不存在）以 created_at 兜底，确保无 NULL
    op.execute("""
        UPDATE audit_record_identity
           SET committed_at = created_at
         WHERE committed_at IS NULL
    """)

    op.execute("ALTER TABLE audit_record_identity ALTER COLUMN committed_at SET NOT NULL")
    op.execute("CREATE INDEX idx_audit_identity_committed ON audit_record_identity (committed_at DESC)")

    # ── 2. audit_records: 分区键 timestamp → committed_at ────────────────────
    # 建新表（按 committed_at 分区）
    op.execute(f"CREATE TABLE audit_records_new ({_AUDIT_RECORDS_DDL}) PARTITION BY RANGE (committed_at)")

    # 月分区：范围与 0001 保持一致，committed_at 值均落在 2026-06 或 2026-07 内
    op.execute("""
        CREATE TABLE audit_records_new_2026_06
            PARTITION OF audit_records_new
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
    """)
    op.execute("""
        CREATE TABLE audit_records_new_2026_07
            PARTITION OF audit_records_new
            FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
    """)

    # 迁移全量数据（显式列名，避免列序差异导致偏移）
    op.execute(
        f"INSERT INTO audit_records_new ({_AUDIT_RECORDS_COLUMNS}) SELECT {_AUDIT_RECORDS_COLUMNS} FROM audit_records"  # noqa: S608
    )

    # 删旧表（CASCADE 同时删除旧分区 audit_records_2026_06/07 及其全部索引）
    op.execute("DROP TABLE audit_records CASCADE")

    # 重命名新表及子分区
    op.execute("ALTER TABLE audit_records_new        RENAME TO audit_records")
    op.execute("ALTER TABLE audit_records_new_2026_06 RENAME TO audit_records_2026_06")
    op.execute("ALTER TABLE audit_records_new_2026_07 RENAME TO audit_records_2026_07")

    # 重建全部索引（原名称保持不变）
    _recreate_audit_records_indexes()

    # ── 3. 新增 audit_chain_checkpoint 表（§4.9）──────────────────────────────
    op.execute("""
        CREATE TABLE audit_chain_checkpoint (
            chain_id        TEXT        NOT NULL,
            boundary_seq    BIGINT      NOT NULL,
            boundary_hash   TEXT        NOT NULL,
            archived_at     TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (chain_id, boundary_seq)
        )
    """)

    # ── 4. audit_export_tasks: 新增 kind 列 ───────────────────────────────────
    op.execute("ALTER TABLE audit_export_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'public'")


def downgrade() -> None:
    # ── 4 回退. 删除 kind（仅开发环境：数据允许丢失）────────────────────────
    op.execute("ALTER TABLE audit_export_tasks DROP COLUMN kind")

    # ── 3 回退. 删除 audit_chain_checkpoint ───────────────────────────────────
    op.execute("DROP TABLE IF EXISTS audit_chain_checkpoint")

    # ── 2 回退. audit_records 分区键改回 timestamp ───────────────────────────
    op.execute(f"CREATE TABLE audit_records_old ({_AUDIT_RECORDS_DDL}) PARTITION BY RANGE (timestamp)")
    op.execute("""
        CREATE TABLE audit_records_old_2026_06
            PARTITION OF audit_records_old
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
    """)
    op.execute("""
        CREATE TABLE audit_records_old_2026_07
            PARTITION OF audit_records_old
            FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
    """)
    op.execute(
        f"INSERT INTO audit_records_old ({_AUDIT_RECORDS_COLUMNS}) SELECT {_AUDIT_RECORDS_COLUMNS} FROM audit_records"  # noqa: S608
    )
    op.execute("DROP TABLE audit_records CASCADE")
    op.execute("ALTER TABLE audit_records_old        RENAME TO audit_records")
    op.execute("ALTER TABLE audit_records_old_2026_06 RENAME TO audit_records_2026_06")
    op.execute("ALTER TABLE audit_records_old_2026_07 RENAME TO audit_records_2026_07")
    _recreate_audit_records_indexes()

    # ── 1 回退. 删除 audit_record_identity.committed_at ─────────────────────
    op.execute("DROP INDEX IF EXISTS idx_audit_identity_committed")
    op.execute("ALTER TABLE audit_record_identity DROP COLUMN committed_at")
