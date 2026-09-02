"""AMP alive-sync 查询投影模型。

AliveView 是面向查询侧的只读视图，与存储行（AliveRecord）解耦。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliveView:
    """单个 AIC 的 alive 查询投影（从 agent_alive_status 行读取后构造）。"""

    aic: str
    alive: bool
    last_seen_at: str | None

    def to_output_dict(self) -> dict[str, object]:
        """转换为 ADP 响应中 aliveMap 的单条值结构。

        返回示例：
            {"alive": True, "aliveLastSeenAt": "2026-06-13T01:20:00Z"}
            {"alive": False, "aliveLastSeenAt": null}
        键 aliveLastSeenAt 始终存在（即使值为 None），以区别于「键缺失=未知 AIC」。
        """
        return {"alive": self.alive, "aliveLastSeenAt": self.last_seen_at}
