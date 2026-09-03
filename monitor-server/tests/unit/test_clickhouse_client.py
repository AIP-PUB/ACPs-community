"""tests/unit/test_clickhouse_client.py — ClickHouse 客户端工厂测试。

TDD C-1：先写测试（红）→ 实现 clickhouse_client.py（绿）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetClickhouseClient:
    """get_clickhouse_client 单例懒初始化测试。"""

    @pytest.mark.asyncio
    async def test_returns_client_instance(self) -> None:
        from app.core import clickhouse_client as ch_mod

        mock_client = MagicMock()
        with patch("app.core.clickhouse_client.clickhouse_connect") as mock_cc:
            mock_cc.get_async_client = AsyncMock(return_value=mock_client)
            ch_mod._client = None
            client = await ch_mod.get_clickhouse_client()
            assert client is mock_client

    @pytest.mark.asyncio
    async def test_singleton_reused(self) -> None:
        from app.core import clickhouse_client as ch_mod

        mock_client = MagicMock()
        ch_mod._client = mock_client
        client = await ch_mod.get_clickhouse_client()
        assert client is mock_client

    @pytest.mark.asyncio
    async def test_close_sets_none(self) -> None:
        from app.core import clickhouse_client as ch_mod

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        ch_mod._client = mock_client
        await ch_mod.close_clickhouse_client()
        assert ch_mod._client is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self) -> None:
        from app.core import clickhouse_client as ch_mod

        ch_mod._client = None
        # Should not raise
        await ch_mod.close_clickhouse_client()
        assert ch_mod._client is None


class TestCheckClickhouse:
    """check_clickhouse 探活测试。"""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        from app.core import clickhouse_client as ch_mod

        mock_probe = AsyncMock()
        mock_probe.query = AsyncMock()
        mock_probe.close = AsyncMock()
        with patch("app.core.clickhouse_client.clickhouse_connect") as mock_cc:
            mock_cc.get_async_client = AsyncMock(return_value=mock_probe)
            result = await ch_mod.check_clickhouse()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self) -> None:
        from app.core import clickhouse_client as ch_mod

        mock_probe = AsyncMock()
        mock_probe.query = AsyncMock(side_effect=Exception("Connection refused"))
        mock_probe.close = AsyncMock()
        with patch("app.core.clickhouse_client.clickhouse_connect") as mock_cc:
            mock_cc.get_async_client = AsyncMock(return_value=mock_probe)
            result = await ch_mod.check_clickhouse()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_client(self) -> None:
        from app.core import clickhouse_client as ch_mod

        with patch("app.core.clickhouse_client.clickhouse_connect") as mock_cc:
            mock_cc.get_async_client = AsyncMock(side_effect=Exception("can't connect"))
            result = await ch_mod.check_clickhouse()
        assert result is False
