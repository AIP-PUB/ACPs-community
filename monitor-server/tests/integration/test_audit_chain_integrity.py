"""tests/integration/test_audit_chain_integrity.py — 哈希链完整性集成测试。

在真实 PostgreSQL 上验证：
- 连续写入 5 条后链连续性（previous_hash 链接正确）
- genesis 记录 previous_hash = NULL
- compute_current_hash 重算与 DB 中存储值一致
- 不同 AIC 的记录路由到不同的 chain_id
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.chain import compute_chain_id, compute_current_hash, compute_raw_log_hash
from app.audit.model import AuditChainHead, AuditRecord
from app.core.amp_schema import LogRecord
from tests.integration.conftest import make_signed_log_record


async def _write_records(
    writer: Any,
    priv: Any,
    kid: str,
    aic: str,
    count: int,
    base_hour: int = 10,
) -> list[dict]:
    """向 writer 写入 count 条记录，返回原始 dict 列表。"""
    raws = []
    for i in range(count):
        raw = make_signed_log_record(
            priv,
            kid=kid,
            aic=aic,
            timestamp=f"2026-06-09T{base_hour + i // 60:02d}:{i % 60:02d}:00+00:00",
        )
        record = LogRecord.model_validate(raw)
        await writer._process_audit_record(record, raw)
        raws.append(raw)
    return raws


class TestChainContinuity:
    async def test_chain_seq_increases_from_zero(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """连续写入 5 条，chain_seq 必须严格为 0..4。"""
        writer, priv, kid = audit_writer_with_mock_keys
        aic = f"aic-chain-{uuid.uuid4()}"
        await _write_records(writer, priv, kid, aic, 5)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        rows = (
            (
                await db_session.execute(
                    select(AuditRecord)
                    .where(AuditRecord.chain_id == chain_id)  # type: ignore[arg-type]
                    .order_by(AuditRecord.chain_seq)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 5
        assert [r.chain_seq for r in rows] == list(range(5))

    async def test_genesis_record_has_null_previous_hash(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """第一条记录（genesis）的 previous_hash 应为 NULL。"""
        writer, priv, kid = audit_writer_with_mock_keys
        aic = f"aic-genesis-{uuid.uuid4()}"
        await _write_records(writer, priv, kid, aic, 1)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        row = (
            (
                await db_session.execute(
                    select(AuditRecord)
                    .where(AuditRecord.chain_id == chain_id)  # type: ignore[arg-type]
                    .order_by(AuditRecord.chain_seq)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.previous_hash is None, f"genesis 记录 previous_hash={row.previous_hash!r}，期望 NULL"

    async def test_previous_hash_links_to_prior_current_hash(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """连续写入 5 条，第 N 条的 previous_hash 必须等于第 N-1 条的 current_hash。"""
        writer, priv, kid = audit_writer_with_mock_keys
        aic = f"aic-link-{uuid.uuid4()}"
        await _write_records(writer, priv, kid, aic, 5)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        rows = (
            (
                await db_session.execute(
                    select(AuditRecord)
                    .where(AuditRecord.chain_id == chain_id)  # type: ignore[arg-type]
                    .order_by(AuditRecord.chain_seq)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 5
        for i in range(1, 5):
            assert rows[i].previous_hash == rows[i - 1].current_hash, (
                f"chain_seq={i} 的 previous_hash 不匹配前一条 current_hash"
            )


class TestCurrentHashVerification:
    async def test_stored_current_hash_matches_recomputed(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """对 DB 中存储的记录，重算 current_hash 应与存储值完全一致。"""
        writer, priv, kid = audit_writer_with_mock_keys
        aic = f"aic-hash-{uuid.uuid4()}"
        raws = await _write_records(writer, priv, kid, aic, 3)

        chain_id = compute_chain_id(aic, writer._logical_chain_count)
        rows = (
            (
                await db_session.execute(
                    select(AuditRecord)
                    .where(AuditRecord.chain_id == chain_id)  # type: ignore[arg-type]
                    .order_by(AuditRecord.chain_seq)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3

        for row in rows:
            # 根据 raws 找到对应的原始 dict（按 log_id 匹配）
            raw = next(r for r in raws if r["log_id"] == row.log_id)
            recomputed_raw_hash = compute_raw_log_hash(raw)
            recomputed_hash = compute_current_hash(
                audit_id=str(row.audit_id),
                log_id=row.log_id,
                timestamp_str=raw["timestamp"],
                aic=row.aic,
                chain_id=row.chain_id,
                chain_seq=row.chain_seq,
                raw_log_hash=recomputed_raw_hash,
                previous_hash=row.previous_hash,
            )
            assert recomputed_hash == row.current_hash, f"chain_seq={row.chain_seq} 的 current_hash 不匹配重算值"


class TestChainRouting:
    async def test_different_aics_route_to_different_chains(
        self,
        audit_writer_with_mock_keys: tuple,
        db_session: AsyncSession,
    ) -> None:
        """具有不同 AIC 且 hash 取模后路由不同的记录，应落入不同 chain_id。"""
        writer, priv, kid = audit_writer_with_mock_keys
        # 构造两个确保路由到不同链的 AIC（通过暴力枚举找两个不同的 chain_id）
        found: dict[str, str] = {}  # chain_id -> aic
        count = writer._logical_chain_count
        i = 0
        while len(found) < 2:
            aic = f"aic-routing-{i}"
            cid = compute_chain_id(aic, count)
            if cid not in found:
                found[cid] = aic
            i += 1

        aic_a, aic_b = list(found.values())[:2]
        chain_a, chain_b = list(found.keys())[:2]

        raw_a = make_signed_log_record(priv, kid=kid, aic=aic_a, timestamp="2026-06-09T10:00:00+00:00")
        raw_b = make_signed_log_record(priv, kid=kid, aic=aic_b, timestamp="2026-06-09T10:00:00+00:00")

        await writer._process_audit_record(LogRecord.model_validate(raw_a), raw_a)
        await writer._process_audit_record(LogRecord.model_validate(raw_b), raw_b)

        for chain_id in [chain_a, chain_b]:
            head = (
                (
                    await db_session.execute(
                        select(AuditChainHead).where(AuditChainHead.chain_id == chain_id)  # type: ignore[arg-type]
                    )
                )
                .scalars()
                .first()
            )
            assert head is not None
            assert head.last_chain_seq == 0, f"chain_id={chain_id!r} 未被写入（last_chain_seq={head.last_chain_seq}）"
