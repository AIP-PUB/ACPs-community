"""app/metrics/snapshot_service.py — snapshots/query（Redis 优先 + TSDB exact-anchor 修复）。

设计 §6.1。唯一允许命中 Redis 作真相源的端点（C-METRIC-QUERY-1）。
Hash 缺失/过期/校验失败时走 amp_snapshot_present exact-anchor 修复，
重建同一事件快照（不跨事件拼字段，§6.1 第 6 条）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Final

import structlog
from acps_sdk.amp.models import LoadMetrics, WindowMetrics
from redis.asyncio import Redis

from app.core.amp_api_schema import AMPResponseMeta, AMPSortSpec
from app.core.config import get_settings
from app.metrics import cursor as cursor_mod
from app.metrics import promql, tsdb
from app.metrics.exception import CursorInvalidError, UnsupportedFieldError
from app.metrics.filters import (
    CompiledFilter,
    LabelMatcher,
    NumericPostFilter,
    apply_numeric_post_filters,
    compile_snapshot_filter,
)
from app.metrics.freshness import apply_degrade_policy, build_meta, evaluate_freshness
from app.metrics.metrics import metrics as _metrics
from app.metrics.schema import MetricsSnapshotQueryRequest, MetricsSnapshotView
from app.metrics.service import iso_duration_to_promql_range, promql_timestamp_to_ms
from app.metrics.snapshot_cache import (
    CachedSnapshot,
    backfill_snapshot,
    mget_snapshots,
    remove_index_entry,
    scan_index_desc,
)

logger = structlog.get_logger(__name__)

ALL_WINDOWS: Final[tuple[()]] = ()  # 空集表示"不裁剪窗口"

# promql.build_snapshot_field_value_exprs 产出的 load 字段键（与 repair 解析一致）
_REPAIR_LOAD_FIELD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "active_tasks",
        "queued_tasks",
        "cpu_usage",
        "memory_usage",
        "disk_usage",
        "network_in_usage",
        "network_out_usage",
    }
)
_REPAIR_UPTIME_KEY: Final = "uptime_seconds"


# ── 排序校验 ─────────────────────────────────────────────────────────────────


def _ensure_snapshot_sort(sort: list[AMPSortSpec] | None) -> None:
    """仅支持默认 (observedAt desc, aic asc)；其它 → UnsupportedFieldError(422)。"""
    if not sort:
        return
    # 允许单条 observedAt desc 或 aic asc（或两者组合，顺序不限）
    allowed = {("observedAt", "desc"), ("aic", "asc")}
    for spec in sort:
        if (spec.field, spec.order) not in allowed:
            raise UnsupportedFieldError(
                f"snapshot sort field={spec.field!r} order={spec.order!r} is not supported. "
                "Only observedAt desc / aic asc are allowed."
            )


# ── 标签内存匹配 ──────────────────────────────────────────────────────────────


def _match_label_matchers(snap: CachedSnapshot, matchers: list[LabelMatcher]) -> bool:
    """内存复核 resource 标签过滤（aic 已在索引/精确集层处理，此处主要 service_* 复核）。"""
    import re

    label_values: dict[str, str] = {"aic": snap.aic}
    if snap.service_name:
        label_values["service_name"] = snap.service_name
    if snap.service_namespace:
        label_values["service_namespace"] = snap.service_namespace
    if snap.deployment_env:
        label_values["deployment_env"] = snap.deployment_env

    for m in matchers:
        val = label_values.get(m.label, "")
        if m.op == "=" and val != m.value:
            return False
        if m.op == "!=" and val == m.value:
            return False
        if m.op == "=~":
            pattern = m.value if isinstance(m.value, str) else "|".join(m.value)
            if not re.fullmatch(pattern, val):
                return False
        if m.op == "!~":
            pattern = m.value if isinstance(m.value, str) else "|".join(m.value)
            if re.fullmatch(pattern, val):
                return False
        if m.op == "in" and isinstance(m.value, list) and val not in m.value:
            return False
    return True


# ── 窗口裁剪 ─────────────────────────────────────────────────────────────────


def _trim_windows(
    window_metrics: list[WindowMetrics] | None,
    requested_windows: set[str] | None,
) -> list[WindowMetrics] | None:
    """按 requested_windows 裁剪 window_metrics；None 表示不裁剪。"""
    if window_metrics is None or requested_windows is None:
        return window_metrics
    trimmed = [wm for wm in window_metrics if wm.window in requested_windows]
    return trimmed or None


# ── snapshot 数值后置过滤 getter ──────────────────────────────────────────────


def _snap_value_getter(snap: MetricsSnapshotView, path: str) -> float | None:
    """从 MetricsSnapshotView 取 path 对应的 float 值。

    path 格式为 camelCase（如 "loadMetrics.activeTasks"），
    映射到 snake_case 模型字段名。
    """
    import re as _re

    def _camel_to_snake(s: str) -> str:
        return _re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()

    if path.startswith("loadMetrics.") and snap.load_metrics is not None:
        camel_field = path.removeprefix("loadMetrics.")
        snake_field = _camel_to_snake(camel_field)
        v = getattr(snap.load_metrics, snake_field, None)
        return float(v) if v is not None else None
    if path.startswith("windowMetrics.") and snap.window_metrics:
        camel_field = path.removeprefix("windowMetrics.")
        snake_field = _camel_to_snake(camel_field)
        values = [getattr(wm, snake_field, None) for wm in snap.window_metrics]
        valid = [float(v) for v in values if v is not None]
        return max(valid) if valid else None
    return None


# ── CachedSnapshot → MetricsSnapshotView ────────────────────────────────────


def _cached_to_view(
    snap: CachedSnapshot,
    requested_windows: set[str] | None,
) -> MetricsSnapshotView:
    """将 CachedSnapshot 转换为 MetricsSnapshotView，含窗口裁剪。"""
    observed_at_iso = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=UTC).isoformat()
    window_metrics = _trim_windows(snap.window_metrics, requested_windows)
    return MetricsSnapshotView(
        aic=snap.aic,
        observed_at=observed_at_iso,
        uptime_seconds=snap.uptime_seconds,
        load_metrics=snap.load_metrics,
        window_metrics=window_metrics,
    )


# ── Redis 收集页 ──────────────────────────────────────────────────────────────


async def _redis_collect_page(
    redis: Redis,
    *,
    static_aics: list[str] | None,
    cursor: cursor_mod.SnapshotCursor | None,
    limit: int,
    requested_windows: set[str] | None,
    post_filters: list[NumericPostFilter],
    label_matchers: list[LabelMatcher],
    cursor_fingerprint: str,
) -> tuple[list[MetricsSnapshotView], list[str], str | None]:
    """§6.1 第 3/4 步：从 Redis 收集分页数据。

    Returns:
        (hydrated, repair_aics, next_cursor_str)
    """
    hydrated: list[MetricsSnapshotView] = []
    repair_aics: list[str] = []
    next_cursor_str: str | None = None
    settings = get_settings()

    if static_aics is not None:
        # 有限 AIC 集：直读对应 Hash
        snaps = await mget_snapshots(redis, static_aics)
        last_snap: CachedSnapshot | None = None
        for aic, snap in zip(static_aics, snaps, strict=True):
            if snap is None:
                repair_aics.append(aic)
                await remove_index_entry(redis, aic)
                continue
            view = _cached_to_view(snap, requested_windows)
            if not _match_label_matchers(snap, label_matchers):
                continue
            filtered = apply_numeric_post_filters([view], post_filters, _snap_value_getter)
            if filtered:
                hydrated.append(filtered[0])
                last_snap = snap
        # 静态集不分页，next_cursor 始终为空
        _ = last_snap
    else:
        # ZSet 遍历
        batch_size = settings.metrics_snapshot_index_scan_batch_size
        scan_cursor = cursor
        while len(hydrated) < limit:
            batch = await scan_index_desc(redis, cursor=scan_cursor, batch_size=batch_size)
            if not batch:
                break
            aics_batch = [aic for aic, _ in batch]
            snaps = await mget_snapshots(redis, aics_batch)
            for (aic, _score_ms), snap in zip(batch, snaps, strict=True):
                if snap is None:
                    repair_aics.append(aic)
                    await remove_index_entry(redis, aic)
                    continue
                view = _cached_to_view(snap, requested_windows)
                if not _match_label_matchers(snap, label_matchers):
                    continue
                filtered = apply_numeric_post_filters([view], post_filters, _snap_value_getter)
                if not filtered:
                    continue
                hydrated.append(filtered[0])
                if len(hydrated) >= limit:
                    break
            # 更新 scan_cursor 为本批末项
            if batch:
                last_aic, last_score = batch[-1]
                scan_cursor = cursor_mod.SnapshotCursor(
                    observed_at_ms=last_score,
                    aic=last_aic,
                    fingerprint="",  # 仅用于扫描位置，不校验
                )
            # 少于 batch_size → 已到索引末
            if len(batch) < batch_size:
                break

        # next_cursor 以本页末项编码
        if hydrated and len(hydrated) >= limit:
            last_view = hydrated[-1]
            last_ts_ms = int(datetime.fromisoformat(last_view.observed_at).timestamp() * 1000)
            fp = cursor_fingerprint
            next_cursor_str = cursor_mod.encode_cursor(
                cursor_mod.SnapshotCursor(
                    observed_at_ms=last_ts_ms,
                    aic=last_view.aic,
                    fingerprint=fp,
                )
            )

    return hydrated, repair_aics, next_cursor_str


# ── TSDB exact-anchor 修复 ────────────────────────────────────────────────────


async def _repair_and_merge(
    redis: Redis,
    repair_aics: list[str],
    requested_windows: set[str] | None,
    post_filters: list[NumericPostFilter],
    lookback: str,
    hydrated: list[MetricsSnapshotView],
    limit: int,
) -> list[MetricsSnapshotView]:
    """§6.1 第 5/6/7 步 exact-anchor 修复。

    用 amp_snapshot_present tlast_over_time 确定每个 AIC 的 anchor observedAt，
    再按字段逐一核对 ts == anchor 才接受（不跨事件拼字段）。
    """
    now_dt = datetime.now(UTC)
    windows_list = list(requested_windows) if requested_windows else None

    # 1. anchor instant：每 aic 的最新 amp_snapshot_present 时刻
    try:
        anchor_expr = promql.build_snapshot_anchor_expr(repair_aics, lookback)
        anchor_samples = await tsdb.instant(anchor_expr, at=now_dt)
    except Exception as exc:
        logger.warning("snapshot_service.repair.anchor_error", exc_info=exc)
        return hydrated

    # anchor: aic → observed_at_ms（tlast_over_time 返回样本时间戳，非 gauge 值）
    anchor_by_aic: dict[str, int] = {}
    for s in anchor_samples:
        aic = s.labels.get("aic", "")
        if aic:
            anchor_by_aic[aic] = promql_timestamp_to_ms(s.value)

    if not anchor_by_aic:
        return hydrated

    aics_with_anchor = [a for a in repair_aics if a in anchor_by_aic]
    if not aics_with_anchor:
        return hydrated

    # 2. 字段值 + 字段 ts（instant_many）
    try:
        value_exprs = promql.build_snapshot_field_value_exprs(aics_with_anchor, windows_list, lookback)
        ts_exprs = promql.build_snapshot_field_ts_exprs(aics_with_anchor, windows_list, lookback)
        value_results = await tsdb.instant_many(value_exprs, at=now_dt)
        ts_results = await tsdb.instant_many(ts_exprs, at=now_dt)
    except Exception as exc:
        logger.warning("snapshot_service.repair.field_query_error", exc_info=exc)
        return hydrated

    # 3. 每 aic 重建快照（仅接受 ts == anchor 的字段）
    repaired_views: list[MetricsSnapshotView] = []
    for aic in aics_with_anchor:
        anchor_ms = anchor_by_aic[aic]

        load_fields: dict[str, float] = {}
        uptime_seconds: float | None = None
        window_fields: dict[str, dict[str, float]] = {}

        for field_key, samples in value_results.items():
            fk = str(field_key)
            ts_samples = ts_results.get(fk, [])

            if fk == _REPAIR_UPTIME_KEY:
                for vs in samples:
                    if vs.labels.get("aic") != aic:
                        continue
                    for tss in ts_samples:
                        if tss.labels.get("aic") != aic:
                            continue
                        if abs(promql_timestamp_to_ms(tss.value) - anchor_ms) < 1000:
                            uptime_seconds = vs.value
                            break
                continue

            if fk in _REPAIR_LOAD_FIELD_KEYS:
                for vs in samples:
                    if vs.labels.get("aic") != aic:
                        continue
                    for tss in ts_samples:
                        if tss.labels.get("aic") != aic:
                            continue
                        if abs(promql_timestamp_to_ms(tss.value) - anchor_ms) < 1000:
                            load_fields[fk] = vs.value
                            break
                continue

            if fk.startswith("latency_ms:"):
                _, window_tag, quantile = fk.split(":", 2)
                wm_field = f"{quantile}_latency_ms"
            elif ":" in fk:
                wm_field, window_tag = fk.split(":", 1)
            else:
                continue

            for vs in samples:
                if vs.labels.get("aic") != aic:
                    continue
                if vs.labels.get("window") != window_tag:
                    continue
                for tss in ts_samples:
                    if tss.labels.get("aic") != aic or tss.labels.get("window") != window_tag:
                        continue
                    if abs(promql_timestamp_to_ms(tss.value) - anchor_ms) < 1000:
                        window_fields.setdefault(window_tag, {})[wm_field] = vs.value
                        break

        load_metrics: LoadMetrics | None = None
        if load_fields:
            int_fields = {"active_tasks", "queued_tasks"}
            kwargs: dict[str, object] = {}
            for k, v in load_fields.items():
                kwargs[k] = int(v) if k in int_fields else v
            with contextlib.suppress(Exception):
                load_metrics = LoadMetrics(**kwargs)

        window_metrics: list[WindowMetrics] | None = None
        if window_fields:
            wms: list[WindowMetrics] = []
            for window_tag, wf in sorted(window_fields.items()):
                int_wm = {"request_total"}
                wm_kwargs: dict[str, object] = {"window": window_tag}
                for k, v in wf.items():
                    wm_kwargs[k] = int(v) if k in int_wm else v
                with contextlib.suppress(Exception):
                    wms.append(WindowMetrics(**wm_kwargs))
            if wms:
                window_metrics = wms

        repaired_snap = CachedSnapshot(
            aic=aic,
            observed_at_ms=anchor_ms,
            uptime_seconds=uptime_seconds,
            load_metrics=load_metrics,
            window_metrics=window_metrics,
            service_name=None,
            service_namespace=None,
            deployment_env=None,
        )

        # 4. 裁剪 + 后置过滤
        view = _cached_to_view(repaired_snap, requested_windows)
        filtered = apply_numeric_post_filters([view], post_filters, _snap_value_getter)
        if filtered:
            repaired_views.append(filtered[0])

        # 5. 异步回写 Hash+ZSet（失败不阻塞）
        task = asyncio.ensure_future(_safe_backfill(redis, repaired_snap))
        del task  # fire-and-forget；_safe_backfill 内部处理异常

    # 6. merge + 截断
    merged = list(hydrated) + repaired_views
    if len(merged) > limit:
        merged = merged[:limit]
    return merged


async def _safe_backfill(redis: Redis, snap: CachedSnapshot) -> None:
    """异步回写 snapshot 缓存，失败仅记 warning（§2.2）。"""
    try:
        await backfill_snapshot(redis, snap)
    except Exception as exc:
        logger.warning("snapshot_service.backfill_error", aic=snap.aic, exc_info=exc)
        _metrics.inc("amp_metrics_snapshot_cache_write_failures_total")


# ── 主入口 ────────────────────────────────────────────────────────────────────


async def query_snapshots(
    redis: Redis,
    req: MetricsSnapshotQueryRequest,
) -> tuple[list[MetricsSnapshotView], AMPResponseMeta]:
    """snapshots/query：Redis 优先读，Hash miss → TSDB exact-anchor 修复。

    设计 §6.1，C-METRIC-QUERY-1/5。
    """
    t0 = time.monotonic()
    settings = get_settings()

    # 1. 解析
    lookback = settings.metrics_snapshot_fallback_lookback
    compiled: CompiledFilter = compile_snapshot_filter(req.filter)
    requested_windows: set[str] | None = set(req.windows) if req.windows else None
    _ensure_snapshot_sort(req.sort)
    # timeRange 静默忽略（§6.1 第 1 条）

    page_limit = req.page.limit if req.page else 50
    cursor_fp = cursor_mod.filter_fingerprint(req.filter, req.windows)

    # 解码分页 cursor
    page_cursor: cursor_mod.SnapshotCursor | None = None
    if req.page and req.page.cursor:
        try:
            page_cursor = cursor_mod.decode_cursor(req.page.cursor, cursor_fp)
        except CursorInvalidError:
            raise
        except Exception as exc:
            raise CursorInvalidError("Invalid or mismatched pagination cursor") from exc

    # 2. Redis 收集页
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    hydrated, repair_aics, next_cursor_str = await _redis_collect_page(
        redis,
        static_aics=compiled.static_aics,
        cursor=page_cursor,
        limit=page_limit,
        requested_windows=requested_windows,
        post_filters=compiled.post_filters,
        label_matchers=compiled.label_matchers,
        cursor_fingerprint=cursor_fp,
    )

    _metrics.inc("amp_metrics_snapshot_cache_hits_total", len(hydrated))
    if repair_aics:
        _metrics.inc("amp_metrics_snapshot_cache_misses_total", len(repair_aics))

    # 3. TSDB 修复
    if repair_aics:
        promql_lookback = iso_duration_to_promql_range(lookback)
        hydrated = await _repair_and_merge(
            redis,
            repair_aics=repair_aics,
            requested_windows=requested_windows,
            post_filters=compiled.post_filters,
            lookback=promql_lookback,
            hydrated=hydrated,
            limit=page_limit,
        )

    # 4. freshness + meta
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _metrics.observe_ms("amp_metrics_query_latency_ms", elapsed_ms)

    freshness = await evaluate_freshness(redis, now_ms=now_ms)
    partial = apply_degrade_policy(freshness)
    meta = build_meta(
        freshness,
        now_ms=now_ms,
        next_cursor=next_cursor_str,
        partial=partial,
        elapsed_ms=elapsed_ms,
    )
    return hydrated, meta


__all__ = ["query_snapshots"]
