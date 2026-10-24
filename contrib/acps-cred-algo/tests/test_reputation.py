# -*- coding: utf-8 -*-
"""ReputationStore 与 Wilson 下界的单元测试。

运行方式（零依赖）：
    python -m unittest discover -s tests -v

或在项目根目录：
    python -m unittest tests.test_reputation -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acps_cred_algo import (  # noqa: E402
    Receipt,
    ReputationConfig,
    ReputationStore,
    wilson_lower_bound,
)


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _rcpt(
    aic: str = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA",
    skill: str = "demo.skill",
    state: str = "completed",
    *,
    leader: str = "1.2.156.3088.1.BUPT.TEST.LEADER.0BBB",
    days_ago: float = 0,
    ms: int = 120,
    provider: str = "",
    acs_version: str = "",
    traceback: bool = False,
) -> Receipt:
    from acps_cred_algo import Evidence

    r = Receipt(
        leaderAic=leader,
        partnerAic=aic,
        skillId=skill,
        terminalState=state,
        durationMs=ms,
        finishedAt=_iso(days_ago),
        acsVersion=acs_version,
    )
    if provider or traceback:
        r.evidence = Evidence(
            source="traceback-confirmed" if traceback else "leader-observation",
            issuerAic=leader,
            issuerProvider=provider,
            peerAic=aic,
            tracebackVerified=traceback,
        )
    return r


class TestWilson(unittest.TestCase):
    """Wilson 下界的数学性质 —— 这是「防一次好评登顶」的基础。"""

    def test_empty(self):
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)

    def test_single_success_is_conservative(self):
        # 1/1 原始成功率 100%，但下界应远低于 100%
        lb = wilson_lower_bound(1, 1)
        self.assertLess(lb, 0.30)
        self.assertGreater(lb, 0.15)

    def test_large_sample_beats_single_success(self):
        # 98/100 的下界应显著高于 1/1 —— 这是本算法的核心主张
        self.assertGreater(wilson_lower_bound(98, 100), wilson_lower_bound(1, 1))

    def test_bounds(self):
        for s, t in [(0, 10), (5, 10), (10, 10)]:
            lb = wilson_lower_bound(s, t)
            self.assertGreaterEqual(lb, 0.0)
            self.assertLessEqual(lb, 1.0)

    def test_minority_of_failures(self):
        # 成功数越多，下界越高（同一总量下单调）
        self.assertLess(wilson_lower_bound(3, 10), wilson_lower_bound(7, 10))


class TestIngest(unittest.TestCase):
    def test_cold_start(self):
        store = ReputationStore()
        d = store.score("A", "s")
        self.assertEqual(d.samples, 0)
        self.assertEqual(d.tier, "cold-start")
        self.assertEqual(d.cred, ReputationConfig().cold_start_cred)

    def test_self_review_rejected(self):
        store = ReputationStore()
        same = "1.2.156.3088.1.BUPT.TEST.SAME.0CCC"
        r = _rcpt(aic=same, leader=same)
        self.assertFalse(store.ingest(r))
        self.assertEqual(store.ingested, 0)

    def test_missing_fields_rejected(self):
        store = ReputationStore()
        self.assertFalse(store.ingest(Receipt(partnerAic="", skillId="s")))
        self.assertFalse(store.ingest(Receipt(partnerAic="A", skillId="")))

    def test_dedup(self):
        store = ReputationStore()
        r = _rcpt()
        self.assertTrue(store.ingest(r))
        self.assertFalse(store.ingest(Receipt.from_dict(r.to_dict())))
        self.assertEqual(store.duplicates, 1)
        self.assertEqual(store.ingested, 1)

    def test_stats(self):
        store = ReputationStore()
        store.ingest_many([_rcpt(ms=100), _rcpt(ms=110)])
        st = store.stats()
        self.assertEqual(st["ingested"], 2)
        self.assertEqual(st["distinctAgentSkill"], 1)


class TestScoring(unittest.TestCase):
    def test_success_beats_failure(self):
        good, bad = ReputationStore(), ReputationStore()
        for i in range(5):
            good.ingest(_rcpt(ms=100 + i))
            bad.ingest(_rcpt(state="failed", ms=100 + i))
        self.assertGreater(good.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill").cred,
                           bad.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill").cred)

    def test_neutral_excluded_from_denominator(self):
        store = ReputationStore()
        store.ingest_many([_rcpt(), _rcpt(), _rcpt(state="canceled")])
        d = store.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill")
        self.assertEqual(d.neutrals, 1)
        self.assertEqual(d.successes, 2)
        # 中性终态不计入成功率分母
        self.assertEqual(d.successRate, 1.0)

    def test_small_sample_is_cold_start(self):
        store = ReputationStore()
        store.ingest(_rcpt())
        d = store.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill")
        self.assertEqual(d.tier, "cold-start")  # min_samples 默认 3

    def test_enough_samples_not_cold_start(self):
        store = ReputationStore()
        for i in range(5):
            store.ingest(_rcpt(ms=100 + i))
        d = store.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill")
        self.assertNotEqual(d.tier, "cold-start")
        self.assertGreater(d.cred, 0)

    def test_time_decay(self):
        fresh, old = ReputationStore(), ReputationStore()
        for i in range(4):
            fresh.ingest(_rcpt(days_ago=0, ms=100 + i))
            old.ingest(_rcpt(days_ago=120, ms=100 + i))
        a = fresh.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill")
        b = old.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill")
        self.assertGreater(a.confidence, b.confidence)

    def test_provider_diversity(self):
        one, two = ReputationStore(), ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        for i in range(4):
            one.ingest(_rcpt(provider="team-A", ms=100 + i))
            two.ingest(_rcpt(provider="team-A" if i % 2 else "team-B", ms=100 + i))
        d1 = one.score(aic, "demo.skill")
        d2 = two.score(aic, "demo.skill")
        self.assertEqual(d1.diversityFactor, 0.8)
        self.assertEqual(d2.diversityFactor, 1.0)
        self.assertGreater(d2.cred, d1.cred)

    def test_self_provider_discounted(self):
        """同一 provider 的自评凭证应被降权。"""
        trusted, selfprod = ReputationStore(), ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        for i in range(4):
            trusted.ingest(_rcpt(provider="other-team", ms=100 + i))
            selfprod.ingest(_rcpt(provider="owner-team", ms=100 + i))
        a = trusted.score(aic, "demo.skill")
        b = selfprod.score(aic, "demo.skill", provider="owner-team")
        self.assertGreater(a.confidence, b.confidence)

    def test_stale_acs_penalty(self):
        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        for i in range(4):
            store.ingest(_rcpt(days_ago=10, ms=100 + i))
        before = store.score(aic, "demo.skill")
        # ACS 在证据之后被修改 → 证据过期
        store.note_acs(aic, _iso(days_ago=0), version="2.0.0")
        after = store.score(aic, "demo.skill")
        self.assertTrue(after.staleEvidence)
        self.assertLess(after.cred, before.cred)

    def test_acs_version_isolation(self):
        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        store.ingest_many([_rcpt(acs_version="1.0.0", ms=100) for _ in range(5)])
        store.ingest_many([_rcpt(acs_version="2.0.0", state="failed", ms=900) for _ in range(5)])
        old = store.score(aic, "demo.skill", acs_version="1.0.0")
        new = store.score(aic, "demo.skill", acs_version="2.0.0")
        self.assertEqual(old.failures, 0)
        self.assertEqual(new.successes, 0)
        self.assertGreater(old.cred, new.cred)

    def test_traceback_counted(self):
        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        store.ingest_many([_rcpt(traceback=True) for _ in range(3)])
        d = store.score(aic, "demo.skill")
        self.assertEqual(d.tracebackConfirmed, 3)

    def test_persistence_roundtrip(self):
        import tempfile

        store = ReputationStore()
        aic = "1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA"
        store.ingest_many([_rcpt(ms=100 + i) for i in range(4)])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rep.json"
            store.save(path)
            loaded = ReputationStore.load(path)
        self.assertEqual(loaded.ingested, store.ingested)
        self.assertAlmostEqual(
            loaded.score(aic, "demo.skill").cred,
            store.score(aic, "demo.skill").cred,
            places=4,
        )

    def test_score_detail_has_all_terms(self):
        """可解释性：必须回传全部中间量。"""
        store = ReputationStore()
        for i in range(4):
            store.ingest(_rcpt(ms=100 + i))
        d = store.score("1.2.156.3088.1.BUPT.TEST.PARTNER.0AAA", "demo.skill").to_dict()
        for key in (
            "samples", "successes", "failures", "neutrals", "successRate",
            "wilsonLower", "latencyEwmaMs", "latencyScore", "confidence",
            "distinctIssuerProviders", "diversityFactor", "freshnessFactor",
            "cred", "tier", "asOf",
        ):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
