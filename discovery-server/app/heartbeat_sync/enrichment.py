"""alive-sync 查询结果注入（§8）。

attach_alive_status：给本地产出的 DiscoveryResponse 注入 aliveMap。
- 仅对 result._alive_enrichable=True 的结果（本地产出）注入
- 转发回传结果（_alive_enrichable=False）原样透传其 aliveMap，不覆盖
- holder 返回 None（未启用 alive sync）时跳过，对既有调用方零破坏
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.heartbeat_sync.holder import get_alive_reader

if TYPE_CHECKING:
    from acps_sdk.adp.models import DiscoveryResponse

logger = logging.getLogger(__name__)


async def attach_alive_status(response: DiscoveryResponse) -> None:
    """原地注入 aliveMap 到 response.result（如满足条件）。

    只对本地产出结果（_alive_enrichable=True）注入 aliveMap；
    转发结果保持原有 aliveMap 不变（ADP §4.2.3 透传语义）。
    """
    if response.result is None:
        return

    result = response.result
    if not result._alive_enrichable:
        return

    reader = get_alive_reader()
    if reader is None:
        return

    # 收集本次发现结果中所有 AIC
    aics = [skill.aic for group in result.agents for skill in group.agent_skills]
    if not aics:
        return

    try:
        alive_views = await reader.load_alive_views(aics)
    except Exception as exc:  # pragma: no cover - 保护主查询链路
        logger.warning("aliveMap 注入失败，跳过本次注入: %s", exc)
        return

    if not alive_views:
        return

    result.alive_map = {aic: view.to_output_dict() for aic, view in alive_views.items()}
