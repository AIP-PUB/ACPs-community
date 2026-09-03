"""alive-sync 查询侧只读句柄（holder 模式，与发现 API 解耦）。

参考 app/discovery/semantic_matcher_holder.py 的 holder 模式：
  - set_alive_reader：alive sync 启动后将 PostgresAliveSyncStore 注入
  - get_alive_reader：发现 API / enrichment 按需取只读句柄（空则跳过注入）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from acps_sdk.amp.alive_sync.store import AliveReader

_alive_reader: AliveReader | None = None


def set_alive_reader(reader: AliveReader) -> None:
    """注入 AliveReader 实例（alive sync 启动成功后调用）。"""
    global _alive_reader
    _alive_reader = reader


def clear_alive_reader() -> None:
    """清除 AliveReader 实例（alive sync 停止时调用）。"""
    global _alive_reader
    _alive_reader = None


def get_alive_reader() -> AliveReader | None:
    """返回当前 AliveReader，未启用时返回 None（enrichment 按 None 跳过注入）。"""
    return _alive_reader
