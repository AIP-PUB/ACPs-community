"""No user-supplied observation is automatically trusted."""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

from .common import digest, json_request, llm, ServiceUnavailable, now

RULE = {"version": "cred-v1", "weights": {"success_rate": 0.7, "source_diversity": 0.15, "freshness": 0.15},
        "minimum_samples": 5, "window_days": 90, "neutral": "不进入成功率分母",
        "diversity": "min(独立已认证调用方数量/3,1)", "freshness": "90天内记录占比",
        "missing": "无证据或少于5个非中性样本时不输出总分；声明不进入评分"}


def sign_record(record, key):
    return {"record": record, "signature": hmac.new(key.encode(), digest(record).encode(), hashlib.sha256).hexdigest()}


def verify_records(items, aic, skill):
    key = os.getenv("EVIDENCE_HMAC_KEY", "")
    valid, rejected, ids = [], 0, set()
    for item in items:
        try:
            r = item["record"]
            sig = sign_record(r, key)["signature"]
            stamp = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if not key or not hmac.compare_digest(sig, item["signature"]) or r["aic"] != aic:
                raise ValueError()
            if skill and r["skill_id"] != skill:
                raise ValueError()
            if r["outcome"] not in ("success", "failure", "neutral") or not r["authenticated_caller"] or age < 0:
                raise ValueError()
            if r["observation_id"] in ids:
                raise ValueError()
            ids.add(r["observation_id"])
            valid.append((r, age / 86400))
        except (KeyError, TypeError, ValueError):
            rejected += 1
    return valid, rejected


def run(payload):
    if payload.get("mode") == "review":
        plan = payload.get("plan")
        if not isinstance(plan, dict):
            return {"status": "awaiting-input", "missing": ["plan 对象"]}
        try:
            review = llm("你是方案评审。只分析方案声明，不评估真实交付能力。输出 JSON：passed 布尔、issues 数组、recommendations 数组、basis 数组。检查八项边界、可执行测试、ACS草案是否虚构身份、来源支撑、失败处理。不要调用其他智能体。", plan)
            if not isinstance(review.get("passed"), bool) or not isinstance(review.get("issues"), list):
                raise ServiceUnavailable("评审结果结构无效。")
            return {"status": "ok", "analysis_type": "方案声明分析（非实证能力评分）", **review}
        except ServiceUnavailable as e:
            return {"status": "dependency-unavailable", "passed": False, "reason": str(e)}
    aic = str(payload.get("aic", "")).strip()
    if not aic and payload.get("text"):
        match = re.search(r"1\.2\.156\.[A-Za-z0-9.]+", str(payload["text"]))
        aic = match.group(0).rstrip(".") if match else ""
    if not aic:
        return {"status": "awaiting-input", "missing": ["aic：平台分配的目标身份码（可选 skill_id）"]}
    skill = payload.get("skill_id")
    declaration, gaps = None, []
    url = os.getenv("DISCOVERY_LOOKUP_URL", "")
    if url:
        try:
            result = json_request(url, {"type": "filtered", "limit": 5,
                "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]},
                "extFilter": {"conditions": [{"field": "alive", "op": "in", "value": ["online", "offline", "unknown"]}]}}, timeout=12)
            # Platform adapter response must be a verified ACS, never a fuzzy first match.
            matches = []
            def walk(v):
                if isinstance(v, dict):
                    if v.get("aic") == aic and isinstance(v.get("skills"), list):
                        matches.append(v)
                    else:
                        for x in v.values(): walk(x)
                elif isinstance(v, list):
                    for x in v: walk(x)
            walk(result)
            if len(matches) != 1:
                raise ValueError()
            declaration = {"aic": aic, "skills": [s for s in matches[0]["skills"] if not skill or s.get("id") == skill],
                           "source": url, "queried_at": now(), "meaning": "能力自述，不能证明真实交付质量"}
        except Exception:
            gaps.append("发现接口不可用或未返回唯一匹配 AIC；未声称已取得 ACS。")
    else:
        gaps.append("尚未配置经核实的 DISCOVERY_LOOKUP_URL。")
    collected = []
    evidence_path = os.getenv('EVIDENCE_STORE_FILE')
    if evidence_path and Path(evidence_path).is_file():
        for line in Path(evidence_path).read_text(encoding='utf-8').splitlines()[-5000:]:
            try:
                item = json.loads(line)
                if item.get('record', {}).get('aic') == aic:
                    collected.append(item)
            except json.JSONDecodeError:
                continue
    records, rejected = verify_records(collected + payload.get("evidence", []), aic, skill)
    counts = {x: sum(r["outcome"] == x for r, age in records) for x in ("success", "failure", "neutral")}
    n = counts["success"] + counts["failure"]
    diversity = len({r["authenticated_caller"] for r, age in records if r["outcome"] != "neutral"})
    eligible = [(r, age) for r, age in records if r["outcome"] != "neutral"]
    components = {"success_rate": counts["success"] / n if n else None,
                  "source_diversity": min(diversity / 3, 1),
                  "freshness": sum(age <= 90 for r, age in eligible) / n if n else None}
    score = round(100 * sum(components[k] * w for k, w in RULE["weights"].items()), 2) if n >= 5 else None
    return {"status": "ok", "aic": aic, "skill_id": skill,
            "platform_facts": declaration, "evidence_gaps": gaps,
            "statistics": {"state": "有签名调用证据（仅代表受信采集器见证）" if n >= 5 else "冷启动/未知或样本不足",
                           "score": score, "sample_count": len(records), "counts": counts,
                           "source_diversity": diversity, "components": components,
                           "time_range": sorted(r["timestamp"] for r, age in records)[::max(len(records)-1, 1)],
                           "rejected_evidence": rejected, "rule": RULE,
                           "evidence_ids": [r["observation_id"] for r, age in records]},
            "unknown": ["未观察到的能力、未提供的技能、总体质量和未来表现均未知。"]}
