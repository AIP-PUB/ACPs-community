"""app/system/store.py — OpenSearch 读写执行 + 模板/ISM bootstrap + ISM 补挂（唯一直接调 opensearch_client 的文件）。

DSL 在 dsl.py（纯函数）；执行在此。事件真相源唯一 amp-system-events-*（C-SYSTEM-WRITE-1/QUERY-1）。
所有 OpenSearch API 调用（含 maintenance 所需的 ISM Add Policy）均在此文件，maintenance 通过调用本文件函数执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from opensearchpy import ConflictError, NotFoundError
from opensearchpy import ConnectionError as OSConnectionError

from app.core.opensearch_client import get_opensearch_client
from app.system import indices
from app.system.exception import OpenSearchBulkError, OpenSearchQueryError
from app.system.metrics import (
    AMP_SYSTEM_PIT_EXPIRED_TOTAL,
    AMP_SYSTEM_PIT_OPEN_TOTAL,
    metrics,
)
from app.system.schema import SystemEventView

if TYPE_CHECKING:
    from app.system.normalizer import SystemEventDoc

logger = structlog.get_logger(__name__)

# ── bootstrap ──────────────────────────────────────────────────────────────────


async def ensure_system_schema(
    *,
    number_of_shards: int,
    number_of_replicas: int,
    hot_days: int,
    warm_days: int,
    archive_days: int,
) -> None:
    """幂等 bootstrap（runtime 启动调用，设计 §3.3/§4）。

    1. PUT _index_template/amp-system-events-template
    2. PUT _plugins/_ism/policies/amp-system-events-ism
    失败抛异常（runtime 视为启动失败）。
    ISM 靠 ism_template 自动挂载新索引，不在模板 settings 写废弃的 policy_id。
    """
    client = await get_opensearch_client()
    template_body = indices.build_index_template(
        number_of_shards=number_of_shards,
        number_of_replicas=number_of_replicas,
    )
    await client.indices.put_index_template(
        name=indices.INDEX_TEMPLATE_NAME,
        body=template_body,
    )
    logger.info("OpenSearch index template upserted", name=indices.INDEX_TEMPLATE_NAME)

    ism_body = indices.build_ism_policy(
        hot_days=hot_days,
        warm_days=warm_days,
        archive_days=archive_days,
    )
    try:
        await client.plugins.index_management.put_policy(
            indices.ISM_POLICY_NAME,
            body=ism_body,
        )
    except ConflictError:
        # 幂等 bootstrap：策略已存在则视为成功（runtime 重启 / 多测试复用同一 OpenSearch）
        logger.info("OpenSearch ISM policy already exists", name=indices.ISM_POLICY_NAME)
    logger.info("OpenSearch ISM policy upserted", name=indices.ISM_POLICY_NAME)


async def ensure_ism_attached() -> None:
    """ISM 存量补挂（maintenance 模块调用，设计 §3.3）。

    POST _plugins/_ism/add/amp-system-events-*  body {"policy_id": "amp-system-events-ism"}
    新索引由 ism_template 自动挂载；此处只补历史索引。已挂载幂等跳过（OpenSearch 返回 200）。
    失败只告警，不影响 Writer 正常写入（设计 §3.3 备注）。
    """
    client = await get_opensearch_client()
    try:
        await client.plugins.index_management.add_policy(
            index=indices.INDEX_PATTERN,
            body={"policy_id": indices.ISM_POLICY_NAME},
        )
        logger.info("ISM add_policy completed (idempotent)", index=indices.INDEX_PATTERN)
    except Exception:
        logger.warning("ISM add_policy failed (non-fatal)", exc_info=True)


# ── Bulk 写侧（_id=log_id upsert，C-SYSTEM-WRITE-6）────────────────────────────


@dataclass(frozen=True)
class BulkResult:
    """Bulk Index 执行结果。"""

    indexed: int
    failed_items: list[tuple[str, str]]  # [(log_id, error_reason)]


_TRANSIENT_STATUS_CODES = frozenset({429, 503, 507})


async def bulk_index(
    docs: list[SystemEventDoc],
    *,
    indexed_at_iso: str,
) -> BulkResult:
    """组装 bulk body → client.bulk → 解析响应。

    全 transient 失败 → raise OpenSearchBulkError（writer 整批重试）；
    permanent 失败项 → failed_items（writer 逐项投 DLQ）；其余成功计入 indexed。
    """
    if not docs:
        return BulkResult(indexed=0, failed_items=[])

    client = await get_opensearch_client()
    bulk_body: list[dict[str, Any]] = []
    for doc in docs:
        action_meta, source = doc.as_bulk_action(indexed_at_iso=indexed_at_iso)
        bulk_body.append(action_meta)
        bulk_body.append(source)

    response = await client.bulk(body=bulk_body, refresh=False)

    if not response.get("errors", False):
        indexed = sum(1 for item in response.get("items", []) if "index" in item)
        return BulkResult(indexed=indexed, failed_items=[])

    indexed = 0
    failed_items: list[tuple[str, str]] = []
    transient_count = 0
    permanent_count = 0

    for item in response.get("items", []):
        op_result = item.get("index", {})
        status = op_result.get("status", 200)
        error = op_result.get("error")
        log_id = op_result.get("_id", "unknown")

        if error is None:
            indexed += 1
        elif status in _TRANSIENT_STATUS_CODES:
            transient_count += 1
            reason = error.get("reason", str(status))
            logger.warning("Bulk transient failure", log_id=log_id, status=status, reason=reason)
        else:
            permanent_count += 1
            reason = error.get("reason", f"status={status}")
            failed_items.append((log_id, reason))
            logger.warning("Bulk permanent failure", log_id=log_id, status=status, reason=reason)

    if transient_count > 0 and indexed == 0 and permanent_count == 0:
        raise OpenSearchBulkError(f"All {transient_count} bulk items failed transiently.")

    return BulkResult(indexed=indexed, failed_items=failed_items)


# ── PIT 读侧（C-SYSTEM-QUERY-5）────────────────────────────────────────────────


@dataclass(frozen=True)
class SystemEventHit:
    """search_events 内部返回结果：携带视图与 OpenSearch 原始 sort 值。

    sort_values 来自 hit["sort"]，与 dsl.build_sort 追加的排序键一一对应（[timestamp_ms, log_id]）。
    service 层用 [h.view for h in rows] 组装输出，用 extract_search_after(rows[-1]) 编游标。
    """

    view: SystemEventView
    sort_values: list[Any]


async def open_pit(*, keep_alive: str) -> str:
    """client.create_pit → 返回 pit_id（吸收 opensearch-py 版本方法名差异）。"""
    client = await get_opensearch_client()
    response = await client.create_pit(
        index=indices.query_index_target(),
        params={"keep_alive": keep_alive},
    )
    pit_id: str = response["pit_id"]
    metrics.inc(AMP_SYSTEM_PIT_OPEN_TOTAL)
    logger.debug("PIT opened", pit_id=pit_id[:16] + "...", keep_alive=keep_alive)
    return pit_id


async def close_pit(pit_id: str) -> None:
    """client.delete_pit；异常只告警（PIT 会自动过期）。"""
    client = await get_opensearch_client()
    try:
        await client.delete_pit(body={"pit_id": [pit_id]})
        logger.debug("PIT closed", pit_id=pit_id[:16] + "...")
    except Exception:
        logger.warning("PIT close failed (non-fatal, will expire naturally)", exc_info=True)


async def search_events(
    search_body: dict[str, Any],
    *,
    pit_id: str,
    keep_alive: str,
    include_raw_log: bool = False,
) -> list[SystemEventHit]:
    """注入 PIT → client.search → 结果映射。

    PIT 过期/无效 → raise OpenSearchQueryError("pit")（service 转 CursorInvalidError, C-SYSTEM-QUERY-5）。
    超时/连接失败 → raise OpenSearchQueryError（service 转 ReadModelLaggingError(503)）。
    """
    client = await get_opensearch_client()
    body = {**search_body, "pit": {"id": pit_id, "keep_alive": keep_alive}}
    try:
        response = await client.search(body=body)
    except NotFoundError as exc:
        metrics.inc(AMP_SYSTEM_PIT_EXPIRED_TOTAL)
        raise OpenSearchQueryError(f"pit expired or not found: {exc}") from exc
    except (OSConnectionError, TimeoutError, Exception) as exc:
        if "pit" in str(exc).lower() or "not_found" in str(exc).lower():
            metrics.inc(AMP_SYSTEM_PIT_EXPIRED_TOTAL)
            raise OpenSearchQueryError(f"pit error: {exc}") from exc
        raise OpenSearchQueryError(f"search failed: {exc}") from exc

    hits = response.get("hits", {}).get("hits", [])
    return [
        SystemEventHit(
            view=_hit_to_view(h, include_raw_log=include_raw_log),
            sort_values=h.get("sort", []),
        )
        for h in hits
    ]


def _hit_to_view(hit: dict[str, Any], *, include_raw_log: bool) -> SystemEventView:
    """_source（indices.EVENT_SOURCE_FIELDS）→ SystemEventView。

    search_text/indexed_at 不出参（内部字段，C-SYSTEM-QUERY 边界）；
    raw_body 仅 include_raw_log 时填（§5.3 第6条）；
    severity_number 缺省补 0；message 缺省补 ""（防御，正常恒有）。
    """
    src = hit.get("_source", {})
    return SystemEventView(
        log_id=src.get("log_id", ""),
        timestamp=src.get("timestamp", ""),
        aic=src.get("aic", ""),
        severity_number=src.get("severity_number", 0) or 0,
        severity_text=src.get("severity_text"),
        trace_id=src.get("trace_id"),
        correlation_id=src.get("correlation_id"),
        message=src.get("message", ""),
        category=src.get("category"),
        component=src.get("component"),
        module=src.get("module"),
        tags=src.get("tags") or None,
        raw_body=src.get("raw_body") if include_raw_log else None,
    )


def extract_search_after(last_hit: SystemEventHit) -> list[Any]:
    """从最后一条 SystemEventHit 取 sort_values（[timestamp_ms, log_id]），service 编游标用。"""
    return list(last_hit.sort_values)
