"""app/access/archive.py — Parquet 冷归档后台任务（§3.4、§2.5，C-ACCESS-RETENTION-2）。

在 ClickHouse TTL 删除热数据前，将临近保留窗口末端的 access_events 分区按天导出为
Parquet 文件，上传至 MinIO（S3 兼容）对象存储。

实现策略：
  - 利用 ClickHouse 内置 `s3()` 表函数，让 ClickHouse 直接写入 MinIO，
    无需在应用侧缓冲大量数据（零拷贝）。
  - 导出幂等：Redis 记录已成功导出的日期（`amp:access:archive:exported:{YYYYMMDD}`），
    任务崩溃后重启会自动跳过已导出日期，仅补齐缺失分区。
  - 本任务不 DROP 分区：热数据删除由 ClickHouse TTL 策略负责（C-ACCESS-RETENTION-2 第 3 条）。

依赖关系：
  - ClickHouse 须能通过 `settings.access_minio_endpoint`（在 Docker 中通常是内网地址，如
    `http://dev-minio:9000`）访问 MinIO；应用侧地址与 ClickHouse 侧可通过配置分离。
  - 由 `AccessRuntime` 当 `access_archive_enabled=true` 时启动后台任务。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from app.access.exception import ClickHouseInsertError
from app.core.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

# Redis key：已导出日期集合（SETNX 幂等防重复导出）
_ARCHIVE_KEY_PREFIX = "amp:access:archive:exported:"
# TTL：保留归档标记若干年（远超 archive_retention_days，防标记提前过期）
_ARCHIVE_MARKER_TTL_SECONDS = 365 * 10 * 24 * 3600


class AccessArchiveTask:
    """Parquet 冷归档周期后台任务（§3.4，C-ACCESS-RETENTION-2）。

    生命周期由 AccessRuntime 管理（`access_archive_enabled` 门控）。
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._running = True

    def stop(self) -> None:
        """标记停止，run() 在下一周期退出。"""
        self._running = False

    async def run(self) -> None:
        """主循环：按 `access_archive_interval_seconds` 周期执行 run_once()。"""
        logger.info("AccessArchiveTask: 归档任务启动", interval=settings.access_archive_interval_seconds)
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("AccessArchiveTask: run_once 异常，等待下次周期重试")
            await asyncio.sleep(settings.access_archive_interval_seconds)

    async def run_once(self) -> None:
        """单次归档扫描：计算待归档日期 → 按天导出 Parquet → 标记已导出。"""
        dates = self._find_dates_to_archive()
        if not dates:
            logger.debug("AccessArchiveTask: 无待归档日期，跳过")
            return

        logger.info("AccessArchiveTask: 发现待归档日期", count=len(dates), dates=[str(d) for d in dates[:5]])
        for target_date in dates:
            if not self._running:
                break
            date_str = target_date.strftime("%Y%m%d")
            if await self._is_already_exported(date_str):
                continue
            try:
                await self._export_date(target_date, date_str)
                await self._mark_exported(date_str)
                logger.info("AccessArchiveTask: 日期归档完成", date=date_str)
            except Exception:
                logger.exception("AccessArchiveTask: 日期归档失败，跳过", date=date_str)

    def _find_dates_to_archive(self) -> list[date]:
        """计算临近 raw_retention_days TTL 但尚未超出 archive_retention_days 的日期段。

        归档窗口：
          - 末端：today - raw_retention_days + archive_lead_days（TTL 删除前的提前量，默认 3 天）
          - 首端：today - archive_retention_days（超出归档保留期的分区不再尝试，可能已被删）
        """
        today = datetime.now(UTC).date()
        retention_days = settings.access_raw_retention_days
        archive_days = settings.access_archive_retention_days
        # 在 TTL 删除前留 3 天余量（C-ACCESS-RETENTION-2）
        archive_lead_days = 3

        end_date = today - timedelta(days=retention_days - archive_lead_days)
        start_date = today - timedelta(days=archive_days)

        if start_date > end_date:
            return []

        result: list[date] = []
        current = start_date
        while current <= end_date:
            result.append(current)
            current += timedelta(days=1)
        return result

    async def _is_already_exported(self, date_str: str) -> bool:
        """检查指定日期是否已成功导出（Redis SETNX 标记）。"""
        try:
            key = f"{_ARCHIVE_KEY_PREFIX}{date_str}"
            val = await self._redis.get(key)
            return val is not None
        except Exception:
            logger.warning("AccessArchiveTask: Redis 检查已导出标记失败，保守视为未导出", date=date_str)
            return False

    async def _export_date(self, target_date: date, date_str: str) -> None:
        """通过 ClickHouse s3() 表函数将指定日期 access_events 导出为 Parquet。

        使用 ClickHouse 内置 S3 写入，数据不经过应用侧缓冲（设计 §3.4 第 2 条）。

        注意：`settings.access_minio_endpoint` 须为 ClickHouse 容器侧可访问地址
        （开发环境 Docker 网络内：`http://dev-minio:9000`）。
        """
        from app.core.clickhouse_client import get_clickhouse_client

        endpoint = settings.access_minio_endpoint.rstrip("/")
        bucket = settings.access_minio_bucket
        access_key = settings.minio_access_key
        secret_key = settings.minio_secret_key
        secure_str = "true" if settings.access_minio_secure else "false"  # noqa: F841

        # S3 路径：{endpoint}/{bucket}/{YYYY}/{MM}/{DD}/access_events.parquet
        year_str = target_date.strftime("%Y")
        month_str = target_date.strftime("%m")
        s3_url = f"{endpoint}/{bucket}/{year_str}/{month_str}/{date_str}/access_events.parquet"

        # ClickHouse s3() 函数直接写入 Parquet
        # toYYYYMMDD(timestamp) 将 DateTime64(3) 转为日期整数比较
        # s3_url/access_key/secret_key 均来自受控配置，date_str 来自 strftime，非用户输入
        sql = (
            f"INSERT INTO FUNCTION s3('{s3_url}', '{access_key}', '{secret_key}', 'Parquet') "  # noqa: S608  # nosec B608
            f"SELECT * FROM {settings.clickhouse_database}.access_events "
            f"WHERE toYYYYMMDD(timestamp) = {date_str}"
        )

        try:
            client = await get_clickhouse_client()
            await client.command(sql)
        except Exception as exc:
            raise ClickHouseInsertError(f"Parquet 导出失败（date={date_str}）: {exc}") from exc

    async def _mark_exported(self, date_str: str) -> None:
        """在 Redis 中标记指定日期已成功导出（幂等，TTL 远超归档保留期）。"""
        try:
            key = f"{_ARCHIVE_KEY_PREFIX}{date_str}"
            exported_at = datetime.now(UTC).isoformat()
            await self._redis.set(key, exported_at, ex=_ARCHIVE_MARKER_TTL_SECONDS)
        except Exception:
            logger.warning("AccessArchiveTask: Redis 写入已导出标记失败（不影响导出本身）", date=date_str)
