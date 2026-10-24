# -*- coding: utf-8 -*-
"""CredRank 复合排序与探索配额的单元测试。

运行方式：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acps_cred_algo import (  # noqa: E402
    RankConfig,
    Receipt,
    ReputationStore,
    cred_rank,
    explain_ranking,
    semantic_score,
)

LEADER = "1.2.156.3088.1.BUPT.TEST.LEADER.0BBB"


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _fill(store: ReputationStore, aic: str, n_ok: int, n_fail: int = 0, ms: int = 120) -> None:
    for i in range(n_ok):
        store.ingest(
            Receipt(
                leaderAic=LEADER,
                partnerAic=aic,
                skillId="demo.skill",
                terminalState="completed",
                durationMs=ms + i,
                finishedAt=_iso(i * 0.1),
            )
        )
    for i in range(n_fail):
        store.ingest(
            Receipt(
                leaderAic=LEADER,
                partnerAic=aic,
                skillId="demo.skill",
                terminalState="failed",
                durationMs=900 + i,
                finishedAt=_iso(i * 0.1),
            )
        )


class TestSemanticScore(unittest.TestCase):
    def test_single_candidate(self):
        self.assertEqual(semantic_score(1, 1), 1.0)

    def test_first_rank_is_one(self):
        self.assertEqual(semantic_score(1, 10), 1.0)

    def test_last_rank_floor(self):
        # 末位保留 0.1 下限，避免语义分被完全抹平
        self.assertAlmostEqual(semantic_score(10, 10), 0.1, places=6)

    def test_monotonic(self):
        scores = [semantic_score(i, 10) for i in range(1, 11)]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestCredRank(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cred_rank([], ReputationStore()), [])

    def test_cred_reranks_semantic(self):
        """语义靠后但可信度显著更高的候选，应反超语义第 1。

        注意：末位候选的语义分被压到 0.1 下限，因此本用例特意构造 5 个候选
        （语义分形成梯度），而不是 2 个 —— 只有 2 个候选时，0.4 的语义权重
        通常足以让第 1 名保住位置，这是有意保留的「语义相关性」下限。
        """
        store = ReputationStore()
        strong = "1.2.156.3088.1.BUPT.TEST.STRONG.0AAA"
        weak = "1.2.156.3088.1.BUPT.TEST.WEAK.0BBB"
        _fill(store, strong, n_ok=20, ms=100)
        _fill(store, weak, n_ok=1, n_fail=6, ms=900)

        cands = [
            {"aic": weak, "skillId": "demo.skill", "ranking": 1},
            {"aic": strong, "skillId": "demo.skill", "ranking": 2},
        ]
        for i, r in enumerate((3, 4, 5), start=1):
            aic = f"1.2.156.3088.1.BUPT.TEST.FILL{i}.0CCC"
            _fill(store, aic, n_ok=3, ms=300)
            cands.append({"aic": aic, "skillId": "demo.skill", "ranking": r})

        ranked = cred_rank(cands, store)
        pos = {r.aic: r.finalRanking for r in ranked}
        self.assertLess(pos[strong], pos[weak])
        self.assertGreater(
            next(r for r in ranked if r.aic == strong).credScore,
            next(r for r in ranked if r.aic == weak).credScore,
        )

    def test_final_ranking_is_contiguous(self):
        store = ReputationStore()
        cands = []
        for i in range(1, 6):
            aic = f"1.2.156.3088.1.BUPT.TEST.C{i:02d}.0AAA"
            _fill(store, aic, n_ok=5 + i, ms=100)
            cands.append({"aic": aic, "skillId": "demo.skill", "ranking": i})
        ranked = cred_rank(cands, store)
        self.assertEqual([r.finalRanking for r in ranked], [1, 2, 3, 4, 5])

    def test_needs_probe_flag(self):
        store = ReputationStore()
        cold = "1.2.156.3088.1.BUPT.TEST.COLD.0AAA"
        ranked = cred_rank([{"aic": cold, "skillId": "demo.skill", "ranking": 1}], store)
        self.assertTrue(ranked[0].needsProbe)
        self.assertEqual(ranked[0].tier, "cold-start")

    def test_exploration_quota_inserts_cold_start(self):
        """冷启动候选应被插入到靠前位置，而不是永远排在最后。"""
        store = ReputationStore()
        cands = []
        for i in range(1, 5):
            aic = f"1.2.156.3088.1.BUPT.TEST.HOT{i}.0AAA"
            _fill(store, aic, n_ok=20, ms=100)
            cands.append({"aic": aic, "skillId": "demo.skill", "ranking": i})
        cold = "1.2.156.3088.1.BUPT.TEST.COLDX.0AAA"
        cands.append({"aic": cold, "skillId": "demo.skill", "ranking": 5})

        cfg = RankConfig(explore_quota=1, explore_slot=3)
        ranked = cred_rank(cands, store, cfg)
        explorers = [r for r in ranked if r.exploration]
        self.assertEqual(len(explorers), 1)
        self.assertEqual(explorers[0].aic, cold)
        # 应落在前 3 名之内
        self.assertLessEqual(explorers[0].finalRanking, 3)

    def test_no_exploration_when_disabled(self):
        store = ReputationStore()
        cands = [
            {"aic": "1.2.156.3088.1.BUPT.TEST.H1.0AAA", "skillId": "demo.skill", "ranking": 1},
            {"aic": "1.2.156.3088.1.BUPT.TEST.C1.0AAA", "skillId": "demo.skill", "ranking": 2},
        ]
        _fill(store, cands[0]["aic"], n_ok=20, ms=100)
        ranked = cred_rank(cands, store, RankConfig(explore_quota=0))
        self.assertFalse(any(r.exploration for r in ranked))

    def test_brief_is_serializable(self):
        import json

        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.SER.0AAA"
        _fill(store, aic, n_ok=5)
        ranked = cred_rank([{"aic": aic, "skillId": "demo.skill", "ranking": 1}], store)
        payload = json.dumps([r.brief() for r in ranked], ensure_ascii=False)
        self.assertIn(aic, payload)

    def test_explain_ranking_readable(self):
        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.EXP.0AAA"
        _fill(store, aic, n_ok=5)
        ranked = cred_rank([{"aic": aic, "skillId": "demo.skill", "ranking": 1}], store)
        lines = explain_ranking(ranked)
        self.assertEqual(len(lines), 1)
        self.assertIn("#1", lines[0])
        self.assertIn("Cred=", lines[0])

    def test_tie_falls_back_to_semantic(self):
        """可信度相同时，应按原始语义序排。"""
        store = ReputationStore()
        a = "1.2.156.3088.1.BUPT.TEST.TIE1.0AAA"
        b = "1.2.156.3088.1.BUPT.TEST.TIE2.0AAA"
        _fill(store, a, n_ok=6, ms=100)
        _fill(store, b, n_ok=6, ms=100)
        ranked = cred_rank(
            [
                {"aic": b, "skillId": "demo.skill", "ranking": 1},
                {"aic": a, "skillId": "demo.skill", "ranking": 2},
            ],
            store,
        )
        self.assertEqual(ranked[0].aic, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
