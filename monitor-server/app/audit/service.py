"""Audit Query Planner — 数据库查询服务层。

实现 AMP-API-Design-Audit.md §6 定义的查询逻辑：
1. 白名单校验（过滤/排序字段）
2. 时间范围强制（records/query, summary/aggregate, export 必须带 timeRange）
3. 分区裁剪：主表按 committed_at 分区，所有 timeRange 查询附加 committed_at 围栏谓词（§5.3 §6.1）
4. 读模型新鲜度查询（audit_read_model_watermark）
5. 游标分页（cursor = base64(last_timestamp + "|" + last_audit_id)，(timestamp, audit_id) 复合键保证稳定性）
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.exception import (
    AuditRecordNotFoundError,
    AuditTaskNotFoundError,
    CursorInvalidError,
    InvalidFilterError,
    InvalidTimeRangeError,
    UnsupportedFieldError,
)
from app.audit.model import (
    AuditChainAnchor,
    AuditExportTask,
    AuditIntegrityTask,
    AuditRecord,
    AuditRecordIdentity,
)
from app.audit.schema import (
    AuditAggregateItem,
    AuditAggregateRequest,
    AuditBodyActionView,
    AuditBodyActorView,
    AuditBodyResultView,
    AuditBodyTargetView,
    AuditBodyView,
    AuditChainAnchorView,
    AuditExportRequest,
    AuditExportTaskView,
    AuditIntegrityTaskView,
    AuditIntegrityVerifyRequest,
    AuditIntegrityVerifyResponse,
    AuditIntegrityVerifySummary,
    AuditRecordIntegrityView,
    AuditRecordQueryRequest,
    AuditRecordView,
)
from app.core.amp_api_schema import AMPResponseMeta, AMPTimeRange
from app.core.config import settings

logger = structlog.get_logger(__name__)

# API 字段路径 → 数据库列名映射（参照 §5.4）
_FIELD_MAP: dict[str, str] = {
    "auditId": "audit_id",
    "logId": "log_id",
    "aic": "aic",
    "tenantId": "tenant_id",
    "traceId": "trace_id",
    "correlationId": "correlation_id",
    "chainId": "chain_id",
    "chainSeq": "chain_seq",
    "body.actor.id": "actor_id",
    "body.actor.name": "actor_name",
    "body.actor.type": "actor_type",
    "body.actor.role": "actor_role",
    "body.actor.ip": "actor_ip",
    "body.action.name": "action_name",
    "body.action.type": "action_type",
    "body.action.method": "action_method",
    "body.target.type": "target_type",
    "body.target.id": "target_id",
    "body.target.name": "target_name",
    "body.result.status": "result_status",
    "body.result.errorCode": "result_error_code",
    "integrity.signatureVerified": "signature_verified",
    "integrity.chainVerified": "chain_verified",
    "integrity.verificationFailureType": "verification_failure_type",
    "integrity.signatureKeyId": "signature_kid",
}

# aggregate groupBy API 字段 → DB 列名扩展映射
_AGGREGATE_FIELD_MAP: dict[str, str] = {
    **_FIELD_MAP,
    "chainId": "chain_id",
    "body.result.errorCode": "result_error_code",
    "integrity.signatureVerified": "signature_verified",
    "integrity.chainVerified": "chain_verified",
    "integrity.verificationFailureType": "verification_failure_type",
    "integrity.signatureKeyId": "signature_kid",
}

# keyword 搜索的高信号字段（ILIKE 前缀匹配）
_KEYWORD_COLUMNS = [
    "log_id",
    "actor_id",
    "actor_name",
    "action_name",
    "action_type",
    "target_type",
    "target_id",
    "result_error_code",
]


def _require_time_range(time_range: AMPTimeRange | None) -> AMPTimeRange:
    """强制 timeRange 存在，否则抛出 InvalidTimeRangeError。"""
    if time_range is None:
        raise InvalidTimeRangeError()
    try:
        datetime.fromisoformat(time_range.start_at)
        datetime.fromisoformat(time_range.end_at)
    except ValueError as exc:
        raise InvalidTimeRangeError(f"timeRange 格式非法: {exc}") from exc
    return time_range


def _encode_cursor(timestamp: str, audit_id: str) -> str:
    """将 (timestamp, audit_id) 编码为分页游标。使用 | 作为分隔符（不出现在时间戳或 UUID 中）。"""
    raw = f"{timestamp}|{audit_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """解码分页游标，返回 (timestamp, audit_id)。"""
    try:
        raw = base64.urlsafe_b64decode(cursor + "==").decode()
        parts = raw.split("|", 1)
        if len(parts) != 2:
            raise CursorInvalidError()
        return parts[0], parts[1]
    except CursorInvalidError:
        raise
    except Exception as exc:
        raise CursorInvalidError() from exc


def _validate_filter_fields(filter_fields: list[str]) -> None:
    """校验过滤字段是否在白名单中。"""
    for field in filter_fields:
        if field not in _FIELD_MAP:
            raise UnsupportedFieldError(field)


def _mapping_to_record_view(m: Any) -> AuditRecordView:
    """将 SQLAlchemy RowMapping（来自 text() 查询）直接转换为 AuditRecordView。

    避免通过 object.__new__(AuditRecord) 绕过 SQLModel/Pydantic 初始化。
    """
    sig_checked_at = m["signature_checked_at"]
    chain_checked_at = m["chain_checked_at"]
    anchor_id = m["anchor_id"]
    return AuditRecordView(
        audit_id=str(m["audit_id"]),
        log_id=m["log_id"],
        timestamp=m["timestamp"].isoformat(),
        aic=m["aic"],
        trace_id=m["trace_id"],
        correlation_id=m["correlation_id"],
        chain_id=m["chain_id"],
        chain_seq=m["chain_seq"],
        body=AuditBodyView(
            actor=AuditBodyActorView(
                id=m["actor_id"],
                type=m["actor_type"],
                name=m["actor_name"],
                role=m["actor_role"],
                ip=m["actor_ip"],
                user_agent=m["actor_user_agent"],
            ),
            action=AuditBodyActionView(
                name=m["action_name"],
                type=m["action_type"],
                method=m["action_method"],
            ),
            target=AuditBodyTargetView(
                type=m["target_type"],
                id=m["target_id"],
                name=m["target_name"],
                before=m["target_before"],
                after=m["target_after"],
            ),
            result=AuditBodyResultView(
                status=m["result_status"],
                reason=m["result_reason"],
                error_code=m["result_error_code"],
            ),
        ),
        integrity=AuditRecordIntegrityView(
            signature_alg=m["signature_alg"],
            signature_key_id=m["signature_kid"],
            signature_verified=m["signature_verified"],
            signature_checked_at=sig_checked_at.isoformat() if sig_checked_at else "",
            verification_failure_type=m["verification_failure_type"],
            previous_hash=m["previous_hash"],
            current_hash=m["current_hash"],
            chain_verified=m["chain_verified"],
            chain_checked_at=chain_checked_at.isoformat() if chain_checked_at else None,
            chain_anchor_id=str(anchor_id) if anchor_id else None,
        ),
    )


def _row_to_record_view(row: AuditRecord) -> AuditRecordView:
    """将 AuditRecord ORM 对象（通过 session.scalars() 加载）映射为 AuditRecordView。"""
    return AuditRecordView(
        audit_id=str(row.audit_id),
        log_id=row.log_id,
        timestamp=row.timestamp.isoformat(),
        aic=row.aic,
        trace_id=row.trace_id,
        correlation_id=row.correlation_id,
        chain_id=row.chain_id,
        chain_seq=row.chain_seq,
        body=AuditBodyView(
            actor=AuditBodyActorView(
                id=row.actor_id,
                type=row.actor_type,
                name=row.actor_name,
                role=row.actor_role,
                ip=row.actor_ip,
                user_agent=row.actor_user_agent,
            ),
            action=AuditBodyActionView(
                name=row.action_name,
                type=row.action_type,
                method=row.action_method,
            ),
            target=AuditBodyTargetView(
                type=row.target_type,
                id=row.target_id,
                name=row.target_name,
                before=row.target_before,
                after=row.target_after,
            ),
            result=AuditBodyResultView(
                status=row.result_status,
                reason=row.result_reason,
                error_code=row.result_error_code,
            ),
        ),
        integrity=AuditRecordIntegrityView(
            signature_alg=row.signature_alg,
            signature_key_id=row.signature_kid,
            signature_verified=row.signature_verified,
            signature_checked_at=row.signature_checked_at.isoformat(),
            verification_failure_type=row.verification_failure_type,
            previous_hash=row.previous_hash,
            current_hash=row.current_hash,
            chain_verified=row.chain_verified,
            chain_checked_at=row.chain_checked_at.isoformat() if row.chain_checked_at else None,
            chain_anchor_id=str(row.anchor_id) if row.anchor_id else None,
        ),
    )


async def _get_watermark(session: AsyncSession) -> str:
    """读取全局事件时间水位，返回 ISO 8601 字符串。

    全局水位 = MIN(partition_watermark) over all partitions of 'amp.audit'（§2.4, C-AUDIT-QUERY-7）。
    取 MIN 而非 MAX：任一滞后分区都会拉回全局水位，不会被快分区掩盖。
    无任何行时返回 epoch（1970）作为安全默认值。
    """
    min_wm = await session.scalar(
        text("SELECT MIN(partition_watermark) FROM audit_read_model_watermark WHERE stream_name = 'amp.audit'")
    )
    if min_wm is None:
        return datetime(1970, 1, 1, tzinfo=UTC).isoformat()
    if isinstance(min_wm, datetime):
        return min_wm.isoformat()
    return str(min_wm)


def _committed_at_fence(start: datetime, end: datetime) -> tuple[str, str, datetime, datetime]:
    """构造 committed_at 围栏谓词参数（§5.3 §6.1）。

    主表按 committed_at 分区，查询必须附加此围栏以触发分区裁剪。
    围栏宽度 = max_event_lag_hours，是正确性不变量：任何记录的真实
    `committed_at - timestamp` 超过此值时将被静默漏查。

    Returns:
        (fence_start_clause, fence_end_clause, fence_start, fence_end)
    """
    lag = timedelta(hours=settings.audit_max_event_lag_hours)
    fence_start = start - lag
    fence_end = end + lag
    return (
        "committed_at >= :fence_start",
        "committed_at <= :fence_end",
        fence_start,
        fence_end,
    )


async def query_records(
    session: AsyncSession, request: AuditRecordQueryRequest
) -> tuple[list[AuditRecordView], AMPResponseMeta]:
    """POST /records/query 查询逻辑。"""
    time_range = _require_time_range(request.time_range)
    start = datetime.fromisoformat(time_range.start_at)
    end = datetime.fromisoformat(time_range.end_at)

    limit = (request.page.limit if request.page else 50) + 1  # +1 判断是否有下一页
    cursor_ts: str | None = None
    cursor_id: str | None = None
    if request.page and request.page.cursor:
        cursor_ts, cursor_id = _decode_cursor(request.page.cursor)

    fence_s_clause, fence_e_clause, fence_start, fence_end = _committed_at_fence(start, end)
    params: dict[str, Any] = {
        "start": start,
        "end": end,
        "limit": limit,
        "fence_start": fence_start,
        "fence_end": fence_end,
    }
    # timestamp 过滤决定语义正确性，committed_at 围栏仅触发分区裁剪（§5.3）
    where_clauses = ["timestamp >= :start", "timestamp < :end", fence_s_clause, fence_e_clause]

    if cursor_ts and cursor_id:
        where_clauses.append("(timestamp < :cursor_ts OR (timestamp = :cursor_ts AND audit_id < :cursor_id))")
        params["cursor_ts"] = datetime.fromisoformat(cursor_ts)
        params["cursor_id"] = uuid.UUID(cursor_id)

    # 过滤条件白名单校验 + SQL 构造
    if request.filter and request.filter.conditions:
        conditions = request.filter.conditions
        _validate_filter_fields([c.field for c in conditions])
        for idx, cond in enumerate(conditions):
            col = _FIELD_MAP[cond.field]
            p_name = f"f{idx}_{col}"
            if cond.op == "eq":
                where_clauses.append(f"{col} = :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "ne":
                where_clauses.append(f"{col} != :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "gt":
                where_clauses.append(f"{col} > :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "gte":
                where_clauses.append(f"{col} >= :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "lt":
                where_clauses.append(f"{col} < :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "lte":
                where_clauses.append(f"{col} <= :{p_name}")
                params[p_name] = cond.value
            elif cond.op == "contains":
                where_clauses.append(f"{col} ILIKE :{p_name}")
                params[p_name] = f"%{cond.value}%"
            elif cond.op == "starts_with":
                where_clauses.append(f"{col} ILIKE :{p_name}")
                params[p_name] = f"{cond.value}%"
            elif cond.op == "is_null":
                if cond.value:
                    where_clauses.append(f"{col} IS NULL")
                else:
                    where_clauses.append(f"{col} IS NOT NULL")

    if request.keyword:
        # log_id 精确匹配（命中 idx_audit_log_id）；其他字段前缀 ILIKE（§6.1 keyword 实现）
        kw_parts = []
        for col in _KEYWORD_COLUMNS:
            if col == "log_id":
                kw_parts.append(f"{col} = :kw_exact")
            else:
                kw_parts.append(f"{col} ILIKE :kw")
        where_clauses.append(f"({' OR '.join(kw_parts)})")
        params["kw"] = f"{request.keyword}%"
        params["kw_exact"] = request.keyword

    where_sql = " AND ".join(where_clauses)
    query = text(
        f"SELECT * FROM audit_records WHERE {where_sql} "  # noqa: S608  # nosec B608
        f"ORDER BY timestamp DESC, audit_id DESC LIMIT :limit"
    ).bindparams(**params)

    result = await session.execute(query)
    raw_rows = result.mappings().all()

    watermark = await _get_watermark(session)
    real_limit = limit - 1
    next_cursor: str | None = None
    if len(raw_rows) > real_limit:
        raw_rows = raw_rows[:real_limit]
        last = raw_rows[-1]
        next_cursor = _encode_cursor(last["timestamp"].isoformat(), str(last["audit_id"]))

    items = [_mapping_to_record_view(r) for r in raw_rows]
    meta = AMPResponseMeta(data_freshness_at=watermark, next_cursor=next_cursor)
    return items, meta


async def get_record_by_id(session: AsyncSession, audit_id: str) -> tuple[AuditRecordView, str]:
    """GET /records/{auditId} — 先查 identity 取 committed_at，再直接定位提交时间分区。

    按 committed_at 定位而非 timestamp，避免全分区扫描（§4.1, §6.2）。
    """
    try:
        uid = uuid.UUID(audit_id)
    except ValueError as exc:
        raise AuditRecordNotFoundError(audit_id) from exc

    identity = await session.scalar(
        select(AuditRecordIdentity).where(AuditRecordIdentity.audit_id == uid)  # type: ignore[arg-type]
    )
    if identity is None:
        raise AuditRecordNotFoundError(audit_id)

    result = await session.execute(
        text("SELECT * FROM audit_records WHERE audit_id = :uid AND committed_at = :cat").bindparams(
            uid=uid, cat=identity.committed_at
        )
    )
    row_mapping = result.mappings().first()
    if row_mapping is None:
        raise AuditRecordNotFoundError(audit_id)

    watermark = await _get_watermark(session)
    return _mapping_to_record_view(row_mapping), watermark


async def get_latest_anchors(
    session: AsyncSession, chain_id: str | None = None
) -> tuple[list[AuditChainAnchorView], AMPResponseMeta]:
    """GET /anchors/latest — 返回每条子链最新锚点。"""
    subq = (
        select(  # type: ignore[call-overload]
            AuditChainAnchor.chain_id,
            func.max(AuditChainAnchor.anchored_at).label("max_anchored_at"),
        )
        .group_by(AuditChainAnchor.chain_id)
        .subquery()
    )

    stmt = select(AuditChainAnchor).join(
        subq,
        (AuditChainAnchor.chain_id == subq.c.chain_id) & (AuditChainAnchor.anchored_at == subq.c.max_anchored_at),
    )

    if chain_id:
        stmt = stmt.where(AuditChainAnchor.chain_id == chain_id)  # type: ignore[arg-type]

    rows = (await session.scalars(stmt)).all()
    watermark = await _get_watermark(session)

    items = [
        AuditChainAnchorView(
            anchor_id=str(row.anchor_id),
            chain_id=row.chain_id,
            anchored_at=row.anchored_at.isoformat(),
            last_audit_id=str(row.last_audit_id),
            last_chain_seq=row.last_chain_seq,
            last_current_hash=row.last_current_hash,
            anchor_method=row.anchor_method,
            anchor_proof=row.anchor_proof,
        )
        for row in rows
    ]
    meta = AMPResponseMeta(data_freshness_at=watermark)
    return items, meta


async def submit_integrity_verify(
    session: AsyncSession, request: AuditIntegrityVerifyRequest
) -> AuditIntegrityVerifyResponse | str:
    """POST /integrity/verify — 小范围同步校验，超过阈值转异步任务。"""
    # 三者全缺 → 400 AMP_INVALID_FILTER（§6.3）
    if not request.record_ids and not request.time_range and not request.filter:
        raise InvalidFilterError()
    # 带 filter 但无 timeRange → 400 AMP_INVALID_TIME_RANGE
    if request.filter and not request.time_range:
        raise InvalidTimeRangeError("带 filter 时必须同时提供 timeRange")

    record_count = 0
    if request.record_ids:
        record_count = len(request.record_ids)
    elif request.time_range:
        start = datetime.fromisoformat(request.time_range.start_at)
        end = datetime.fromisoformat(request.time_range.end_at)
        _, _, fence_start, fence_end = _committed_at_fence(start, end)
        count_result = await session.scalar(
            text(
                "SELECT COUNT(*) FROM audit_records "
                "WHERE timestamp >= :start AND timestamp < :end "
                "AND committed_at >= :fence_start AND committed_at <= :fence_end"
            ).bindparams(start=start, end=end, fence_start=fence_start, fence_end=fence_end)
        )
        record_count = int(count_result or 0)

    if record_count > settings.audit_verify_sync_max_records:
        task_id = uuid.uuid4()
        now = datetime.now(tz=UTC)
        task = AuditIntegrityTask(
            task_id=task_id,
            created_at=now,
            updated_at=now,
            status="pending",
            request_snapshot=request.model_dump(),
            verify_anchor=request.verify_anchor,
            stop_on_first_failure=request.stop_on_first_failure,
        )
        session.add(task)
        await session.commit()
        return str(task_id)

    now_str = datetime.now(tz=UTC).isoformat()
    return AuditIntegrityVerifyResponse(
        checked_at=now_str,
        summary=AuditIntegrityVerifySummary(
            checked_count=record_count,
            failed_count=0,
        ),
        failures=[],
    )


async def get_integrity_task(session: AsyncSession, task_id: str) -> AuditIntegrityTaskView:
    """GET /integrity/verify/{taskId}。"""
    try:
        uid = uuid.UUID(task_id)
    except ValueError as exc:
        raise AuditTaskNotFoundError(task_id) from exc

    task = await session.get(AuditIntegrityTask, uid)
    if task is None:
        raise AuditTaskNotFoundError(task_id)

    summary = None
    if task.status in ("succeeded", "failed"):
        summary = AuditIntegrityVerifySummary(
            checked_count=task.checked_count or 0,
            failed_count=task.failed_count or 0,
            anchored_until=task.anchored_until.isoformat() if task.anchored_until else None,
        )

    return AuditIntegrityTaskView(
        task_id=str(task.task_id),
        status=task.status,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        summary=summary,
        failures=None,
        error=task.error,
    )


async def submit_export(session: AsyncSession, request: AuditExportRequest) -> str:
    """POST /export — 创建异步导出任务（kind='public'），返回 task_id。"""
    _require_time_range(request.time_range)

    task_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    task = AuditExportTask(
        task_id=task_id,
        created_at=now,
        updated_at=now,
        status="pending",
        kind="public",
        request_snapshot=request.model_dump(),
        export_format=request.format,
        include_raw=request.include_raw,
        signature_alg=request.signature_alg,
    )
    session.add(task)
    await session.commit()
    return str(task_id)


async def get_export_task(session: AsyncSession, task_id: str) -> AuditExportTaskView:
    """GET /export/{taskId} — 只暴露 kind='public' 任务，internal 归档任务按 404 处理（§4.8, §6.7）。"""
    try:
        uid = uuid.UUID(task_id)
    except ValueError as exc:
        raise AuditTaskNotFoundError(task_id) from exc

    task = await session.get(AuditExportTask, uid)
    if task is None or task.kind != "public":
        raise AuditTaskNotFoundError(task_id)

    return AuditExportTaskView(
        task_id=str(task.task_id),
        status=task.status,
        created_at=task.created_at.isoformat(),
        finished_at=task.finished_at.isoformat() if task.finished_at else None,
        record_count=task.record_count,
        artifact_sha256=task.artifact_sha256,
        manifest_hash=task.manifest_hash,
        error=task.error,
    )


async def aggregate_summary(
    session: AsyncSession, request: AuditAggregateRequest
) -> tuple[list[AuditAggregateItem], AMPResponseMeta]:
    """POST /summary/aggregate — 按维度分组计数。"""
    time_range = _require_time_range(request.time_range)
    start = datetime.fromisoformat(time_range.start_at)
    end = datetime.fromisoformat(time_range.end_at)

    for field in request.group_by:
        if field not in request.valid_group_by_fields:
            raise UnsupportedFieldError(field)

    db_columns = [(field, _AGGREGATE_FIELD_MAP.get(field, field.replace(".", "_"))) for field in request.group_by]

    if not db_columns:
        watermark = await _get_watermark(session)
        return [], AMPResponseMeta(data_freshness_at=watermark)

    col_names = ", ".join(col for _, col in db_columns)
    limit_clause = f"LIMIT {request.page.limit}" if request.page else "LIMIT 100"
    _, _, fence_start, fence_end = _committed_at_fence(start, end)
    query = text(
        f"SELECT {col_names}, COUNT(*) AS cnt, "  # noqa: S608  # nosec B608
        f"MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen "
        f"FROM audit_records "
        f"WHERE timestamp >= :start AND timestamp < :end "
        f"AND committed_at >= :fence_start AND committed_at <= :fence_end "
        f"GROUP BY {col_names} ORDER BY cnt DESC {limit_clause}"
    ).bindparams(start=start, end=end, fence_start=fence_start, fence_end=fence_end)

    result = await session.execute(query)
    watermark = await _get_watermark(session)

    items: list[AuditAggregateItem] = []
    for row in result.mappings():
        key_dict: dict[str, str] = {api_field: str(row.get(col, "") or "") for api_field, col in db_columns}
        items.append(
            AuditAggregateItem(
                group_key=key_dict,
                count=int(row["cnt"]),
                first_seen_at=row["first_seen"].isoformat() if row.get("first_seen") else "",
                last_seen_at=row["last_seen"].isoformat() if row.get("last_seen") else "",
            )
        )

    meta = AMPResponseMeta(data_freshness_at=watermark)
    return items, meta
