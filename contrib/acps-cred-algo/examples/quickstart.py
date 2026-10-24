# -*- coding: utf-8 -*-
"""最小可运行示例：从零开始完成「灌凭证 → 评分 → 排序 → 解释」全流程。

运行：
    python examples/quickstart.py

不需要安装任何第三方依赖，也不需要联网。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acps_cred_algo import (  # noqa: E402
    RankConfig,
    Receipt,
    ReputationStore,
    cred_rank,
    explain_ranking,
)

LEADER = "1.2.156.3088.1.BUPT.DEMO.LEADER.0AAA"


def days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def make(aic: str, state: str, ms: int, *, d: float = 0, provider: str = "") -> Receipt:
    """构造一条凭证。

    注意 ``provider`` 的语义：它是**凭证签发方所属团队**，不是被评价方。
    若这里填成被评价方自己的团队，``score(provider=...)`` 会把它判为「同源自评」
    并施加 0.3 的权重折扣 —— 这正是设计意图（抑制自吹自擂）。
    在真实系统里，签发方就是编排任务的那一方（Leader），通常不等于被评价的 Partner。
    """
    from acps_cred_algo import Evidence

    r = Receipt(
        leaderAic=LEADER,
        partnerAic=aic,
        skillId="demo.translate",
        terminalState=state,
        durationMs=ms,
        finishedAt=days_ago(d),
    )
    if provider:
        r.evidence = Evidence(issuerProvider=provider, peerAic=aic)
    return r


def main() -> None:
    store = ReputationStore()

    # 三个候选：A 稳定、B 快但常失败、C 全新（冷启动）
    A = "1.2.156.3088.1.BUPT.DEMO.AGENT_A.0AA1"
    B = "1.2.156.3088.1.BUPT.DEMO.AGENT_B.0BB2"
    C = "1.2.156.3088.1.BUPT.DEMO.AGENT_C.0CC3"

    receipts = []
    # 凭证由编排方（leader-team）签发，而不是被评价方自己 —— 因此不会触发同源降权
    receipts += [make(A, "completed", 150, d=i * 0.3, provider="leader-team") for i in range(12)]
    receipts += [make(B, "completed", 60, d=i * 0.3, provider="leader-team") for i in range(2)]
    receipts += [make(B, "failed", 900, d=i * 0.3, provider="leader-team") for i in range(7)]
    # C 没有任何凭证 —— 模拟刚注册的新智能体

    added = store.ingest_many(receipts)
    print(f"灌入凭证 {len(receipts)} 条，实际入库 {added} 条（去重后）\n")

    for aic, label in [(A, "A 稳定"), (B, "B 快但常失败"), (C, "C 全新")]:
        d = store.score(aic, "demo.translate")
        print(f"{label:<12} cred={d.cred:>6.2f}  tier={d.tier:<11} "
              f"样本={d.samples:>2}  成功={d.successes:>2} 失败={d.failures:>2}")
        print(f"{'':<12} Wilson下界={d.wilsonLower:.3f}  置信度={d.confidence:.3f}  "
              f"时延={d.latencyEwmaMs:.0f}ms  多样性={d.diversityFactor:.1f}")
        print()

    # 假设发现服务给出的语义序是 A→B→C（A 最相关）
    candidates = [
        {"aic": A, "skillId": "demo.translate", "ranking": 1, "provider": "team-1"},
        {"aic": B, "skillId": "demo.translate", "ranking": 2, "provider": "team-2"},
        {"aic": C, "skillId": "demo.translate", "ranking": 3},
    ]

    print("=" * 78)
    print("按纯语义排名（发现服务原始结果）：" + " → ".join(c["aic"][-4:] for c in candidates))
    print("=" * 78)

    ranked = cred_rank(candidates, store, RankConfig(explore_quota=1, explore_slot=2))
    print("\nCredRank 重排结果：")
    for line in explain_ranking(ranked):
        print("  " + line)

    print("\n要点：")
    print("  · B 语义第 2，但失败率高 → 被降权到后面")
    print("  · C 完全没证据 → 靠「探索配额」被插到第 2 位，避免永远没有曝光机会")
    print("  · 每个分数都能反查到中间量，不是黑盒")


if __name__ == "__main__":
    main()
