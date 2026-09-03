import contextlib
import hashlib
import hmac
import json
import math
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock, Timer
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
from fastapi import status
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db_session import get_sync_session
from app.sync.exception import SyncError, SyncErrorCode
from app.sync.model import ChangeLog, Snapshot, WebHook
from app.sync.schema import ChangeLogResponse, Envelope, SnapshotInfo
from app.utils.utils import get_beijing_time, sha256

if TYPE_CHECKING:
    from app.agent.model import Agent

logger = structlog.get_logger(__name__)

CHANGELOG_SEQ_COL = cast("Any", ChangeLog.seq)
CHANGELOG_TS_COL = cast("Any", ChangeLog.ts)
CHANGELOG_ID_COL = cast("Any", ChangeLog.id)
CHANGELOG_TYPE_COL = cast("Any", ChangeLog.type)
SNAPSHOT_ID_COL = cast("Any", Snapshot.id)
SNAPSHOT_IS_DELETED_COL = cast("Any", Snapshot.is_deleted)
SNAPSHOT_EXPIRE_AT_COL = cast("Any", Snapshot.expire_at)
SNAPSHOT_CREATED_AT_COL = cast("Any", Snapshot.created_at)
WEBHOOK_ID_COL = cast("Any", WebHook.id)
WEBHOOK_STATUS_COL = cast("Any", WebHook.status)
WEBHOOK_EVENTS_COL = cast("Any", WebHook.events)
WEBHOOK_TYPES_COL = cast("Any", WebHook.types)
WEBHOOK_CREATED_AT_COL = cast("Any", WebHook.created_at)


@dataclass
class DataChangeBatchState:
    types: set[str] = field(default_factory=set)
    max_seq: int | None = None
    timer: Timer | None = None


def generate_next_seq(db: Session) -> int:
    """生成下一个全局序列号"""
    try:
        result = db.execute(text("SELECT nextval('global_seq')"))
        return int(result.scalar_one())
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.GLOBAL_SEQ_GENERATE_FAILED,
            error_msg=f"Failed to generate global sequence: {e!s}",
            input_params={},
        ) from None


def create_change_log(
    db: Session,
    data_type: str,
    object_id: str,
    version: int,
    payload: Any | None = None,
    op: str = "upsert",  # 新增操作类型参数，默认为upsert
    seq: int | None = None,
) -> ChangeLog:
    """创建变更日志记录"""
    try:
        # 如果没有提供seq，则生成新的
        if seq is None:
            seq = generate_next_seq(db)

        change_log = ChangeLog(
            seq=seq,
            ts=get_beijing_time(),
            type=data_type,
            op=op,  # 添加操作类型
            id=object_id,
            version=version,
            payload=payload,
        )

        db.add(change_log)
        # 注意：这里不提交事务，让调用方控制事务
        return change_log

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGELOG_CREATE_FAILED,
            error_msg=f"Failed to create change log entry: {e!s}",
            input_params={
                "object_id": object_id,
                "version": version,
                "type": data_type,
            },
        ) from None


async def generate_next_seq_async(session: AsyncSession) -> int:
    """异步生成下一个全局序列号。"""
    try:
        result = await session.execute(text("SELECT nextval('global_seq')"))
        return int(result.scalar_one())
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.GLOBAL_SEQ_GENERATE_FAILED,
            error_msg=f"Failed to generate global sequence: {e!s}",
            input_params={},
        ) from None


async def create_change_log_async(
    session: AsyncSession,
    data_type: str,
    object_id: str,
    version: int,
    payload: Any | None = None,
    op: str = "upsert",
    seq: int | None = None,
) -> ChangeLog:
    """异步创建变更日志记录。"""
    try:
        if seq is None:
            seq = await generate_next_seq_async(session)

        change_log = ChangeLog(
            seq=seq,
            ts=get_beijing_time(),
            type=data_type,
            op=op,
            id=object_id,
            version=version,
            payload=payload,
        )

        session.add(change_log)
        return change_log
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGELOG_CREATE_FAILED,
            error_msg=f"Failed to create change log entry: {e!s}",
            input_params={
                "object_id": object_id,
                "version": version,
                "type": data_type,
            },
        ) from None


def update_agent_with_changelog(db: Session, agent: Agent, agent_data: dict[str, Any]) -> Agent:
    """
    更新Agent并在acs数据变化时创建ChangeLog记录
    这个函数在一个事务中完成以下操作：
    1. 检查acs是否变化
    2. 如果变化，生成新的seq
    3. 更新Agent的acs_version和acs_last_seq
    4. 创建ChangeLog记录

    注意：此函数不修改ACS的内容，ACS内容的更新由update_agent_acs_data负责

    Args:
        db: 数据库会话
        agent: Agent对象实例（不是agent_id）
        agent_data: 要更新的数据
    """
    try:
        # 检查是否有acs数据变化
        acs_changed = False
        new_acs_hash = None

        if "acs" in agent_data:
            new_acs = agent_data["acs"]
            if new_acs:
                # acs 现在是 JSONB 类型（dict），需要序列化为字符串来计算 hash
                if isinstance(new_acs, dict):
                    new_acs_hash = sha256(json.dumps(new_acs, ensure_ascii=False))
                elif isinstance(new_acs, str):
                    new_acs_hash = sha256(new_acs)
                else:
                    new_acs_hash = None
            else:
                new_acs_hash = None

            # 比较acs_hash是否不同
            if new_acs_hash != agent.acs_hash:
                acs_changed = True

        if acs_changed:
            # 生成新的seq值
            new_seq = generate_next_seq(db)

            # 更新Agent的acs相关字段（但不修改acs内容本身）
            agent.acs_hash = new_acs_hash
            agent.acs_version = (agent.acs_version or 0) + 1
            agent.acs_last_seq = new_seq

            # 创建ChangeLog记录
            if agent.aic:  # 只有有AIC的Agent才记录ChangeLog
                create_change_log(
                    db=db,
                    data_type="acs",
                    object_id=agent.aic,
                    version=agent.acs_version,
                    payload=agent_data["acs"],  # 使用传入的acs数据
                    op="upsert",  # 更新操作默认为upsert
                    seq=new_seq,
                )

        # 更新其他字段（除了acs相关的同步字段）
        for key, value in agent_data.items():
            if key not in [
                "acs",
                "acs_hash",
                "acs_version",
                "acs_last_seq",
            ] and hasattr(agent, key):
                setattr(agent, key, value)

        # 更新时间戳
        agent.updated_at = get_beijing_time()

        # 注意：这里不调用db.add(agent)，让调用方控制
        # 也不提交事务，让调用方控制事务

        return agent

    except Exception as e:
        if isinstance(e, SyncError):
            raise
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGELOG_CREATE_FAILED,
            error_msg=f"Failed to update agent with changelog: {e!s}",
            input_params={"agent_id": str(agent.id), "agent_data": agent_data},
        ) from None


async def update_agent_with_changelog_async(
    session: AsyncSession,
    agent: Agent,
    agent_data: dict[str, Any],
) -> Agent:
    """异步更新 Agent 并在 ACS 变化时创建 ChangeLog。"""
    try:
        acs_changed = False
        new_acs_hash = None

        if "acs" in agent_data:
            new_acs = agent_data["acs"]
            if new_acs:
                if isinstance(new_acs, dict):
                    new_acs_hash = sha256(json.dumps(new_acs, ensure_ascii=False))
                elif isinstance(new_acs, str):
                    new_acs_hash = sha256(new_acs)

            if new_acs_hash != agent.acs_hash:
                acs_changed = True

        if acs_changed:
            new_seq = await generate_next_seq_async(session)

            agent.acs_hash = new_acs_hash
            agent.acs_version = (agent.acs_version or 0) + 1
            agent.acs_last_seq = new_seq

            if agent.aic:
                await create_change_log_async(
                    session=session,
                    data_type="acs",
                    object_id=agent.aic,
                    version=agent.acs_version,
                    payload=agent_data["acs"],
                    op="upsert",
                    seq=new_seq,
                )

        for key, value in agent_data.items():
            if key not in ["acs", "acs_hash", "acs_version", "acs_last_seq"] and hasattr(agent, key):
                setattr(agent, key, value)

        agent.updated_at = get_beijing_time()
        return agent
    except Exception as e:
        if isinstance(e, SyncError):
            raise
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGELOG_CREATE_FAILED,
            error_msg=f"Failed to update agent with changelog: {e!s}",
            input_params={"agent_id": str(agent.id), "agent_data": agent_data},
        ) from None


def _build_envelope(item: Any) -> Envelope | None:
    try:
        payload_data = None
        if item.payload is not None:
            payload_data = json.loads(item.payload) if isinstance(item.payload, str) else item.payload

        return Envelope(
            seq=item.seq,
            ts=item.ts,
            op=item.op,
            type=item.type,
            id=item.id,
            version=item.version,
            payload=payload_data,
        )
    except json.JSONDecodeError:
        return None


def _build_envelopes(items: Sequence[Any]) -> list[Envelope]:
    envelopes: list[Envelope] = []
    for item in items:
        envelope = _build_envelope(item)
        if envelope is not None:
            envelopes.append(envelope)

    return envelopes


def _build_changes_result(changes: Sequence[Any], initial_seq: int) -> tuple[list[Envelope], int]:
    envelopes: list[Envelope] = []
    next_seq = initial_seq

    for change in changes:
        envelope = _build_envelope(change)
        if envelope is None:
            continue

        envelopes.append(envelope)
        next_seq = envelope.seq

    return envelopes, next_seq


def get_changes(
    db: Session,
    seq: int | None = None,
    limit: int = 1000,
    types: list[str] | None = None,
) -> tuple[list[Envelope], int]:
    """
    获取增量变更数据
    返回: (变更列表, 下一个seq号)
    """
    try:
        if seq is not None:
            oldest_seq_result = db.query(func.min(CHANGELOG_SEQ_COL)).scalar()
            if oldest_seq_result and seq < oldest_seq_result:
                raise SyncError(
                    status_code=status.HTTP_410_GONE,
                    error_name=SyncErrorCode.RETENTION_WINDOW_EXCEEDED,
                    error_msg=(
                        f"Requested seq {seq} is too old. Oldest available seq is {oldest_seq_result}. "
                        "Please perform a snapshot sync."
                    ),
                    input_params={
                        "requested_seq": seq,
                        "oldest_seq": oldest_seq_result,
                    },
                )

        query = db.query(ChangeLog)
        if seq is not None:
            query = query.filter(seq < CHANGELOG_SEQ_COL)
        if types:
            query = query.filter(CHANGELOG_TYPE_COL.in_(types))

        changes = query.order_by(CHANGELOG_SEQ_COL).limit(limit).all()
        return _build_changes_result(changes, seq or 0)

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to query changes: {e!s}",
            input_params={"seq": seq, "limit": limit, "types": types},
        ) from None


def get_changelog_list(
    db: Session,
    page_num: int = 1,
    page_size: int = 10,
    object_id: str | None = None,
    data_type: str | None = None,
) -> tuple[list[ChangeLog], int]:
    """获取变更日志列表"""
    try:
        query = db.query(ChangeLog)

        # 应用过滤条件
        if object_id:
            query = query.filter(object_id == CHANGELOG_ID_COL)
        if data_type:
            query = query.filter(data_type == CHANGELOG_TYPE_COL)

        # 获取总数
        total = query.count()

        # 计算分页偏移量
        skip = (page_num - 1) * page_size

        # 应用分页和排序
        change_logs = query.order_by(CHANGELOG_SEQ_COL.desc()).offset(skip).limit(page_size).all()

        return change_logs, total

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to query change logs: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "object_id": object_id,
                "data_type": data_type,
            },
        ) from None


def get_retention_oldest_seq(db: Session, window_hours: int, max_records: int) -> int:
    """
    获取保留窗口内最老的seq号
    基于时间窗口和最大记录数两个条件
    """
    try:
        current_time = get_beijing_time()
        cutoff_time = current_time - timedelta(hours=window_hours)

        # 基于时间窗口的最老seq：在cutoff_time之后的最小seq
        time_based_seq = db.query(func.min(CHANGELOG_SEQ_COL)).filter(cutoff_time <= CHANGELOG_TS_COL).scalar()

        # 基于最大记录数的最老seq：最新max_records条记录中的最小seq
        record_based_seq = (
            db.query(CHANGELOG_SEQ_COL).order_by(CHANGELOG_SEQ_COL.desc()).offset(max_records - 1).limit(1).scalar()
        )

        min_seq = None
        if not time_based_seq and not record_based_seq:
            min_seq = db.query(func.min(CHANGELOG_SEQ_COL)).scalar()

        return _resolve_retention_oldest_seq(time_based_seq, record_based_seq, min_seq)

    except SQLAlchemyError:
        # 如果查询失败，返回1作为保守值
        return 1


def cleanup_old_changelog_entries(db: Session, window_hours: int, max_records: int) -> int:
    """
    清理超出保留窗口的旧ChangeLog条目

    Args:
        db: 数据库会话
        window_hours: 保留窗口时长（小时）
        max_records: 保留的最大记录数

    Returns:
        清理的记录数量
    """
    try:
        current_time = get_beijing_time()
        cutoff_time = current_time - timedelta(hours=window_hours)

        # 基于时间窗口删除旧记录
        time_based_delete = db.query(ChangeLog).filter(cutoff_time > CHANGELOG_TS_COL).delete(synchronize_session=False)

        # 基于最大记录数删除多余记录
        total_count = db.query(ChangeLog).count()
        record_based_delete = 0

        if total_count > max_records:
            # 获取需要保留的最小seq（最新的max_records条记录的最小seq）
            keep_seq_threshold = (
                db.query(CHANGELOG_SEQ_COL).order_by(CHANGELOG_SEQ_COL.desc()).offset(max_records - 1).limit(1).scalar()
            )

            if keep_seq_threshold:
                record_based_delete = (
                    db.query(ChangeLog).filter(keep_seq_threshold > CHANGELOG_SEQ_COL).delete(synchronize_session=False)
                )

        return time_based_delete + record_based_delete

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to cleanup old changelogs: {e!s}",
            input_params={
                "window_hours": window_hours,
                "max_records": max_records,
            },
        ) from None


def get_current_max_seq(db: Session) -> int:
    """获取当前最大的seq号"""
    result = db.query(func.max(CHANGELOG_SEQ_COL)).scalar()
    return result or 0


def create_changelog_response(change_log: ChangeLog) -> ChangeLogResponse:
    """将ChangeLog ORM对象转换为响应模型"""
    return ChangeLogResponse.model_validate(change_log)


async def get_changes_async(
    session: AsyncSession,
    seq: int | None = None,
    limit: int = 1000,
    types: list[str] | None = None,
) -> tuple[list[Envelope], int]:
    """获取增量变更（异步请求路径）。"""
    try:
        if seq is not None:
            oldest_seq_result = await session.execute(select(func.min(CHANGELOG_SEQ_COL)))
            oldest_seq = oldest_seq_result.scalar()
            if oldest_seq and seq < oldest_seq:
                raise SyncError(
                    status_code=status.HTTP_410_GONE,
                    error_name=SyncErrorCode.RETENTION_WINDOW_EXCEEDED,
                    error_msg=(
                        f"Requested seq {seq} is too old. Oldest available seq is {oldest_seq}. "
                        "Please perform a snapshot sync."
                    ),
                    input_params={
                        "requested_seq": seq,
                        "oldest_seq": oldest_seq,
                    },
                )

        stmt = select(ChangeLog)
        if seq is not None:
            stmt = stmt.where(seq < CHANGELOG_SEQ_COL)
        if types:
            stmt = stmt.where(CHANGELOG_TYPE_COL.in_(types))
        stmt = stmt.order_by(CHANGELOG_SEQ_COL).limit(limit)

        result = await session.execute(stmt)
        changes = list(result.scalars().all())
        return _build_changes_result(changes, seq or 0)
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to query changes: {e!s}",
            input_params={"seq": seq, "limit": limit, "types": types},
        ) from None


async def get_changelog_list_async(
    session: AsyncSession,
    page_num: int = 1,
    page_size: int = 10,
    object_id: str | None = None,
    data_type: str | None = None,
) -> tuple[list[ChangeLog], int]:
    """获取变更日志列表（异步请求路径）。"""
    try:
        filters: list[Any] = []
        if object_id:
            filters.append(object_id == CHANGELOG_ID_COL)
        if data_type:
            filters.append(data_type == CHANGELOG_TYPE_COL)

        stmt = select(ChangeLog)
        count_stmt = select(func.count()).select_from(ChangeLog)
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        skip = (page_num - 1) * page_size
        stmt = stmt.order_by(CHANGELOG_SEQ_COL.desc()).offset(skip).limit(page_size)

        result = await session.execute(stmt)
        count_result = await session.execute(count_stmt)
        return list(result.scalars().all()), int(count_result.scalar_one())
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to query change logs: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "object_id": object_id,
                "data_type": data_type,
            },
        ) from None


async def get_retention_oldest_seq_async(session: AsyncSession, window_hours: int, max_records: int) -> int:
    """获取保留窗口内的最早序列号（异步请求路径）。"""
    try:
        current_time = get_beijing_time()
        cutoff_time = current_time - timedelta(hours=window_hours)

        time_based_result = await session.execute(
            select(func.min(CHANGELOG_SEQ_COL)).where(cutoff_time <= CHANGELOG_TS_COL)
        )
        time_based_seq = time_based_result.scalar()

        record_based_result = await session.execute(
            select(CHANGELOG_SEQ_COL).order_by(CHANGELOG_SEQ_COL.desc()).offset(max_records - 1).limit(1)
        )
        record_based_seq = record_based_result.scalar()

        min_seq_result = await session.execute(select(func.min(CHANGELOG_SEQ_COL)))
        min_seq = min_seq_result.scalar()
        return _resolve_retention_oldest_seq(time_based_seq, record_based_seq, min_seq)
    except SQLAlchemyError:
        return 1


async def cleanup_old_changelog_entries_async(session: AsyncSession, window_hours: int, max_records: int) -> int:
    """清理旧变更日志记录（异步请求路径）。"""
    try:
        current_time = get_beijing_time()
        cutoff_time = current_time - timedelta(hours=window_hours)

        time_based_result = await session.execute(delete(ChangeLog).where(cutoff_time > CHANGELOG_TS_COL))
        time_based_delete = int(getattr(time_based_result, "rowcount", 0) or 0)

        total_count_result = await session.execute(select(func.count()).select_from(ChangeLog))
        total_count = int(total_count_result.scalar_one())
        record_based_delete = 0

        if total_count > max_records:
            keep_seq_result = await session.execute(
                select(CHANGELOG_SEQ_COL).order_by(CHANGELOG_SEQ_COL.desc()).offset(max_records - 1).limit(1)
            )
            keep_seq_threshold = keep_seq_result.scalar()
            if keep_seq_threshold:
                record_based_result = await session.execute(
                    delete(ChangeLog).where(keep_seq_threshold > CHANGELOG_SEQ_COL)
                )
                record_based_delete = int(getattr(record_based_result, "rowcount", 0) or 0)

        return time_based_delete + record_based_delete
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to cleanup old changelogs: {e!s}",
            input_params={
                "window_hours": window_hours,
                "max_records": max_records,
            },
        ) from None


async def get_current_max_seq_async(session: AsyncSession) -> int:
    """获取当前最大序列号（异步请求路径）。"""
    result = await session.execute(select(func.max(CHANGELOG_SEQ_COL)))
    value = result.scalar()
    return int(value) if value else 0


# Snapshot相关的服务函数


def generate_snapshot_id() -> str:
    """生成快照ID"""
    return f"snap_{uuid.uuid4().hex[:12]}"


def calculate_expire_time(access_timeout_hours: int | None = None, max_lifetime_hours: int | None = None) -> datetime:
    """计算快照过期时间，取访问超时和最大生存时间的较小值"""
    if access_timeout_hours is None:
        access_timeout_hours = settings.dsp_snapshot_access_timeout_hours
    if max_lifetime_hours is None:
        max_lifetime_hours = settings.dsp_snapshot_max_lifetime_hours

    now = get_beijing_time()
    access_expire = now + timedelta(hours=access_timeout_hours)
    max_expire = now + timedelta(hours=max_lifetime_hours)
    return min(access_expire, max_expire)


def _build_snapshot_table_name(snapshot_id: str) -> str:
    return f"snapshot_{snapshot_id.replace('snap_', '')}"


def _validated_snapshot_table_name(table_name: str) -> str:
    """验证内部生成的快照表名，避免其进入动态 SQL 时携带非法字符。"""

    suffix = table_name.removeprefix("snapshot_")
    if not suffix or not table_name.startswith("snapshot_") or not suffix.isalnum():
        raise ValueError(f"非法快照表名：{table_name!r}")
    return table_name


def _build_snapshot_query_filters(types: list[str], from_seq: int | None) -> tuple[str, dict[str, Any]]:
    query_conditions: list[str] = []
    params: dict[str, Any] = {}

    if "acs" in types:
        query_conditions.append("a.acs IS NOT NULL AND a.aic IS NOT NULL")

    if from_seq is not None:
        query_conditions.append("a.acs_last_seq > :from_seq")
        params["from_seq"] = from_seq

    query_conditions.append("a.is_active = true AND a.is_deleted = false")
    where_clause = " AND ".join(query_conditions) if query_conditions else "1=1"
    return where_clause, params


def _build_snapshot_create_table_sql(table_name: str, where_clause: str) -> str:
    safe_table_name = _validated_snapshot_table_name(table_name)
    return (
        f"CREATE TABLE {safe_table_name} AS\n"  # nosec B608 - validated internal table name
        "SELECT\n"
        "  COALESCE(a.acs_last_seq, 0) AS seq,\n"
        "  a.updated_at AS ts,\n"
        "  'upsert' AS op,\n"
        "  'acs' AS type,\n"
        "  a.aic AS id,\n"
        "  COALESCE(a.acs_version, 1) AS version,\n"
        "  a.acs AS payload\n"
        "FROM agent a\n"
        f"WHERE {where_clause}\n"  # nosec B608 - assembled exclusively from fixed predicates
        "  AND a.acs_last_seq <= :current_seq\n"
        "ORDER BY COALESCE(a.acs_last_seq, 0)\n"
    )


def _build_snapshot_select_sql(table_name: str, include_offset: bool = False) -> str:
    safe_table_name = _validated_snapshot_table_name(table_name)
    offset_clause = " OFFSET :offset" if include_offset else ""
    return (
        "SELECT seq, ts, op, type, id, version, payload\n"
        f"FROM {safe_table_name}\n"  # nosec B608 - validated internal table name
        "ORDER BY seq\n"
        f"LIMIT :limit{offset_clause}\n"  # nosec B608 - offset is selected from a fixed clause
    )


def _build_snapshot_envelopes(rows: Sequence[Any]) -> list[Envelope]:
    return _build_envelopes(rows)


def _build_snapshot_model(
    snapshot_id: str,
    types: list[str],
    current_seq: int,
    chunk_total: int,
    object_count: int,
    from_seq: int | None,
) -> Snapshot:
    current_time = get_beijing_time()
    return Snapshot(
        id=snapshot_id,
        types=",".join(types),
        seq=current_seq,
        chunk_total=chunk_total,
        object_count=object_count,
        from_seq=from_seq,
        is_deleted=False,
        created_at=current_time,
        last_access_at=current_time,
        expire_at=calculate_expire_time(),
    )


def _validate_snapshot_chunk_request(snapshot: Snapshot | None, snapshot_id: str, chunk_index: int) -> Snapshot:
    if not snapshot:
        raise SyncError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=SyncErrorCode.SNAPSHOT_NOT_FOUND,
            error_msg=f"Snapshot {snapshot_id} not found",
            input_params={"snapshot_id": snapshot_id},
        )

    if get_beijing_time() > snapshot.expire_at:
        raise SyncError(
            status_code=status.HTTP_410_GONE,
            error_name=SyncErrorCode.SNAPSHOT_EXPIRED,
            error_msg=f"Snapshot {snapshot_id} has expired",
            input_params={"snapshot_id": snapshot_id},
        )

    if chunk_index < 0 or chunk_index >= snapshot.chunk_total:
        raise SyncError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=SyncErrorCode.INVALID_CHUNK_INDEX,
            error_msg=f"Invalid chunk index {chunk_index}. Must be between 0 and {snapshot.chunk_total - 1}",
            input_params={
                "snapshot_id": snapshot_id,
                "chunk_index": chunk_index,
                "chunk_total": snapshot.chunk_total,
            },
        )

    return snapshot


def _mark_snapshot_deleted(snapshot: Snapshot) -> None:
    snapshot.is_deleted = True


def _resolve_retention_oldest_seq(
    time_based_seq: int | None,
    record_based_seq: int | None,
    min_seq: int | None,
) -> int:
    if time_based_seq and record_based_seq:
        return max(int(time_based_seq), int(record_based_seq))
    if time_based_seq:
        return int(time_based_seq)
    if record_based_seq:
        return int(record_based_seq)
    return int(min_seq) if min_seq else 1


def get_snapshot_list(
    db: Session,
    page_num: int = 1,
    page_size: int = 10,
    include_deleted: bool = False,
) -> tuple[list[Snapshot], int]:
    """分页获取快照列表。"""
    try:
        query = db.query(Snapshot)

        if not include_deleted:
            query = query.filter(SNAPSHOT_IS_DELETED_COL.is_(False))

        total = query.count()
        skip = (page_num - 1) * page_size
        snapshots = query.order_by(SNAPSHOT_CREATED_AT_COL.desc()).offset(skip).limit(page_size).all()
        return snapshots, total
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to list snapshots: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "include_deleted": include_deleted,
            },
        ) from None


async def create_snapshot_async(
    session: AsyncSession,
    types: list[str],
    limit: int = 10000,
    from_seq: int | None = None,
) -> tuple[Snapshot, list[Envelope]]:
    """创建快照（异步请求路径）。"""
    try:
        snapshot_id = generate_snapshot_id()
        current_seq = await get_current_max_seq_async(session)
        table_name = _build_snapshot_table_name(snapshot_id)
        where_clause, params = _build_snapshot_query_filters(types, from_seq)
        create_table_sql = _build_snapshot_create_table_sql(table_name, where_clause)
        params["current_seq"] = current_seq
        await session.execute(text(create_table_sql), params)

        safe_table_name = _validated_snapshot_table_name(table_name)
        count_result = await session.execute(text(f"SELECT COUNT(*) FROM {safe_table_name}"))  # nosec B608 - validated internal table name
        object_count = count_result.scalar() or 0
        chunk_total = max(1, math.ceil(object_count / limit))

        first_chunk_sql = _build_snapshot_select_sql(table_name)
        result = await session.execute(text(first_chunk_sql), {"limit": limit})
        rows = result.fetchall()
        envelopes = _build_snapshot_envelopes(rows)

        snapshot = _build_snapshot_model(
            snapshot_id=snapshot_id,
            types=types,
            current_seq=current_seq,
            chunk_total=chunk_total,
            object_count=object_count,
            from_seq=from_seq,
        )

        session.add(snapshot)
        await session.flush()
        return snapshot, envelopes
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_CREATE_FAILED,
            error_msg=f"Failed to create snapshot: {e!s}",
            input_params={
                "types": types,
                "limit": limit,
                "from_seq": from_seq,
            },
        ) from None


async def get_snapshot_chunk_async(
    session: AsyncSession,
    snapshot_id: str,
    chunk_index: int,
    limit: int = 10000,
) -> tuple[Snapshot, list[Envelope]]:
    """获取快照分片（异步请求路径）。"""
    try:
        stmt = select(Snapshot).where(snapshot_id == SNAPSHOT_ID_COL, SNAPSHOT_IS_DELETED_COL.is_(False)).limit(1)
        result = await session.execute(stmt)
        snapshot = _validate_snapshot_chunk_request(result.scalar_one_or_none(), snapshot_id, chunk_index)

        snapshot.last_access_at = get_beijing_time()
        session.add(snapshot)

        table_name = _build_snapshot_table_name(snapshot_id)
        offset = chunk_index * limit
        chunk_sql = _build_snapshot_select_sql(table_name, include_offset=True)
        chunk_result = await session.execute(text(chunk_sql), {"limit": limit, "offset": offset})
        rows = chunk_result.fetchall()
        envelopes = _build_snapshot_envelopes(rows)

        await session.flush()
        return snapshot, envelopes
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to get snapshot chunk: {e!s}",
            input_params={
                "snapshot_id": snapshot_id,
                "chunk_index": chunk_index,
                "limit": limit,
            },
        ) from None


async def delete_snapshot_async(session: AsyncSession, snapshot_id: str) -> bool:
    """删除快照（异步请求路径）。"""
    try:
        result = await session.execute(select(Snapshot).where(snapshot_id == SNAPSHOT_ID_COL).limit(1))
        snapshot = result.scalar_one_or_none()
        if not snapshot:
            return True

        table_name = _build_snapshot_table_name(snapshot_id)
        with contextlib.suppress(SQLAlchemyError):
            await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        _mark_snapshot_deleted(snapshot)
        session.add(snapshot)
        await session.flush()
        return True
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_TABLE_DROP_FAILED,
            error_msg=f"Failed to delete snapshot: {e!s}",
            input_params={"snapshot_id": snapshot_id},
        ) from None


async def cleanup_expired_snapshots_async(session: AsyncSession) -> int:
    """清理过期快照（异步请求路径）。"""
    try:
        current_time = get_beijing_time()
        stmt = select(Snapshot).where(current_time > SNAPSHOT_EXPIRE_AT_COL, SNAPSHOT_IS_DELETED_COL.is_(False))
        result = await session.execute(stmt)
        expired_snapshots = list(result.scalars().all())

        cleaned_count = 0
        for snapshot in expired_snapshots:
            try:
                table_name = _build_snapshot_table_name(snapshot.id)
                await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                _mark_snapshot_deleted(snapshot)
                session.add(snapshot)
                cleaned_count += 1
            except SQLAlchemyError:
                continue

        if cleaned_count > 0:
            await session.flush()
        return cleaned_count
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_TABLE_DROP_FAILED,
            error_msg=f"Failed to cleanup expired snapshots: {e!s}",
            input_params={},
        ) from None


async def get_snapshot_info_async(session: AsyncSession, snapshot_id: str) -> SnapshotInfo:
    """获取快照信息（异步请求路径）。"""
    try:
        result = await session.execute(select(Snapshot).where(snapshot_id == SNAPSHOT_ID_COL).limit(1))
        snapshot = result.scalar_one_or_none()
        if not snapshot:
            raise SyncError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_name=SyncErrorCode.SNAPSHOT_NOT_FOUND,
                error_msg=f"Snapshot {snapshot_id} not found",
                input_params={"snapshot_id": snapshot_id},
            )
        return SnapshotInfo.model_validate(snapshot)
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to get snapshot info: {e!s}",
            input_params={"snapshot_id": snapshot_id},
        ) from None


async def get_snapshot_list_async(
    session: AsyncSession,
    page_num: int = 1,
    page_size: int = 10,
    include_deleted: bool = False,
) -> tuple[list[Snapshot], int]:
    """获取快照列表（异步请求路径）。"""
    try:
        stmt = select(Snapshot)
        count_stmt = select(func.count()).select_from(Snapshot)

        if not include_deleted:
            stmt = stmt.where(SNAPSHOT_IS_DELETED_COL.is_(False))
            count_stmt = count_stmt.where(SNAPSHOT_IS_DELETED_COL.is_(False))

        skip = (page_num - 1) * page_size
        stmt = stmt.order_by(SNAPSHOT_CREATED_AT_COL.desc()).offset(skip).limit(page_size)

        result = await session.execute(stmt)
        count_result = await session.execute(count_stmt)
        return list(result.scalars().all()), int(count_result.scalar_one())
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to list snapshots: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "include_deleted": include_deleted,
            },
        ) from None


def create_snapshot(
    db: Session,
    types: list[str],
    limit: int = 10000,
    from_seq: int | None = None,
) -> tuple[Snapshot, list[Envelope]]:
    """
    创建快照并返回第一个chunk的数据

    Args:
        db: 数据库会话
        types: 数据类型列表
        limit: 每个chunk的最大对象数量
        from_seq: 增量快照的起始序列号，None表示全量快照

    Returns:
        (Snapshot对象, 第一个chunk的数据)
    """
    try:
        # 生成快照ID
        snapshot_id = generate_snapshot_id()

        # 获取当前最大seq作为快照的切点
        current_seq = get_current_max_seq(db)

        # 创建物化表名
        table_name = _build_snapshot_table_name(snapshot_id)
        where_clause, params = _build_snapshot_query_filters(types, from_seq)
        create_table_sql = _build_snapshot_create_table_sql(table_name, where_clause)
        params["current_seq"] = current_seq

        # 执行创建物化表
        db.execute(text(create_table_sql), params)

        # 获取总对象数量
        safe_table_name = _validated_snapshot_table_name(table_name)
        count_result = db.execute(text(f"SELECT COUNT(*) FROM {safe_table_name}")).scalar()  # nosec B608 - validated internal table name
        object_count = count_result or 0

        # 计算总chunk数量
        chunk_total = max(1, math.ceil(object_count / limit))

        # 获取第一个chunk的数据
        first_chunk_sql = _build_snapshot_select_sql(table_name)

        result = db.execute(text(first_chunk_sql), {"limit": limit})
        rows = result.fetchall()

        envelopes = _build_snapshot_envelopes(rows)

        # 创建Snapshot记录
        snapshot = _build_snapshot_model(
            snapshot_id=snapshot_id,
            types=types,
            current_seq=current_seq,
            chunk_total=chunk_total,
            object_count=object_count,
            from_seq=from_seq,
        )

        db.add(snapshot)
        db.flush()

        return snapshot, envelopes

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_CREATE_FAILED,
            error_msg=f"Failed to create snapshot: {e!s}",
            input_params={
                "types": types,
                "limit": limit,
                "from_seq": from_seq,
            },
        ) from None


def get_snapshot_chunk(
    db: Session, snapshot_id: str, chunk_index: int, limit: int = 10000
) -> tuple[Snapshot, list[Envelope]]:
    """
    获取快照的指定chunk数据

    Args:
        db: 数据库会话
        snapshot_id: 快照ID
        chunk_index: chunk索引（从0开始）
        limit: 每个chunk的最大对象数量

    Returns:
        (Snapshot对象, chunk数据)
    """
    try:
        # 获取快照信息
        snapshot = _validate_snapshot_chunk_request(
            db.query(Snapshot).filter(snapshot_id == SNAPSHOT_ID_COL, SNAPSHOT_IS_DELETED_COL.is_(False)).first(),
            snapshot_id,
            chunk_index,
        )

        # 更新最后访问时间
        snapshot.last_access_at = get_beijing_time()
        db.add(snapshot)

        # 构建物化表名
        table_name = _build_snapshot_table_name(snapshot_id)

        # 计算分页参数
        offset = chunk_index * limit

        # 获取chunk数据
        chunk_sql = _build_snapshot_select_sql(table_name, include_offset=True)

        result = db.execute(text(chunk_sql), {"limit": limit, "offset": offset})
        rows = result.fetchall()

        envelopes = _build_snapshot_envelopes(rows)

        db.flush()
        return snapshot, envelopes

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to get snapshot chunk: {e!s}",
            input_params={
                "snapshot_id": snapshot_id,
                "chunk_index": chunk_index,
                "limit": limit,
            },
        ) from None


def delete_snapshot(db: Session, snapshot_id: str) -> bool:
    """
    删除快照及其物化表

    Args:
        db: 数据库会话
        snapshot_id: 快照ID

    Returns:
        是否成功删除
    """
    try:
        # 获取快照信息
        snapshot = db.query(Snapshot).filter(snapshot_id == SNAPSHOT_ID_COL).first()

        if not snapshot:
            # 快照不存在时仍然返回成功，符合幂等性要求
            return True

        # 删除物化表（如果存在）
        table_name = _build_snapshot_table_name(snapshot_id)
        with contextlib.suppress(SQLAlchemyError):
            db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        # 标记快照为已删除
        _mark_snapshot_deleted(snapshot)
        db.add(snapshot)
        db.flush()

        return True

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_TABLE_DROP_FAILED,
            error_msg=f"Failed to delete snapshot: {e!s}",
            input_params={"snapshot_id": snapshot_id},
        ) from None


def cleanup_expired_snapshots(db: Session) -> int:
    """
    清理过期的快照

    Args:
        db: 数据库会话

    Returns:
        清理的快照数量
    """
    try:
        current_time = get_beijing_time()

        # 查找过期的快照
        expired_snapshots = (
            db.query(Snapshot).filter(current_time > SNAPSHOT_EXPIRE_AT_COL, SNAPSHOT_IS_DELETED_COL.is_(False)).all()
        )

        cleaned_count = 0

        for snapshot in expired_snapshots:
            try:
                # 删除物化表
                table_name = _build_snapshot_table_name(snapshot.id)
                db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

                # 标记为已删除
                _mark_snapshot_deleted(snapshot)
                db.add(snapshot)
                cleaned_count += 1

            except SQLAlchemyError:
                # 继续处理其他快照
                continue

        if cleaned_count > 0:
            db.flush()

        return cleaned_count

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_TABLE_DROP_FAILED,
            error_msg=f"Failed to cleanup expired snapshots: {e!s}",
            input_params={},
        ) from None


def get_snapshot_info(db: Session, snapshot_id: str) -> SnapshotInfo:
    """
    获取快照信息

    Args:
        db: 数据库会话
        snapshot_id: 快照ID

    Returns:
        快照信息
    """
    try:
        snapshot = db.query(Snapshot).filter(snapshot_id == SNAPSHOT_ID_COL).first()

        if not snapshot:
            raise SyncError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_name=SyncErrorCode.SNAPSHOT_NOT_FOUND,
                error_msg=f"Snapshot {snapshot_id} not found",
                input_params={"snapshot_id": snapshot_id},
            )

        return SnapshotInfo.model_validate(snapshot)

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_DATA_QUERY_FAILED,
            error_msg=f"Failed to get snapshot info: {e!s}",
            input_params={"snapshot_id": snapshot_id},
        ) from None


# WebHook相关的服务函数

_inflight_sends_lock: Lock = Lock()
_inflight_sends = set()  # Set[tuple[str, str]] => (webhook_id, event)

_data_change_batch_lock: Lock = Lock()
_data_change_batch_state = DataChangeBatchState()


def _mark_inflight(webhook_id: str, event: str) -> bool:
    """把一个webhook-event标记为正在进行中；如果已经在进行中，则返回 False"""
    with _inflight_sends_lock:
        key = (webhook_id, event)
        if key in _inflight_sends:
            return False
        _inflight_sends.add(key)
        return True


def _clear_inflight(webhook_id: str, event: str) -> None:
    """清除某个 webhook-event 对的 in-flight 标记。"""
    with _inflight_sends_lock:
        _inflight_sends.discard((webhook_id, event))


def generate_webhook_id() -> str:
    """生成WebHook ID"""
    return f"wh_{uuid.uuid4().hex[:12]}"


def _build_webhook_model(
    *,
    url: str,
    secret: str,
    types: list[str],
    events: list[str],
    description: str | None,
) -> WebHook:
    current_time = get_beijing_time()
    return WebHook(
        id=generate_webhook_id(),
        url=url,
        secret=secret,
        types=",".join(types),
        events=",".join(events),
        description=description,
        status="active",
        failure_count=0,
        created_at=current_time,
        updated_at=current_time,
    )


def _apply_webhook_updates(
    webhook: WebHook,
    *,
    url: str | None,
    secret: str | None,
    types: list[str] | None,
    events: list[str] | None,
    description: str | None,
) -> None:
    if url is not None:
        webhook.url = url
    if secret is not None:
        webhook.secret = secret
    if types is not None:
        webhook.types = ",".join(types)
    if events is not None:
        webhook.events = ",".join(events)
    if description is not None:
        webhook.description = description

    webhook.updated_at = get_beijing_time()


def _reset_webhook_failure_state(webhook: WebHook) -> None:
    webhook.status = "active"
    webhook.failure_count = 0
    webhook.next_retry_at = None
    webhook.last_failure_reason = None
    webhook.updated_at = get_beijing_time()


def create_webhook(
    db: Session,
    url: str,
    secret: str,
    types: list[str],
    events: list[str],
    description: str | None = None,
) -> WebHook:
    """
    创建WebHook

    Args:
        db: 数据库会话
        url: 回调URL
        secret: 签名密钥
        types: 关注的数据类型列表
        events: 关注的事件类型列表
        description: WebHook描述

    Returns:
        创建的WebHook对象
    """
    try:
        webhook = _build_webhook_model(
            url=url,
            secret=secret,
            types=types,
            events=events,
            description=description,
        )

        db.add(webhook)
        db.flush()

        return webhook

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_CREATE_FAILED,
            error_msg=f"Failed to create webhook: {e!s}",
            input_params={
                "url": url,
                "types": types,
                "events": events,
            },
        ) from None


def get_webhook(db: Session, webhook_id: str) -> WebHook:
    """
    获取WebHook信息

    Args:
        db: 数据库会话
        webhook_id: WebHook ID

    Returns:
        WebHook对象
    """
    try:
        webhook = db.query(WebHook).filter(webhook_id == WEBHOOK_ID_COL).first()

        if not webhook:
            raise SyncError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_name=SyncErrorCode.WEBHOOK_NOT_FOUND,
                error_msg=f"WebHook {webhook_id} not found",
                input_params={"webhook_id": webhook_id},
            )

        return webhook

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_QUERY_FAILED,
            error_msg=f"Failed to get webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


def update_webhook(
    db: Session,
    webhook_id: str,
    url: str | None = None,
    secret: str | None = None,
    types: list[str] | None = None,
    events: list[str] | None = None,
    description: str | None = None,
) -> WebHook:
    """
    更新WebHook

    Args:
        db: 数据库会话
        webhook_id: WebHook ID
        url: 新的回调URL
        secret: 新的签名密钥
        types: 新的数据类型列表
        events: 新的事件类型列表
        description: 新的描述

    Returns:
        更新后的WebHook对象
    """
    try:
        webhook = get_webhook(db, webhook_id)
        _apply_webhook_updates(
            webhook,
            url=url,
            secret=secret,
            types=types,
            events=events,
            description=description,
        )

        db.add(webhook)
        db.flush()

        return webhook

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_UPDATE_FAILED,
            error_msg=f"Failed to update webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


def delete_webhook(db: Session, webhook_id: str) -> bool:
    """
    删除WebHook

    Args:
        db: 数据库会话
        webhook_id: WebHook ID

    Returns:
        是否删除成功
    """
    try:
        webhook = get_webhook(db, webhook_id)
        db.delete(webhook)
        db.flush()
        return True

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_DELETE_FAILED,
            error_msg=f"Failed to delete webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


def reactivate_webhook(db: Session, webhook_id: str) -> WebHook:
    """
    重新激活WebHook，重置失败状态

    Args:
        db: 数据库会话
        webhook_id: WebHook ID

    Returns:
        重新激活的WebHook对象
    """
    try:
        webhook = get_webhook(db, webhook_id)
        _reset_webhook_failure_state(webhook)

        db.add(webhook)
        db.flush()

        return webhook

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_REACTIVATE_FAILED,
            error_msg=f"Failed to reactivate webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


def get_webhook_list(
    db: Session,
    page_num: int = 1,
    page_size: int = 10,
    status_filter: str | None = None,
) -> tuple[list[WebHook], int]:
    """
    获取WebHook列表

    Args:
        db: 数据库会话
        page_num: 页码
        page_size: 每页数量
        status_filter: 状态过滤

    Returns:
        (WebHook列表, 总数)
    """
    try:
        query = db.query(WebHook)

        if status_filter:
            query = query.filter(status_filter == WEBHOOK_STATUS_COL)

        # 获取总数
        total = query.count()

        # 计算分页偏移量
        skip = (page_num - 1) * page_size

        # 应用分页和排序
        webhooks = query.order_by(WEBHOOK_CREATED_AT_COL.desc()).offset(skip).limit(page_size).all()

        return webhooks, total

    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_QUERY_FAILED,
            error_msg=f"Failed to list webhooks: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "status_filter": status_filter,
            },
        ) from None


async def create_webhook_async(
    session: AsyncSession,
    url: str,
    secret: str,
    types: list[str],
    events: list[str],
    description: str | None = None,
) -> WebHook:
    """创建 WebHook（异步请求路径）。"""
    try:
        webhook = _build_webhook_model(
            url=url,
            secret=secret,
            types=types,
            events=events,
            description=description,
        )

        session.add(webhook)
        await session.flush()
        return webhook
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_CREATE_FAILED,
            error_msg=f"Failed to create webhook: {e!s}",
            input_params={
                "url": url,
                "types": types,
                "events": events,
            },
        ) from None


async def get_webhook_async(session: AsyncSession, webhook_id: str) -> WebHook:
    """获取 WebHook（异步请求路径）。"""
    try:
        result = await session.execute(select(WebHook).where(webhook_id == WEBHOOK_ID_COL).limit(1))
        webhook = result.scalar_one_or_none()
        if not webhook:
            raise SyncError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_name=SyncErrorCode.WEBHOOK_NOT_FOUND,
                error_msg=f"WebHook {webhook_id} not found",
                input_params={"webhook_id": webhook_id},
            )
        return webhook
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_QUERY_FAILED,
            error_msg=f"Failed to get webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


async def update_webhook_async(
    session: AsyncSession,
    webhook_id: str,
    url: str | None = None,
    secret: str | None = None,
    types: list[str] | None = None,
    events: list[str] | None = None,
    description: str | None = None,
) -> WebHook:
    """更新 WebHook（异步请求路径）。"""
    try:
        webhook = await get_webhook_async(session, webhook_id)
        _apply_webhook_updates(
            webhook,
            url=url,
            secret=secret,
            types=types,
            events=events,
            description=description,
        )
        session.add(webhook)
        await session.flush()
        return webhook
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_UPDATE_FAILED,
            error_msg=f"Failed to update webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


async def delete_webhook_async(session: AsyncSession, webhook_id: str) -> bool:
    """删除 WebHook（异步请求路径）。"""
    try:
        webhook = await get_webhook_async(session, webhook_id)
        await session.delete(webhook)
        await session.flush()
        return True
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_DELETE_FAILED,
            error_msg=f"Failed to delete webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


async def reactivate_webhook_async(session: AsyncSession, webhook_id: str) -> WebHook:
    """重新激活 WebHook（异步请求路径）。"""
    try:
        webhook = await get_webhook_async(session, webhook_id)
        _reset_webhook_failure_state(webhook)
        session.add(webhook)
        await session.flush()
        return webhook
    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_REACTIVATE_FAILED,
            error_msg=f"Failed to reactivate webhook: {e!s}",
            input_params={"webhook_id": webhook_id},
        ) from None


async def get_webhook_list_async(
    session: AsyncSession,
    page_num: int = 1,
    page_size: int = 10,
    status_filter: str | None = None,
) -> tuple[list[WebHook], int]:
    """获取 WebHook 列表（异步请求路径）。"""
    try:
        stmt = select(WebHook)
        count_stmt = select(func.count()).select_from(WebHook)
        if status_filter:
            stmt = stmt.where(status_filter == WEBHOOK_STATUS_COL)
            count_stmt = count_stmt.where(status_filter == WEBHOOK_STATUS_COL)

        skip = (page_num - 1) * page_size
        stmt = stmt.order_by(WEBHOOK_CREATED_AT_COL.desc()).offset(skip).limit(page_size)

        result = await session.execute(stmt)
        count_result = await session.execute(count_stmt)
        return list(result.scalars().all()), int(count_result.scalar_one())
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.WEBHOOK_QUERY_FAILED,
            error_msg=f"Failed to list webhooks: {e!s}",
            input_params={
                "page_num": page_num,
                "page_size": page_size,
                "status_filter": status_filter,
            },
        ) from None


def generate_webhook_signature(secret: str, timestamp: int, payload: str) -> str:
    """
    生成WebHook签名

    Args:
        secret: 签名密钥
        timestamp: 时间戳
        payload: 负载数据

    Returns:
        HMAC-SHA256签名
    """
    message = f"{timestamp}.{payload}"
    signature = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={signature}"


def send_webhook_notification(
    webhook: WebHook,
    event: str,
    event_data: dict[str, Any],
    timeout: int = 30,
    max_retries: int = 3,
) -> bool:
    """
    发送WebHook通知

    Args:
        webhook: WebHook对象
        event: 事件类型
        event_data: 事件数据
        timeout: 请求超时时间（秒）
        max_retries: 最大重试次数

    Returns:
        是否发送成功
    """
    try:
        current_time = get_beijing_time()
        timestamp = int(current_time.timestamp())

        # 构建通知载荷
        notification_payload = {
            "webhook_id": webhook.id,
            "event": event,
            "timestamp": current_time.isoformat(),
            "data": event_data,
        }

        payload_json = json.dumps(notification_payload, ensure_ascii=False)

        # 生成签名
        signature = generate_webhook_signature(webhook.secret, timestamp, payload_json)

        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-ID": webhook.id,
            "X-Webhook-Signature": signature,
            "X-Webhook-Timestamp": str(timestamp),
            "User-Agent": "ACPs-DSP-WebHook/1.0",
        }

        # 如果该webhook-event已在发送中，则跳过本次发送
        if not _mark_inflight(webhook.id, event):
            logger.info("跳过重复的进行中 WebHook 发送", webhook_id=webhook.id, event_name=event)
            return True

        try:
            # 发送请求
            response = httpx.post(
                webhook.url,
                content=payload_json.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
        finally:
            _clear_inflight(webhook.id, event)

        # 检查响应状态
        if 200 <= response.status_code < 300:
            return True
        logger.warning("WebHook 返回非 2xx 状态码", webhook_id=webhook.id, status_code=response.status_code)
        return False

    except httpx.HTTPError as e:
        logger.error("发送 WebHook 通知失败", webhook_id=webhook.id, error=str(e))
        return False
    except Exception as e:
        logger.error("发送 WebHook 时发生未预期错误", webhook_id=webhook.id, error=str(e))
        return False


def update_webhook_status(
    webhook_id: str,
    success: bool,
    failure_reason: str | None = None,
) -> None:
    """
    更新WebHook状态

    Args:
        webhook_id: WebHook ID
        success: 是否成功
        failure_reason: 失败原因
    """
    try:
        with get_sync_session() as status_db:
            webhook = status_db.query(WebHook).filter(webhook_id == WEBHOOK_ID_COL).first()

            if not webhook:
                return

            current_time = get_beijing_time()
            webhook.last_triggered_at = current_time

            if success:
                webhook.last_success_at = current_time
                webhook.failure_count = 0
                webhook.next_retry_at = None
                webhook.last_failure_reason = None
                if webhook.status == "failed":
                    webhook.status = "active"
            else:
                webhook.last_failure_at = current_time
                webhook.failure_count += 1
                webhook.last_failure_reason = failure_reason

                # 计算下次重试时间（指数退避）
                base_interval = 5  # 基础间隔5秒
                max_interval = 3600  # 最大间隔1小时
                retry_interval = min(base_interval * (2**webhook.failure_count), max_interval)
                webhook.next_retry_at = current_time + timedelta(seconds=retry_interval)

                # 如果失败次数超过阈值，标记为失败状态
                if webhook.failure_count >= 10:  # 最大重试10次
                    webhook.status = "failed"

            webhook.updated_at = current_time

            status_db.add(webhook)
    except SQLAlchemyError as e:
        logger.error("更新 WebHook 状态失败", webhook_id=webhook_id, error=str(e))


def trigger_webhooks(
    event: str,
    event_data: dict[str, Any],
    data_types: list[str] | None = None,
) -> None:
    """
    触发相关的WebHooks

    Args:
        event: 事件类型
        event_data: 事件数据
        data_types: 相关的数据类型列表
    """
    try:
        with get_sync_session() as webhook_db:
            # 查找匹配的活跃WebHooks
            query = webhook_db.query(WebHook).filter(
                WEBHOOK_STATUS_COL == "active",
                WEBHOOK_EVENTS_COL.contains(event),  # 检查事件类型匹配
            )

            # 如果指定了数据类型，则进一步过滤
            if data_types:
                # 使用OR条件匹配任何一个数据类型
                type_conditions = []
                for data_type in data_types:
                    type_conditions.append(WEBHOOK_TYPES_COL.contains(data_type))
                if type_conditions:
                    if len(type_conditions) == 1:  # 避免只有一个数据类型时产生不合法的查询语句
                        query = query.filter(type_conditions[0])
                    else:
                        query = query.filter(or_(*type_conditions))

            webhooks = query.all()
            with contextlib.suppress(Exception):
                webhook_db.expunge_all()

        if not webhooks:
            logger.info("未找到事件对应的活跃 WebHook", event_name=event)
            return

        # 异步发送WebHook通知
        def send_notification(webhook: WebHook) -> None:
            try:
                success = send_webhook_notification(webhook, event, event_data)
                update_webhook_status(
                    webhook.id,
                    success,
                    None if success else "HTTP request failed",
                )
            except Exception as e:
                update_webhook_status(
                    webhook.id,
                    False,
                    f"Exception: {e!s}",
                )

        # 使用线程池异步发送通知
        with ThreadPoolExecutor(max_workers=5) as executor:
            for webhook in webhooks:
                executor.submit(send_notification, webhook)

        logger.info("已触发 WebHook", event_name=event, webhook_count=len(webhooks))

    except Exception as e:
        logger.error("触发 WebHook 失败", event_name=event, error=str(e))


def trigger_data_change_webhook(db: Session, data_types: list[str]) -> None:
    """
    通知发现服务器指定数据类型发生变化(data_change)
    并在短时间窗口内合并为批量通知(data_change)

    Args:
        db: 数据库会话
        data_types: 数据类型列表
    """
    try:
        batch_window = settings.dsp_webhook_batch_window_seconds
        if batch_window and batch_window > 0:
            # 启用批处理：不发送即时data_change，只合并到窗口后统一发送一次
            current_seq = get_current_max_seq(db)
            with _data_change_batch_lock:
                for data_type in data_types:
                    _data_change_batch_state.types.add(data_type)
                # 记录窗口时间内的最大seq
                if _data_change_batch_state.max_seq is None:
                    _data_change_batch_state.max_seq = current_seq
                else:
                    _data_change_batch_state.max_seq = max(_data_change_batch_state.max_seq, current_seq)

                # 重置定时器，静默期后执行批量发送
                timer = _data_change_batch_state.timer
                if timer is not None:
                    timer.cancel()

                def _send_batch() -> None:
                    types_list: list[str]
                    max_seq_val: int
                    with _data_change_batch_lock:
                        types_list = list(_data_change_batch_state.types) or ["acs"]
                        max_seq_val = (
                            int(_data_change_batch_state.max_seq) if _data_change_batch_state.max_seq is not None else 0
                        )

                        # 清空批处理状态
                        _data_change_batch_state.types.clear()
                        _data_change_batch_state.max_seq = None
                        _data_change_batch_state.timer = None

                    try:
                        # 为每个数据类型发送单独的data_change事件
                        for data_type in types_list:
                            event_data = {
                                "type": data_type,
                                "current_seq": max_seq_val,
                            }
                            trigger_webhooks(
                                event="data_change",
                                event_data=event_data,
                                data_types=[data_type],
                            )
                    except Exception:
                        logger.exception("发送 data_change WebHook 时出错")

                new_timer = Timer(batch_window, _send_batch)
                _data_change_batch_state.timer = new_timer
                new_timer.daemon = True
                new_timer.start()
        else:
            # 未启用批处理：按类型即时发送 data_change
            current_seq = get_current_max_seq(db)
            for data_type in data_types:
                event_data = {"type": data_type, "current_seq": current_seq}
                trigger_webhooks(
                    event="data_change",
                    event_data=event_data,
                    data_types=[data_type],
                )

    except Exception:
        logger.exception("触发 data_change WebHook 时出错")


def trigger_retention_cleanup_webhook(db: Session, cleaned_count: int, window_hours: int, max_records: int) -> None:
    """
    在数据保留策略清理时触发WebHook

    Args:
        db: 数据库会话
        cleaned_count: 清理的记录数量
        window_hours: 保留窗口时长（小时）
        max_records: 保留的最大记录数
    """
    try:
        # 只有在实际清理了数据时才触发webhook
        if cleaned_count > 0:
            current_seq = get_current_max_seq(db)
            oldest_seq = get_retention_oldest_seq(db, window_hours, max_records)

            event_data = {
                "type": "acs",
                "cleaned_count": cleaned_count,
                "window_hours": window_hours,
                "max_records": max_records,
                "current_seq": current_seq,
                "oldest_seq": oldest_seq,
                "cleanup_timestamp": get_beijing_time().isoformat(),
            }

            trigger_webhooks(
                event="retention_cleanup",
                event_data=event_data,
                data_types=["acs"],
            )

            logger.info("已触发 retention_cleanup WebHook", cleaned_count=cleaned_count)
    except Exception:
        logger.exception("触发 retention_cleanup WebHook 时出错")
