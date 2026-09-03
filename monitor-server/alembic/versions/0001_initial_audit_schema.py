"""initial audit schema

Revision ID: 0001
Revises:
Create Date: 2026-06-09

创建 Audit 数据库层的全部七张表、索引、初始分区及预置数据。
参照 AMP-API-Design-Audit.md §4 的精确 SQL 定义。
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import op

# 将项目根目录加入 sys.path，以便在 alembic 命令行中正确 import app 模块
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # ── 1. audit_record_identity ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_record_identity (
            audit_id    UUID        PRIMARY KEY,
            log_id      TEXT        NOT NULL UNIQUE,
            timestamp   TIMESTAMPTZ NOT NULL,
            chain_id    TEXT        NOT NULL,
            chain_seq   BIGINT      NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL,
            UNIQUE (chain_id, chain_seq)
        )
    """)
    op.execute("""
        CREATE INDEX idx_audit_identity_ts
            ON audit_record_identity (timestamp DESC)
    """)
    op.execute("""
        CREATE INDEX idx_audit_identity_chain_seq
            ON audit_record_identity (chain_id, chain_seq DESC)
    """)

    # ── 2. audit_chain_head ───────────────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_chain_head (
            chain_id            TEXT        PRIMARY KEY,
            last_audit_id       UUID,
            last_chain_seq      BIGINT      NOT NULL,
            last_current_hash   TEXT,
            updated_at          TIMESTAMPTZ NOT NULL
        )
    """)

    # ── 3. audit_records（分区主表，无全局 PRIMARY KEY）─────────────────
    op.execute("""
        CREATE TABLE audit_records (
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

            raw_log                 JSONB       NOT NULL
        ) PARTITION BY RANGE (timestamp)
    """)
    op.execute("CREATE INDEX idx_audit_id ON audit_records (audit_id)")
    op.execute("CREATE INDEX idx_audit_log_id ON audit_records (log_id)")
    op.execute("CREATE INDEX idx_audit_timestamp ON audit_records (timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_aic_ts ON audit_records (aic, timestamp DESC)")
    op.execute("CREATE INDEX idx_audit_actor ON audit_records (actor_id, timestamp DESC)")
    op.execute("""
        CREATE INDEX idx_audit_target
            ON audit_records (target_type, target_id, timestamp DESC)
    """)
    op.execute("""
        CREATE INDEX idx_audit_action_type
            ON audit_records (action_type, action_name)
    """)
    op.execute("""
        CREATE INDEX idx_audit_chain_seq
            ON audit_records (chain_id, chain_seq DESC)
    """)

    # 初始月分区：当月（2026-06）和下月（2026-07）
    op.execute("""
        CREATE TABLE audit_records_2026_06
            PARTITION OF audit_records
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
    """)
    op.execute("""
        CREATE TABLE audit_records_2026_07
            PARTITION OF audit_records
            FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')
    """)

    # ── 4. audit_chain_anchor ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_chain_anchor (
            anchor_id           UUID        PRIMARY KEY,
            chain_id            TEXT        NOT NULL,
            anchored_at         TIMESTAMPTZ NOT NULL,
            last_audit_id       UUID        NOT NULL,
            last_chain_seq      BIGINT      NOT NULL,
            last_current_hash   TEXT        NOT NULL,
            anchor_method       TEXT        NOT NULL,
            anchor_proof        JSONB       NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX idx_audit_anchor_chain_time
            ON audit_chain_anchor (chain_id, anchored_at DESC)
    """)

    # ── 5. audit_read_model_watermark ─────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_read_model_watermark (
            stream_name             TEXT        PRIMARY KEY,
            event_time_watermark    TIMESTAMPTZ NOT NULL,
            updated_at              TIMESTAMPTZ NOT NULL,
            source_offsets          JSONB
        )
    """)

    # ── 6. audit_integrity_tasks ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_integrity_tasks (
            task_id                 UUID        PRIMARY KEY,
            created_at              TIMESTAMPTZ NOT NULL,
            updated_at              TIMESTAMPTZ NOT NULL,
            status                  TEXT        NOT NULL,
            request_snapshot        JSONB       NOT NULL,
            verify_anchor           BOOLEAN     NOT NULL,
            stop_on_first_failure   BOOLEAN     NOT NULL DEFAULT false,
            checked_count           BIGINT,
            failed_count            BIGINT,
            anchored_until          TIMESTAMPTZ,
            failures_json           JSONB,
            error                   TEXT
        )
    """)

    # ── 7. audit_export_tasks ─────────────────────────────────────────────
    op.execute("""
        CREATE TABLE audit_export_tasks (
            task_id             UUID        PRIMARY KEY,
            created_at          TIMESTAMPTZ NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL,
            status              TEXT        NOT NULL,
            request_snapshot    JSONB       NOT NULL,
            export_format       TEXT        NOT NULL,
            include_raw         BOOLEAN     NOT NULL,
            signature_alg       TEXT,
            record_count        BIGINT,
            manifest_hash       TEXT,
            artifact_uri        TEXT,
            artifact_sha256     TEXT,
            artifact_size_bytes BIGINT,
            finished_at         TIMESTAMPTZ,
            error               TEXT
        )
    """)

    # ── 预置数据 ──────────────────────────────────────────────────────────

    # 预创建 256 条 audit_chain_head 行（chain_seq = -1 为创世前状态）
    from app.core.config import settings

    logical_chain_count = settings.audit_logical_chain_count
    width = len(str(logical_chain_count - 1))
    chain_rows = ", ".join(f"('audit-chain-{i:0{width}d}', NULL, -1, NULL, NOW())" for i in range(logical_chain_count))
    op.execute(
        f"INSERT INTO audit_chain_head (chain_id, last_audit_id, last_chain_seq, last_current_hash, updated_at) VALUES {chain_rows}"  # noqa: S608
    )

    # 预插入 watermark 初始行
    op.execute("""
        INSERT INTO audit_read_model_watermark
            (stream_name, event_time_watermark, updated_at, source_offsets)
        VALUES
            ('amp.audit', '1970-01-01T00:00:00Z', NOW(), NULL)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_export_tasks")
    op.execute("DROP TABLE IF EXISTS audit_integrity_tasks")
    op.execute("DROP TABLE IF EXISTS audit_read_model_watermark")
    op.execute("DROP TABLE IF EXISTS audit_chain_anchor")
    # 删除分区主表（CASCADE 同时删除所有子分区）
    op.execute("DROP TABLE IF EXISTS audit_records CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_chain_head")
    op.execute("DROP TABLE IF EXISTS audit_record_identity")
