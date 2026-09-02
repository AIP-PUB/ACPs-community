import asyncio
from collections.abc import Iterator
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.config import settings
from app.core.db_session import get_session
from app.sync.exception import SyncError, SyncErrorCode
from app.sync.schema import Envelope, InfoResponse
from app.sync.service import (
    create_snapshot_async,
    delete_snapshot_async,
    get_changes_async,
    get_current_max_seq_async,
    get_retention_oldest_seq_async,
    get_snapshot_chunk_async,
)

router_protocol = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]
NDJSON_MEDIA_TYPE = "application/x-ndjson"
POLL_INTERVAL_SECONDS = 1


class SnapshotHeaderLike(Protocol):
    id: str
    seq: int
    chunk_total: int
    object_count: int


def _problem_response(description: str) -> dict[str, object]:
    return {"description": description, "content": {PROBLEM_JSON_MEDIA_TYPE: {}}}


BAD_REQUEST_RESPONSE = _problem_response("Invalid sync request")
NOT_FOUND_RESPONSE = _problem_response("Sync resource not found")
GONE_RESPONSE = _problem_response("Sync resource is no longer available")
SERVER_ERROR_RESPONSE = _problem_response("Sync processing failed")


def _generate_ndjson(items: list[Envelope]) -> Iterator[str]:
    for envelope in items:
        yield envelope.model_dump_json(exclude_none=True) + "\n"


def _normalize_changes_limit(limit: int) -> int:
    if limit > settings.dsp_changes_max_limit:
        return settings.dsp_changes_max_limit
    if limit <= 0:
        return settings.dsp_changes_default_limit
    return limit


def _parse_optional_types(types: str | None) -> list[str] | None:
    if not types:
        return None
    return [item.strip() for item in types.split(",")]


def _parse_wait_seconds(wait: str | None) -> int:
    if not wait:
        return 0

    try:
        return max(int(float(wait)), 0)
    except TypeError, ValueError:
        return 0


def _build_changes_response(response: Response, envelopes: list[Envelope], next_seq: int) -> StreamingResponse | str:
    response.headers["X-Next-Seq"] = str(next_seq)
    response.headers["Content-Type"] = NDJSON_MEDIA_TYPE
    if not envelopes:
        response.status_code = status.HTTP_204_NO_CONTENT
        return ""

    return StreamingResponse(
        _generate_ndjson(envelopes),
        media_type=NDJSON_MEDIA_TYPE,
        headers={"X-Next-Seq": str(next_seq)},
    )


def _set_snapshot_headers(response: Response, snapshot: SnapshotHeaderLike, chunk_index: str) -> None:
    response.headers["X-Snapshot-Id"] = snapshot.id
    response.headers["X-Snapshot-Seq"] = str(snapshot.seq)
    response.headers["X-Snapshot-Chunk-Index"] = chunk_index
    response.headers["X-Snapshot-Chunk-Total"] = str(snapshot.chunk_total)
    response.headers["X-Snapshot-Object-Count"] = str(snapshot.object_count)
    response.headers["Content-Type"] = NDJSON_MEDIA_TYPE


@router_protocol.get(
    "/info",
    status_code=status.HTTP_200_OK,
    summary="获取 DSP 服务信息",
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: SERVER_ERROR_RESPONSE,
    },
)
async def get_info(db: DbSession) -> InfoResponse:
    """获取系统信息和配置"""
    try:
        max_seq = await get_current_max_seq_async(db)
        oldest_seq = await get_retention_oldest_seq_async(
            db, settings.dsp_retention_window_hours, settings.dsp_retention_max_records
        )

        return InfoResponse(
            service=settings.project_name,
            version=settings.project_version,
            status="healthy",
            supported_types=["acs"],
            retention={
                "window_hours": settings.dsp_retention_window_hours,
                "oldest_seq": oldest_seq,
                "newest_seq": max_seq,
            },
            snapshot={
                "access_timeout_hours": settings.dsp_snapshot_access_timeout_hours,
                "max_lifetime_hours": settings.dsp_snapshot_max_lifetime_hours,
                "supports_incremental": True,
                "supports_chunking": True,
            },
            changes={
                "supports_long_polling": False,
                "payload_type": "FULL_OBJ",
            },
        )

    except SyncError:
        raise
    except SQLAlchemyError as e:
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.CHANGES_QUERY_FAILED,
            error_msg=f"Failed to get system info: {e!s}",
            input_params={},
        ) from None


@router_protocol.get(
    "/changes",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="按序列获取增量变更",
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: SERVER_ERROR_RESPONSE,
    },
)
async def get_changes_api(
    response: Response,
    db: DbSession,
    types: Annotated[str | None, Query(description="数据类型，逗号分隔")] = None,
    seq: Annotated[int | None, Query(description="起始序列号")] = None,
    limit: Annotated[int, Query(description="返回条数限制")] = settings.dsp_changes_default_limit,
    wait: Annotated[str | None, Query(description="长轮询等待时间（秒）")] = None,
) -> StreamingResponse | str:
    """获取增量变更数据，支持长轮询"""
    try:
        limit = _normalize_changes_limit(limit)
        type_list = _parse_optional_types(types)
        wait_seconds = _parse_wait_seconds(wait)
        waited = 0

        while True:
            envelopes, next_seq = await get_changes_async(db, seq=seq, limit=limit, types=type_list)

            if envelopes or wait_seconds == 0:
                return _build_changes_response(response, envelopes, next_seq)

            if waited >= wait_seconds:
                response.headers["X-Next-Seq"] = str(next_seq)
                response.status_code = status.HTTP_204_NO_CONTENT
                return ""
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            waited += POLL_INTERVAL_SECONDS

    except SyncError:
        raise


@router_protocol.get(
    "/snapshots",
    status_code=status.HTTP_200_OK,
    summary="创建快照或读取快照分块",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_500_INTERNAL_SERVER_ERROR: SERVER_ERROR_RESPONSE,
    },
)
async def get_snapshot_api(
    response: Response,
    db: DbSession,
    types: Annotated[str | None, Query(description="数据类型，逗号分隔")] = None,
    limit: Annotated[int, Query(description="每块最大对象数量")] = 10000,
    from_seq: Annotated[int | None, Query(description="增量快照的起始序号")] = None,
    id: Annotated[str | None, Query(description="快照ID，用于获取后续块")] = None,  # noqa: A002
    chunk: Annotated[int | None, Query(description="块索引")] = None,
) -> StreamingResponse:
    """创建快照或获取快照数据"""
    try:
        if id and chunk is None:
            raise SyncError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                error_msg="chunk parameter is required when id is provided",
                input_params={"id": id, "chunk": chunk},
            )

        if not id and not types:
            raise SyncError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                error_msg="types parameter is required when creating a new snapshot",
                input_params={"types": types},
            )

        if id:
            if chunk is None:
                raise SyncError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                    error_msg="chunk parameter is required when id is provided",
                    input_params={"id": id, "chunk": chunk},
                )
            snapshot, envelopes = await get_snapshot_chunk_async(db, snapshot_id=id, chunk_index=chunk, limit=limit)
            await db.commit()
            _set_snapshot_headers(response, snapshot, str(chunk))
        else:
            if types is None:
                raise SyncError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                    error_msg="types parameter is required when creating a new snapshot",
                    input_params={"types": types},
                )
            type_list = [item.strip() for item in types.split(",") if item.strip()]
            if not type_list:
                raise SyncError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                    error_msg="At least one data type must be specified",
                    input_params={"types": types},
                )

            supported_types = ["acs"]
            invalid_types = [item for item in type_list if item not in supported_types]
            if invalid_types:
                raise SyncError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_name=SyncErrorCode.INVALID_SNAPSHOT_PARAMS,
                    error_msg=f"Unsupported data types: {invalid_types}. Supported types: {supported_types}",
                    input_params={"types": types, "invalid_types": invalid_types},
                )

            snapshot, envelopes = await create_snapshot_async(db, types=type_list, limit=limit, from_seq=from_seq)
            await db.commit()
            _set_snapshot_headers(response, snapshot, "0")

        return StreamingResponse(
            _generate_ndjson(envelopes),
            media_type=NDJSON_MEDIA_TYPE,
            headers={
                "X-Snapshot-Id": response.headers["X-Snapshot-Id"],
                "X-Snapshot-Seq": response.headers["X-Snapshot-Seq"],
                "X-Snapshot-Chunk-Index": response.headers["X-Snapshot-Chunk-Index"],
                "X-Snapshot-Chunk-Total": response.headers["X-Snapshot-Chunk-Total"],
                "X-Snapshot-Object-Count": response.headers["X-Snapshot-Object-Count"],
            },
        )

    except SyncError:
        raise


@router_protocol.delete(
    "/snapshots/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除指定快照",
    responses={
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_500_INTERNAL_SERVER_ERROR: SERVER_ERROR_RESPONSE,
    },
)
async def delete_snapshot_api(id: str, db: DbSession) -> Response:  # noqa: A002
    """删除指定的快照"""
    try:
        success = await delete_snapshot_async(db, snapshot_id=id)
        if success:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        raise SyncError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_name=SyncErrorCode.SNAPSHOT_TABLE_DROP_FAILED,
            error_msg=f"Failed to delete snapshot {id}",
            input_params={"id": id},
        )

    except SyncError:
        raise
