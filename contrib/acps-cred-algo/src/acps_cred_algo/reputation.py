"""能力可信度（Cred）计算 —— 核心算法。

设计目标：把"可核验的交互凭证"聚合成一个**可比较、可解释、抗操纵**的标量评分。

## 评分公式

对每个 (AIC, skillId, acsVersion) 聚合后的凭证集合：

```
                ┌─ 成功/失败计数 ─┐
wilsonLower  =  Wilson(success, counted, z=1.96)        ← 二项分布 95% 置信下界

latencyScore =  min(1, latency_budget_ms / latencyEwma)  ← 时延得分（越快越高）

confidence   =  w_total / (w_total + k)                  ← 证据充分度（衰减后权重）

diversity    =  min(1, 0.6 + 0.2 × max(1, distinctProviders))    ← 来源多样性（单源降权）

freshness    =  1.0 或 0.5                               ← ACS 版本晚于证据时降权

base         =  0.75 × wilsonLower + 0.25 × latencyScore

cred01       =  confidence × diversity × freshness × base

cred         =  round(100 × cred01, 2)
```

其中 `w_total` 是**按半衰期衰减后的**证据权重之和，而不是原始条数 ——
这样批量补录很久以前的凭证无法提升置信度。

## 抗操纵设计（每一处都对应一种攻击）

| 攻击方式 | 防御机制 |
|---|---|
| 一次好评登顶（1/1 = 100%） | Wilson 下界 + 置信度因子、`min_samples` 冷启动判定 |
| 批量补录旧凭证刷分 | 时间衰减按**衰减后权重**算置信度，不看条数 |
| 自评（自己给自己好评） | `ingest()` 直接拒绝 `leaderAic == partnerAic` 的凭证 |
| 同一 provider 批量刷好评 | 来源多样性因子 + 同 provider 折扣 `self_provider_weight` |
| 借旧版本信誉卖新能力 | ACS 版本隔离 + `stale_acs_factor` 整体降权 |
| 重复提交同一凭证 | 凭证摘要去重（`_digests` 集合） |

## 可解释性

`score()` 返回 `ScoreDetail`，包含**全部中间量**（样本数、成功/失败/中性计数、
原始成功率、Wilson 下界、时延均值、置信度、多样性、新鲜度、回查确认数），
使评分结果**可复核、可争议** —— 这是与黑盒评分的本质区别。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import Receipt

__all__ = [
    "wilson_lower_bound",
    "age_days",
    "ReputationConfig",
    "ScoreDetail",
    "ReputationStore",
]


# ────────────────────────────── 基础函数 ──────────────────────────────


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        raw = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def age_days(ts: str, *, now: Optional[datetime] = None) -> float:
    """某时间戳距今多少天（无法解析时按 0 处理）。"""
    dt = _parse_iso(ts)
    if dt is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def wilson_lower_bound(success: int, total: int, z: float = 1.96) -> float:
    """二项分布的 Wilson 置信区间下界（默认 95%）。

    相比原始成功率 ``success/total``，它在小样本时自动保守：
    1/1 的原始成功率是 100%，但 Wilson 下界只有约 20.7%；
    98/100 的原始成功率是 98%，Wilson 下界约 93.0%。
    这正是「防止一次好评登顶」的数学基础。

    Args:
        success: 成功次数
        total: 参与统计的总次数（不含中性终态）
        z: 正态分位数，1.96 对应 95% 置信

    Returns:
        [0, 1] 区间内的下界值；``total <= 0`` 时返回 0。
    """
    if total <= 0:
        return 0.0
    phat = success / total
    denom = 1.0 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return max(0.0, min(1.0, (centre - margin) / denom))


# ────────────────────────────── 配置与结果 ──────────────────────────────


@dataclass
class ReputationConfig:
    """评分超参数。

    每个参数都在 `docs/ALGORITHM.md` 中给出取值依据与敏感性讨论，
    修改它们会改变评分的保守程度。
    """

    half_life_days: float = 14.0        # 时间衰减半衰期（天）
    prior_weight: float = 5.0           # 置信度因子 k：需要多少证据才算「可信」
    wilson_z: float = 1.96              # 95% 置信
    latency_budget_ms: float = 800.0    # 达到该时延即拿满时延分
    success_weight: float = 0.75        # 成功率与延迟的权重分配
    self_provider_weight: float = 0.3   # 同 provider 自评凭证的权重折扣
    min_samples: int = 3                # 低于该样本数视为冷启动
    stale_acs_factor: float = 0.5       # ACS 版本晚于证据时的整体降权
    cold_start_cred: float = 20.0       # 冷启动期名义分（用于展示，不参与排序压制）


@dataclass
class ScoreDetail:
    """一次评分的完整可解释明细。

    设计原则：**返回全部中间量**，让调用方能够复核每一步计算，
    而不是只给一个无法追责的数字。
    """

    aic: str = ""
    skillId: str = ""
    acsVersion: str = ""
    samples: int = 0
    successes: int = 0
    failures: int = 0
    neutrals: int = 0
    successRate: float = 0.0
    wilsonLower: float = 0.0
    latencyEwmaMs: float = 0.0
    latencyScore: float = 0.0
    confidence: float = 0.0
    distinctIssuerProviders: int = 0
    diversityFactor: float = 1.0
    freshnessFactor: float = 1.0
    staleEvidence: bool = False
    cred: float = 0.0
    tier: str = "cold-start"
    tracebackConfirmed: int = 0
    asOf: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# ────────────────────────────── 核心存储与评分 ──────────────────────────────


class ReputationStore:
    """凭证聚合与评分存储。

    轻量实现：内存索引 + JSON 文件落盘。
    生产环境可替换为数据库/时序库，**对外接口保持不变**。

    索引结构：``(aic, skillId) -> {acsVersion -> [Receipt, ...]}``
    """

    def __init__(self, config: Optional[ReputationConfig] = None):
        self.cfg = config or ReputationConfig()
        self._by_key: Dict[Tuple[str, str], Dict[str, List[Receipt]]] = {}
        self._digests: set[str] = set()
        self._acs_modified: Dict[str, str] = {}
        self.ingested = 0
        self.duplicates = 0

    # ------------------------------------------------------------------ 写入

    def note_acs(self, aic: str, last_modified_time: str, version: str = "") -> None:
        """记录目标智能体 ACS 的最后修改时间，用于版本隔离与过期判定。"""
        if aic and last_modified_time:
            self._acs_modified[aic] = last_modified_time

    def ingest(self, receipt: Receipt) -> bool:
        """写入一条凭证。

        Returns:
            True = 新入库；False = 重复、字段缺失或自评被拒。
        """
        if not receipt.partnerAic or not receipt.skillId:
            return False
        # 自评（编排方与被评价方同一身份）直接拒绝：无信息量且是刷分入口
        if receipt.leaderAic and receipt.leaderAic == receipt.partnerAic:
            return False
        digest = receipt.digest
        if digest in self._digests:
            self.duplicates += 1
            return False
        self._digests.add(digest)

        key = (receipt.partnerAic, receipt.skillId)
        bucket = self._by_key.setdefault(key, {})
        bucket.setdefault(receipt.acsVersion or "", []).append(receipt)
        self.ingested += 1
        return True

    def ingest_many(self, receipts: Iterable[Receipt]) -> int:
        """批量写入，返回实际新增条数。"""
        return sum(1 for r in receipts if self.ingest(r))

    # ------------------------------------------------------------------ 评分

    def _effective_receipts(
        self, aic: str, skill_id: str, acs_version: str = ""
    ) -> List[Receipt]:
        bucket = self._by_key.get((aic, skill_id))
        if not bucket:
            return []
        if acs_version:
            return list(bucket.get(acs_version, []))
        # 未指定版本时，取凭证数量最多的版本组（不保证时间最新）作为当前能力证据
        return list(max(bucket.values(), key=len))

    def score(
        self,
        aic: str,
        skill_id: str,
        *,
        acs_version: str = "",
        provider: str = "",
    ) -> ScoreDetail:
        """计算某个 (AIC, 技能) 的能力可信度。

        Args:
            aic: 目标智能体身份码
            skill_id: 技能标识
            acs_version: 按版本隔离证据（留空则取凭证数量最多的版本组）
            provider: 目标智能体的提供方；用于对同源自评凭证降权

        Returns:
            ``ScoreDetail``，含最终 ``cred``（0–100）与全部中间量。
        """
        detail = ScoreDetail(aic=aic, skillId=skill_id, acsVersion=acs_version)
        receipts = self._effective_receipts(aic, skill_id, acs_version)
        detail.samples = len(receipts)
        if not receipts:
            detail.cred = self.cfg.cold_start_cred
            detail.tier = "cold-start"
            detail.asOf = datetime.now(timezone.utc).isoformat()
            return detail

        now = datetime.now(timezone.utc)
        w_success = 0.0
        w_total_evi = 0.0
        lat_weighted = 0.0
        lat_weight = 0.0
        providers: set[str] = set()

        for r in receipts:
            if r.is_neutral:
                detail.neutrals += 1
                continue
            # 时间衰减：半衰期越久权重越低
            decay = 0.5 ** (age_days(r.finishedAt, now=now) / self.cfg.half_life_days)
            issuer_provider = (r.evidence.issuerProvider or "").strip()
            if provider and issuer_provider and issuer_provider == provider:
                # 同源提供方的自评凭证降权，提高刷分成本
                decay *= self.cfg.self_provider_weight
            else:
                if issuer_provider:
                    providers.add(issuer_provider)
            if r.evidence.tracebackVerified:
                detail.tracebackConfirmed += 1

            if r.is_success:
                detail.successes += 1
                w_success += decay
            elif r.is_failure:
                detail.failures += 1
            # 中性/未知终态：既不算成功也不算失败，但计入总体证据权重
            w_total_evi += decay
            if r.durationMs > 0 and (r.is_success or r.is_failure):
                lat_weighted += r.durationMs * decay
                lat_weight += decay

        counted = detail.successes + detail.failures
        detail.successRate = (detail.successes / counted) if counted else 0.0
        detail.wilsonLower = wilson_lower_bound(detail.successes, counted, self.cfg.wilson_z)
        detail.latencyEwmaMs = (lat_weighted / lat_weight) if lat_weight else 0.0
        detail.latencyScore = (
            min(1.0, self.cfg.latency_budget_ms / detail.latencyEwmaMs)
            if detail.latencyEwmaMs > 0
            else 0.0
        )

        # 置信度：按「衰减后的证据权重」而非原始条数，避免批量刷旧凭证
        detail.confidence = w_total_evi / (w_total_evi + self.cfg.prior_weight)

        # 来源多样性：单一 provider（含无 provider 信息）记 0.8，≥2 家记满 1.0
        detail.distinctIssuerProviders = len(providers)
        detail.diversityFactor = min(1.0, 0.6 + 0.2 * max(1, len(providers)))

        # 版本隔离：ACS 在证据之后被修改 -> 该批证据过期降权
        acs_mod = self._acs_modified.get(aic, "")
        latest_evidence = max((r.finishedAt for r in receipts if r.finishedAt), default="")
        if acs_mod and latest_evidence:
            a, b = _parse_iso(acs_mod), _parse_iso(latest_evidence)
            if a and b and a > b:
                detail.staleEvidence = True
                detail.freshnessFactor = self.cfg.stale_acs_factor

        base = (
            self.cfg.success_weight * detail.wilsonLower
            + (1 - self.cfg.success_weight) * detail.latencyScore
        )
        cred01 = (
            detail.confidence
            * detail.diversityFactor
            * detail.freshnessFactor
            * base
        )
        detail.cred = round(100.0 * max(0.0, min(1.0, cred01)), 2)

        if counted < self.cfg.min_samples:
            detail.tier = "cold-start"
        elif detail.cred >= 55:
            detail.tier = "high"
        elif detail.cred >= 25:
            detail.tier = "medium"
        else:
            detail.tier = "low"
        detail.asOf = now.isoformat()
        return detail

    # ------------------------------------------------------------------ 持久化

    def to_dict(self) -> Dict[str, Any]:
        receipts = [
            r.to_dict()
            for bucket in self._by_key.values()
            for grp in bucket.values()
            for r in grp
        ]
        return {
            "config": dict(self.cfg.__dict__),
            "acsModified": self._acs_modified,
            "receipts": receipts,
        }

    def save(self, path: str | Path) -> None:
        """导出为 JSON 文件。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> "ReputationStore":
        """从 JSON 文件恢复。"""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = ReputationConfig(**raw.get("config", {}))
        store = cls(cfg)
        store._acs_modified = dict(raw.get("acsModified", {}))
        for item in raw.get("receipts", []):
            store.ingest(Receipt.from_dict(item))
        return store

    def stats(self) -> Dict[str, Any]:
        """存储概况（用于监控与调试）。"""
        keys = [
            {"aic": a, "skillId": s, "versions": len(v)}
            for (a, s), v in self._by_key.items()
        ]
        return {
            "ingested": self.ingested,
            "duplicates": self.duplicates,
            "distinctAgentSkill": len(keys),
        }
