"""ACPs-Cred 算法模块（acps-cred-algo）。

从 ACPs-Cred 智能体互联作品中抽出的**纯算法层**，零外部依赖：

- :class:`~acps_cred_algo.reputation.ReputationStore` —— 能力可信度评分
  （Wilson 置信下界 + 时延 + 证据充分度 + 来源多样性 + 版本新鲜度）
- :func:`~acps_cred_algo.ranker.cred_rank` —— 复合排序（语义 × 可信度 + 探索配额）
- :class:`~acps_cred_algo.models.Receipt` —— 交互凭证模型

核心算法不依赖任何 AIP SDK、不依赖网络、不依赖数据库，
可以直接用于其他智能体互联平台或离线实验。

快速开始::

    from acps_cred_algo import Receipt, ReputationStore, cred_rank

    store = ReputationStore()
    store.ingest(Receipt(partnerAic="A", skillId="s", terminalState="completed",
                         durationMs=120, finishedAt="2026-10-20T12:00:00+00:00"))
    detail = store.score("A", "s")
    print(detail.cred, detail.tier)

    ranked = cred_rank(
        [{"aic": "A", "skillId": "s", "ranking": 1}],
        store,
    )
"""

from .models import (
    FAILURE_STATES,
    NEUTRAL_STATES,
    RECEIPT_SCHEMA,
    SCORE_SCHEMA,
    SUCCESS_STATES,
    Evidence,
    Receipt,
    canonical_json,
    now_iso,
    result_digest_of,
    sha256_hex,
)
from .ranker import (
    RankConfig,
    RankedCandidate,
    cred_rank,
    explain_ranking,
    semantic_score,
)
from .reputation import (
    ReputationConfig,
    ReputationStore,
    ScoreDetail,
    age_days,
    wilson_lower_bound,
)

__version__ = "1.0.0"

__all__ = [
    # 模型
    "Receipt",
    "Evidence",
    "RECEIPT_SCHEMA",
    "SCORE_SCHEMA",
    "SUCCESS_STATES",
    "FAILURE_STATES",
    "NEUTRAL_STATES",
    "canonical_json",
    "sha256_hex",
    "result_digest_of",
    "now_iso",
    # 评分
    "ReputationStore",
    "ReputationConfig",
    "ScoreDetail",
    "wilson_lower_bound",
    "age_days",
    # 排序
    "cred_rank",
    "RankConfig",
    "RankedCandidate",
    "semantic_score",
    "explain_ranking",
    "__version__",
]
