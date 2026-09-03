"""tests/unit/test_access_dedupe.py — Writer 持久化去重窗口测试。

TDD C-3 (dedupe.py)：先写测试（红）→ 实现 dedupe.py（绿）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_redis_mock(**kwargs: Any) -> Any:
    redis = AsyncMock()
    for k, v in kwargs.items():
        setattr(redis, k, v)
    return redis


class TestFilterUnseen:
    """filter_unseen：写 CH 前只读检查（fail-open）。"""

    @pytest.mark.asyncio
    async def test_all_unseen_returns_all(self) -> None:
        from app.access.dedupe import filter_unseen

        redis = AsyncMock()
        redis.mget = AsyncMock(return_value=[None, None, None])
        result, available = await filter_unseen(redis, ["id1", "id2", "id3"])
        assert result == {"id1", "id2", "id3"}
        assert available is True

    @pytest.mark.asyncio
    async def test_some_seen_excluded(self) -> None:
        from app.access.dedupe import filter_unseen

        redis = AsyncMock()
        # id2 is already seen (non-None value)
        redis.mget = AsyncMock(return_value=[None, "1", None])
        result, _ = await filter_unseen(redis, ["id1", "id2", "id3"])
        assert "id1" in result
        assert "id3" in result
        assert "id2" not in result

    @pytest.mark.asyncio
    async def test_all_seen_returns_empty(self) -> None:
        from app.access.dedupe import filter_unseen

        redis = AsyncMock()
        redis.mget = AsyncMock(return_value=["1", "1"])
        result, _ = await filter_unseen(redis, ["id1", "id2"])
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_redis_error_fail_open(self) -> None:
        """Redis 异常 → fail-open：返回全部 log_ids + available=False。"""
        from app.access.dedupe import filter_unseen

        redis = AsyncMock()
        redis.mget = AsyncMock(side_effect=Exception("Connection error"))
        log_ids = ["id1", "id2"]
        result, available = await filter_unseen(redis, log_ids)
        assert result == set(log_ids)
        assert available is False

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self) -> None:
        from app.access.dedupe import filter_unseen

        redis = AsyncMock()
        redis.mget = AsyncMock(return_value=[])
        result, _ = await filter_unseen(redis, [])
        assert result == set()


class TestMarkSeen:
    """mark_seen：写 CH 成功后三步提交第 2 步（fail-open，异常只告警）。"""

    @pytest.mark.asyncio
    async def test_marks_keys_with_ttl(self) -> None:
        from app.access.dedupe import mark_seen

        redis = AsyncMock()
        pipeline = AsyncMock()
        pipeline.__aenter__ = AsyncMock(return_value=pipeline)
        pipeline.__aexit__ = AsyncMock(return_value=False)
        pipeline.set = MagicMock()
        pipeline.execute = AsyncMock(return_value=[True, True])
        redis.pipeline = MagicMock(return_value=pipeline)

        await mark_seen(redis, ["id1", "id2"], ttl_seconds=3600)
        assert pipeline.set.call_count == 2

    @pytest.mark.asyncio
    async def test_redis_error_no_raise(self) -> None:
        """Redis 异常 → 只告警，不抛出（标记失败只导致后续可能重复）。"""
        from app.access.dedupe import mark_seen

        redis = AsyncMock()
        redis.pipeline = MagicMock(side_effect=Exception("Connection error"))
        # Should not raise
        await mark_seen(redis, ["id1"], ttl_seconds=3600)


class TestKeyFormat:
    """KEY_PREFIX 格式验证。"""

    def test_key_prefix_defined(self) -> None:
        from app.access.dedupe import KEY_PREFIX

        assert KEY_PREFIX.startswith("amp:")
        assert "dedupe" in KEY_PREFIX
