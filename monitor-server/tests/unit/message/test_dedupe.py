"""单元测试：C-2a dedupe.py — 持久化去重窗口（Redis mock）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.message.dedupe import KEY_PREFIX, filter_unseen, mark_seen


def _make_redis(mget_return: list) -> MagicMock:
    redis = MagicMock()
    redis.mget = AsyncMock(return_value=mget_return)
    pipe = AsyncMock()
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=pipe), __aexit__=AsyncMock()))
    return redis


class TestFilterUnseen:
    @pytest.mark.asyncio
    async def test_all_new(self) -> None:
        redis = _make_redis([None, None])
        unseen, available = await filter_unseen(redis, ["a", "b"])
        assert unseen == {"a", "b"}
        assert available is True

    @pytest.mark.asyncio
    async def test_some_seen(self) -> None:
        redis = _make_redis([b"1", None])
        unseen, available = await filter_unseen(redis, ["a", "b"])
        assert unseen == {"b"}
        assert available is True

    @pytest.mark.asyncio
    async def test_all_seen(self) -> None:
        redis = _make_redis([b"1", b"1"])
        unseen, available = await filter_unseen(redis, ["a", "b"])
        assert unseen == set()
        assert available is True

    @pytest.mark.asyncio
    async def test_empty_log_ids(self) -> None:
        redis = _make_redis([])
        unseen, available = await filter_unseen(redis, [])
        assert unseen == set()
        assert available is True

    @pytest.mark.asyncio
    async def test_redis_error_fail_open(self) -> None:
        redis = MagicMock()
        redis.mget = AsyncMock(side_effect=ConnectionError("redis down"))
        unseen, available = await filter_unseen(redis, ["a", "b"])
        assert unseen == {"a", "b"}
        assert available is False

    def test_key_prefix(self) -> None:
        assert KEY_PREFIX.startswith("amp:message:dedupe:")


class TestMarkSeen:
    @pytest.mark.asyncio
    async def test_marks_all_ids(self) -> None:
        redis = MagicMock()
        pipe = AsyncMock()
        pipe.set = MagicMock(return_value=None)
        pipe.execute = AsyncMock()
        redis.pipeline = MagicMock(
            return_value=MagicMock(__aenter__=AsyncMock(return_value=pipe), __aexit__=AsyncMock())
        )
        await mark_seen(redis, ["a", "b"], ttl_seconds=3600)
        assert pipe.set.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_ids_no_op(self) -> None:
        redis = MagicMock()
        redis.pipeline = MagicMock()
        await mark_seen(redis, [], ttl_seconds=3600)
        redis.pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_error_does_not_raise(self) -> None:
        redis = MagicMock()
        redis.pipeline = MagicMock(side_effect=Exception("pipe error"))
        await mark_seen(redis, ["a"], ttl_seconds=3600)
