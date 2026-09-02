"""tests/unit/test_access_trace_hint.py — trace_seen hint cache 测试。

TDD C-3 (trace_hint.py)：先写测试（红）→ 实现 trace_hint.py（绿）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestMarkTraces:
    @pytest.mark.asyncio
    async def test_sadd_called(self) -> None:
        from app.access.trace_hint import mark_traces

        redis = AsyncMock()
        redis.sadd = AsyncMock()
        await mark_traces(redis, {"t1", "t2"}, ttl_seconds=3600)
        redis.sadd.assert_called()

    @pytest.mark.asyncio
    async def test_redis_error_no_raise(self) -> None:
        from app.access.trace_hint import mark_traces

        redis = AsyncMock()
        redis.sadd = AsyncMock(side_effect=Exception("Connection error"))
        # Should not raise
        await mark_traces(redis, {"t1"}, ttl_seconds=3600)


class TestMaybeSeen:
    @pytest.mark.asyncio
    async def test_returns_true_when_member(self) -> None:
        from app.access.trace_hint import maybe_seen

        redis = AsyncMock()
        redis.sismember = AsyncMock(return_value=True)
        result = await maybe_seen(redis, "t1")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_member(self) -> None:
        from app.access.trace_hint import maybe_seen

        redis = AsyncMock()
        redis.sismember = AsyncMock(return_value=False)
        result = await maybe_seen(redis, "unknown-trace")
        assert result is False

    @pytest.mark.asyncio
    async def test_redis_error_returns_none(self) -> None:
        """Redis 异常 → None（调用方忽略预检，直接查 CH）。"""
        from app.access.trace_hint import maybe_seen

        redis = AsyncMock()
        redis.sismember = AsyncMock(side_effect=Exception("Connection error"))
        result = await maybe_seen(redis, "t1")
        assert result is None


class TestConstants:
    def test_key_defined(self) -> None:
        from app.access.trace_hint import KEY

        assert KEY.startswith("amp:")
        assert "trace" in KEY
