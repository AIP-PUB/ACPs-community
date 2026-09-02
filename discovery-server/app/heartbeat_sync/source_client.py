"""AMP alive-sync HTTP transport 客户端。

对接 monitor-server Provider 的 /sync/info 与 /sync/snapshot（NDJSON）端点。
解析委托 SDK（parse_meta_line / parse_snapshot_row），本模块只负责：
  1. HTTP 发起与关闭
  2. 错误码 → AliveSyncError 映射
  3. 保持 httpx 流在 stream_snapshot context manager 内开放
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from acps_sdk.amp.alive_sync.snapshot import parse_meta_line, parse_snapshot_row
from acps_sdk.amp.heartbeat_sync import (
    SNAPSHOT_CONTENT_TYPE,
    AliveDeltaEnvelope,
    AliveSnapshotMeta,
    HeartbeatSyncInfo,
)

from app.heartbeat_sync.exception import AliveSyncError, AliveSyncErrorCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class AliveSyncSourceClient:
    """HTTP client，对接 Provider 的 /sync/info 与 /sync/snapshot。

    使用方式：
        async with source_client.stream_snapshot() as (meta, rows):
            await engine.apply_snapshot(meta, rows)
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        bearer_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {}
        token = (bearer_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        )

    async def fetch_sync_info(self) -> HeartbeatSyncInfo:
        """GET /sync/info → HeartbeatSyncInfo。

        Raises:
            AliveSyncError(CLIENT_CONFIG_ERROR): base_url 为空。
            AliveSyncError(PROVIDER_UNAVAILABLE): 404（SYNC_DISABLED）。
            AliveSyncError(SNAPSHOT_UNAVAILABLE): 503。
            AliveSyncError(INVALID_RESPONSE): 响应格式非法。
            AliveSyncError(CONNECTION_FAIL): 网络异常。
        """
        if not self._base_url:
            raise AliveSyncError(
                AliveSyncErrorCode.CLIENT_CONFIG_ERROR,
                "ALIVE_SYNC_PROVIDER_BASE_URL 未配置",
            )
        try:
            response = await self._client.get("/sync/info")
        except httpx.RequestError as exc:
            raise AliveSyncError(
                AliveSyncErrorCode.CONNECTION_FAIL,
                f"连接 /sync/info 失败: {exc}",
            ) from exc

        if response.status_code == 404:
            raise AliveSyncError(
                AliveSyncErrorCode.PROVIDER_UNAVAILABLE,
                "Provider /sync/info 返回 404（SYNC_DISABLED）",
                status_code=404,
            )
        if response.status_code == 503:
            raise AliveSyncError(
                AliveSyncErrorCode.SNAPSHOT_UNAVAILABLE,
                "Provider /sync/info 返回 503（服务不可用）",
                status_code=503,
            )
        if response.status_code != 200:
            raise AliveSyncError(
                AliveSyncErrorCode.INVALID_RESPONSE,
                f"Provider /sync/info 非预期状态码: {response.status_code}",
            )

        try:
            return HeartbeatSyncInfo.model_validate(response.json())
        except Exception as exc:
            raise AliveSyncError(
                AliveSyncErrorCode.INVALID_RESPONSE,
                f"Provider /sync/info 响应格式非法: {exc}",
            ) from exc

    @asynccontextmanager
    async def stream_snapshot(
        self,
    ) -> AsyncIterator[tuple[AliveSnapshotMeta, AsyncIterator[AliveDeltaEnvelope]]]:
        """GET /sync/snapshot，流式读取 NDJSON。

        用法（保持 httpx 流在 rows 消费期间开放）：
            async with source_client.stream_snapshot() as (meta, rows):
                await engine.apply_snapshot(meta, rows)

        Raises:
            AliveSyncError(PROVIDER_UNAVAILABLE): 404。
            AliveSyncError(SNAPSHOT_UNAVAILABLE): 503 SNAPSHOT_UNAVAILABLE。
            AliveSyncError(DELTA_LOG_UNHEALTHY): 503 DELTA_LOG_UNHEALTHY。
            AliveSyncError(INVALID_RESPONSE): 响应格式/协议非法。
            AliveSyncError(CONNECTION_FAIL): 网络异常。
        """
        try:
            async with self._client.stream(
                "GET",
                "/sync/snapshot",
                headers={"Accept": SNAPSHOT_CONTENT_TYPE},
            ) as response:
                if response.status_code == 404:
                    raise AliveSyncError(
                        AliveSyncErrorCode.PROVIDER_UNAVAILABLE,
                        "Provider /sync/snapshot 返回 404（SYNC_DISABLED）",
                        status_code=404,
                    )
                if response.status_code == 503:
                    body = await response.aread()
                    body_str = body.decode(errors="replace")
                    code = (
                        AliveSyncErrorCode.DELTA_LOG_UNHEALTHY
                        if "DELTA_LOG_UNHEALTHY" in body_str
                        else AliveSyncErrorCode.SNAPSHOT_UNAVAILABLE
                    )
                    raise AliveSyncError(
                        code,
                        f"Provider /sync/snapshot 返回 503: {body_str[:200]}",
                        status_code=503,
                    )
                if response.status_code != 200:
                    raise AliveSyncError(
                        AliveSyncErrorCode.INVALID_RESPONSE,
                        f"Provider /sync/snapshot 非预期状态码: {response.status_code}",
                    )

                # 建立单一行迭代器，meta 和 rows 共享
                line_iter = response.aiter_lines().__aiter__()

                # 读首行 → meta
                meta_line: str | None = None
                async for line in line_iter:
                    if line.strip():
                        meta_line = line
                        break

                if meta_line is None:
                    raise AliveSyncError(
                        AliveSyncErrorCode.INVALID_RESPONSE,
                        "Provider /sync/snapshot 响应为空（无 meta 行）",
                    )

                try:
                    meta = parse_meta_line(meta_line)
                except Exception as exc:
                    raise AliveSyncError(
                        AliveSyncErrorCode.INVALID_RESPONSE,
                        f"Provider /sync/snapshot meta 行解析失败: {exc}",
                    ) from exc

                async def _rows() -> AsyncIterator[AliveDeltaEnvelope]:
                    async for raw_line in line_iter:
                        if not raw_line.strip():
                            continue
                        try:
                            yield parse_snapshot_row(raw_line)
                        except Exception as exc:
                            raise AliveSyncError(
                                AliveSyncErrorCode.INVALID_RESPONSE,
                                f"Provider /sync/snapshot 数据行解析失败: {exc}",
                            ) from exc

                yield meta, _rows()
        except httpx.RequestError as exc:
            raise AliveSyncError(
                AliveSyncErrorCode.CONNECTION_FAIL,
                f"连接 /sync/snapshot 失败: {exc}",
            ) from exc

    async def close(self) -> None:
        """关闭 httpx client。"""
        await self._client.aclose()
