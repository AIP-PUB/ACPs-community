"""tests: AliveSyncError + AliveSyncSourceClient（Step 9）。

使用 monkeypatch.setattr(httpx, "AsyncClient", Fake) 模拟 HTTP 响应，
与项目既有 DSP sync service 测试惯例一致。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from app.heartbeat_sync.exception import AliveSyncError, AliveSyncErrorCode
from app.heartbeat_sync.source_client import AliveSyncSourceClient

# ── exception.py 测试 ────────────────────────────────────────────────────────


class TestAliveSyncError:
    def test_is_app_base_error(self) -> None:
        from app.core.base_exception import AppBaseError

        err = AliveSyncError(AliveSyncErrorCode.CONNECTION_FAIL, "test")
        assert isinstance(err, AppBaseError)

    def test_error_group_alive_sync(self) -> None:
        err = AliveSyncError(AliveSyncErrorCode.SNAPSHOT_FAIL, "snap fail")
        assert err.error_group == "alive_sync"

    def test_str_code(self) -> None:
        err = AliveSyncError(AliveSyncErrorCode.KAFKA_ERROR, "msg")
        assert "kafka_error" in err.error_name

    def test_raise_and_catch(self) -> None:
        with pytest.raises(AliveSyncError) as exc_info:
            raise AliveSyncError(AliveSyncErrorCode.RESYNC_REQUIRED, "resync")
        assert exc_info.value.error_name == "resync_required"


# ── source_client.py 测试工具 ─────────────────────────────────────────────────

_VALID_SYNC_INFO = {
    "type": "amp-alive-delta",
    "schemaVersion": "1",
    "snapshotContentType": "application/x-ndjson",
    "kafkaTopic": "amp.heartbeat.alive-delta",
    "shardCount": 1,
    "refreshEmitIntervalSeconds": 60,
    "deltaRetentionHours": 24,
    "currentPublishedSeqByShard": {"hb-000": "42"},
}

_VALID_META_LINE = json.dumps(
    {
        "recordType": "snapshot-meta",
        "type": "amp-alive-delta",
        "cutoverSeqByShard": {"hb-000": "42"},
        "generatedAt": "2026-06-13T01:00:00Z",
    }
)

_VALID_ROW_LINE = json.dumps(
    {
        "shard": "hb-000",
        "seq": "43",
        "type": "amp-alive-delta",
        "id": "urn:amp:alive:AIC-001",
        "version": "43",
        "op": "upsert",
        "kind": "snapshot",
        "payload": {"aic": "AIC-001", "lastSeenAt": "2026-06-13T01:00:00Z"},
    }
)


def _make_response(status_code: int, json_body: dict[str, Any] | None = None, text: str = "") -> httpx.Response:
    """构造 httpx.Response（不发送真实网络请求）。"""
    content = json.dumps(json_body).encode() if json_body is not None else text.encode()
    return httpx.Response(status_code=status_code, content=content)


class FakeHttpxClient:
    """用于测试 fetch_sync_info 的伪 httpx.AsyncClient（非流式）。"""

    def __init__(self, *, status_code: int = 200, json_body: dict[str, Any] | None = None, **kw: object):
        self._status_code = status_code
        self._json_body = json_body or {}
        raw_headers = kw.get("headers")
        self.headers: dict[str, str] = dict(raw_headers) if isinstance(raw_headers, dict) else {}

    async def __aenter__(self) -> FakeHttpxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def get(self, url: str, **_kw: object) -> httpx.Response:
        return _make_response(self._status_code, self._json_body)


class FakeStreamResponse:
    """伪造 httpx stream response，支持 aiter_lines()。"""

    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    async def aread(self) -> bytes:
        return b"503 error body"

    async def aiter_lines(self) -> object:
        for line in self._lines:
            yield line


class FakeStreamClient:
    """用于测试 stream_snapshot 的伪 httpx.AsyncClient（流式）。"""

    def __init__(self, *, status_code: int = 200, lines: list[str] | None = None, **kw: object):
        self._status_code = status_code
        self._lines = lines or [_VALID_META_LINE, _VALID_ROW_LINE]
        raw_headers = kw.get("headers")
        self.headers: dict[str, str] = dict(raw_headers) if isinstance(raw_headers, dict) else {}

    async def __aenter__(self) -> FakeStreamClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def aclose(self) -> None:
        pass

    @asynccontextmanager
    async def stream(self, method: str, url: str, **_kw: object) -> Any:
        yield FakeStreamResponse(status_code=self._status_code, lines=self._lines)


# ── fetch_sync_info 测试 ──────────────────────────────────────────────────────


class TestFetchSyncInfo:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeHttpxClient(status_code=200, json_body=_VALID_SYNC_INFO),
        )
        client = AliveSyncSourceClient("http://localhost:9009/acps-amp-v1/heartbeat")
        info = await client.fetch_sync_info()
        assert info.shard_count == 1
        assert info.kafka_topic == "amp.heartbeat.alive-delta"

    @pytest.mark.asyncio
    async def test_404_raises_provider_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeHttpxClient(status_code=404),
        )
        client = AliveSyncSourceClient("http://localhost:9009/acps-amp-v1/heartbeat")
        with pytest.raises(AliveSyncError) as exc_info:
            await client.fetch_sync_info()
        assert exc_info.value.error_name == AliveSyncErrorCode.PROVIDER_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_503_raises_snapshot_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeHttpxClient(status_code=503),
        )
        client = AliveSyncSourceClient("http://localhost:9009/acps-amp-v1/heartbeat")
        with pytest.raises(AliveSyncError) as exc_info:
            await client.fetch_sync_info()
        assert exc_info.value.error_name == AliveSyncErrorCode.SNAPSHOT_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_empty_base_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeHttpxClient(status_code=200, json_body=_VALID_SYNC_INFO),
        )
        client = AliveSyncSourceClient("")
        with pytest.raises(AliveSyncError) as exc_info:
            await client.fetch_sync_info()
        assert exc_info.value.error_name == AliveSyncErrorCode.CLIENT_CONFIG_ERROR

    @pytest.mark.asyncio
    async def test_bearer_token_sent_as_authorization_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _factory(**kw: object) -> FakeHttpxClient:
            raw_headers = kw.get("headers")
            captured["headers"] = dict(raw_headers) if isinstance(raw_headers, dict) else {}
            return FakeHttpxClient(status_code=200, json_body=_VALID_SYNC_INFO, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        client = AliveSyncSourceClient(
            "http://localhost:9009/acps-amp-v1/heartbeat",
            bearer_token="svc-sync-token",  # noqa: S106
        )
        await client.fetch_sync_info()
        assert captured["headers"].get("Authorization") == "Bearer svc-sync-token"


# ── stream_snapshot 测试 ──────────────────────────────────────────────────────


class TestStreamSnapshot:
    @pytest.mark.asyncio
    async def test_success_meta_and_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeStreamClient(status_code=200),
        )
        client = AliveSyncSourceClient("http://localhost:9009/acps-amp-v1/heartbeat")
        async with client.stream_snapshot() as (meta, rows):
            assert meta.generated_at == "2026-06-13T01:00:00Z"
            collected = [env async for env in rows]
        assert len(collected) == 1
        assert collected[0].shard == "hb-000"

    @pytest.mark.asyncio
    async def test_404_raises_provider_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeStreamClient(status_code=404),
        )
        client = AliveSyncSourceClient("http://localhost:9009")
        with pytest.raises(AliveSyncError) as exc_info:
            async with client.stream_snapshot():
                pass
        assert exc_info.value.error_name == AliveSyncErrorCode.PROVIDER_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_503_raises_snapshot_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: FakeStreamClient(status_code=503),
        )
        client = AliveSyncSourceClient("http://localhost:9009")
        with pytest.raises(AliveSyncError) as exc_info:
            async with client.stream_snapshot():
                pass
        assert exc_info.value.error_name == AliveSyncErrorCode.SNAPSHOT_UNAVAILABLE
