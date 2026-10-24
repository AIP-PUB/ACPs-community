"""CredRank：把「语义排名」升级为「语义 × 能力可信 × 可用性」的复合排序。

## 为什么在客户端排序，而不是改发现服务？

标准只规定发现流程与结果结构，发现服务给出的 ``ranking`` 是**语义匹配序**。
若直接修改发现服务，等于自造一套发现语义：既破坏与既有实现的兼容，
也无法在跨域转发链中被下游识别。

本模块因此选择：

1. **只消费标准发现结果**，不做任何协议改造；
2. 在调用方侧做**本地二次排序**（纯客户端行为，零兼容成本）；
3. 把「能力可信」作为**可选过滤条件**回灌，说明是在标准机制内做增强；
4. 引入**探索配额**，保证冷启动智能体不被「零证据 → 零曝光」的死循环饿死。

## 排序公式

```
semantic_i = 0.1 + 0.9 × (1 - (rank_i - 1) / (max_rank - 1))     ← 映射到 (0.1, 1.0]

combined_i = α × semantic_i + β × (cred_i / 100)                  ← 默认 α=0.4, β=0.6

排序：combined 降序；同分时按原始语义序升序
```

## 探索配额

纯按 `combined` 排序会让冷启动智能体永远排在最后 → 永远拿不到调用 →
永远没有证据 → 永远是冷启动。本模块在最终序列的前 `explore_slot` 位置
插入最多 `explore_quota` 个冷启动候选，以极小的排序代价打破这个死循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .reputation import ReputationStore, ScoreDetail

__all__ = [
    "RankConfig",
    "RankedCandidate",
    "semantic_score",
    "cred_rank",
    "explain_ranking",
]


@dataclass
class RankConfig:
    """排序超参数。"""

    semantic_weight: float = 0.4      # α：发现服务给出的语义匹配序权重
    cred_weight: float = 0.6          # β：能力可信度权重
    explore_quota: int = 1            # 保留给冷启动智能体的探索名额
    explore_slot: int = 3             # 探索名额的插入位置（1 起）
    min_cred_for_direct: float = 25.0 # 低于该可信度不直接首发（需预检或后置）
    require_probe_below: float = 55.0 # 可信度低于该值时必须先做能力预检


@dataclass
class RankedCandidate:
    """一个参与排序的候选及其全部评分中间量。"""

    aic: str
    skillId: str
    semanticScore: float
    credScore: float
    combined: float
    originalRanking: int
    finalRanking: int = 0
    tier: str = ""
    samples: int = 0
    exploration: bool = False
    needsProbe: bool = False
    detail: Optional[ScoreDetail] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def brief(self) -> Dict[str, Any]:
        """精简视图，适合直接序列化返回给调用方。"""
        return {
            "aic": self.aic,
            "skillId": self.skillId,
            "adpRanking": self.originalRanking,
            "credRanking": self.finalRanking,
            "semanticScore": round(self.semanticScore, 4),
            "credScore": round(self.credScore, 2),
            "combined": round(self.combined, 4),
            "tier": self.tier,
            "samples": self.samples,
            "exploration": self.exploration,
            "needsProbe": self.needsProbe,
        }


def semantic_score(ranking: int, max_ranking: int) -> float:
    """把语义排名（越小越靠前）映射到 (0.1, 1.0]，第 1 名记 1.0。

    保留 0.1 的下限，避免末位候选的语义分被压到 0 而完全由 cred 决定。
    """
    if max_ranking <= 1:
        return 1.0
    r = max(1, int(ranking))
    return round(max(0.0, 1.0 - (r - 1) / (max_ranking - 1)) * 0.9 + 0.1, 6)


def cred_rank(
    candidates: List[Dict[str, Any]],
    store: ReputationStore,
    cfg: Optional[RankConfig] = None,
) -> List[RankedCandidate]:
    """对发现结果做复合重排。

    Args:
        candidates: 每项形如::

                {
                    "aic": "...",          # 必填
                    "skillId": "...",      # 必填
                    "ranking": 1,          # 必填，发现服务给出的语义序（1 起）
                    "acsVersion": "",      # 可选，按版本隔离证据
                    "provider": "",        # 可选，用于同源降权
                }

        store: 已灌入凭证的信誉库。
        cfg: 排序超参数。

    Returns:
        按 ``finalRanking`` 排好序的 ``RankedCandidate`` 列表。
    """
    cfg = cfg or RankConfig()
    if not candidates:
        return []

    max_ranking = max(int(c.get("ranking", 1)) for c in candidates)
    ranked: List[RankedCandidate] = []
    for c in candidates:
        aic = c["aic"]
        skill_id = c["skillId"]
        acs_version = str(c.get("acsVersion", "") or "")
        detail = store.score(
            aic, skill_id, acs_version=acs_version, provider=c.get("provider", "")
        )
        sem = semantic_score(int(c.get("ranking", 1)), max_ranking)
        cred = detail.cred / 100.0
        combined = cfg.semantic_weight * sem + cfg.cred_weight * cred
        ranked.append(
            RankedCandidate(
                aic=aic,
                skillId=skill_id,
                semanticScore=sem,
                credScore=detail.cred,
                combined=combined,
                originalRanking=int(c.get("ranking", 1)),
                tier=detail.tier,
                samples=detail.samples,
                needsProbe=(detail.cred < cfg.require_probe_below)
                or detail.tier == "cold-start",
                detail=detail,
                extra={"acsVersion": acs_version},
            )
        )

    ranked.sort(key=lambda x: (-x.combined, x.originalRanking))

    # ---- 探索配额：把冷启动候选插入到靠前位置，打破「零证据 → 零曝光」死循环 ----
    if cfg.explore_quota > 0:
        picked = list(ranked[: cfg.explore_slot - 1])
        rest = ranked[len(picked):]
        explorers = [x for x in rest if x.tier == "cold-start"][: cfg.explore_quota]
        if explorers:
            for e in explorers:
                e.exploration = True
                rest.remove(e)
            head = picked + explorers
            head.sort(key=lambda x: -x.combined)
            ranked = head + rest

    for i, item in enumerate(ranked, start=1):
        item.finalRanking = i
    return ranked


def explain_ranking(ranked: List[RankedCandidate]) -> List[str]:
    """生成人类可读的排序理由，便于复核、日志追溯与演示。

    Returns:
        每个候选一行，形如::

            #1 <AIC> · <skillId> | 语义第1名 语义=1.00 Cred=72.4(n=12,high) 综合=0.834
    """
    lines: List[str] = []
    for r in ranked:
        flags = []
        if r.exploration:
            flags.append("探索配额")
        if r.needsProbe:
            flags.append("需预检")
        if r.tier == "cold-start":
            flags.append("冷启动")
        tag = f" [{','.join(flags)}]" if flags else ""
        lines.append(
            f"#{r.finalRanking} {r.aic} · {r.skillId} | 语义第{r.originalRanking}名 "
            f"语义={r.semanticScore:.2f} Cred={r.credScore:.1f}(n={r.samples},{r.tier}) "
            f"综合={r.combined:.3f}{tag}"
        )
    return lines
