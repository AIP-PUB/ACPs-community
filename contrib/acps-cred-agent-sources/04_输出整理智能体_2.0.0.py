import re

from .common import ServiceUnavailable, llm

DIMENSIONS = ["事实忠实度", "任务覆盖", "可读性", "结构格式", "引用保留"]


def citations(text):
    return set(re.findall(r"https?://[^\s<>\]\)\"，。；]+|\[\d+\]", text))


def run(payload):
    original = str(payload.get("text", "")).strip()
    if not original:
        return {"status": "awaiting-input", "missing": ["text：待整理文本"]}
    purpose = payload.get("purpose", "清晰呈现，保留证据、限定条件和不确定性")
    mode = payload.get("tone", "正式")
    audit, candidate = [], original
    try:
        for round_no in range(3):
            generated = llm("按用户目的整理文本，不增加新事实，保留所有来源、数值、限定和不确定性。返回 JSON：text 字符串。只修复反馈中未达标项。", {
                "original": original, "current": candidate, "purpose": purpose, "tone": mode,
                "feedback": audit[-1] if audit else None})
            if not isinstance(generated.get("text"), str) or not generated["text"].strip():
                raise ServiceUnavailable("整理结果为空或格式无效。")
            candidate = generated["text"]
            judged = llm("独立对照原文评估整理结果。返回 JSON：scores 对象，五项事实忠实度、任务覆盖、可读性、结构格式、引用保留各整数0到2；issues 字符串数组；reason 字符串。事实有新增/删掉关键限定即忠实度不能为2。", {
                "original": original, "candidate": candidate, "purpose": purpose})
            scores = judged.get("scores", {})
            if any(type(scores.get(d)) is not int or scores[d] not in (0, 1, 2) for d in DIMENSIONS):
                raise ServiceUnavailable("质量评分缺少维度或超出范围。")
            if citations(original) - citations(candidate):
                scores["引用保留"] = 0
                judged.setdefault("issues", []).append("检测到原文引用标记或 URL 丢失。")
            passed = sum(scores.values()) >= 8 and scores["事实忠实度"] == 2 and not (citations(original) - citations(candidate))
            audit.append({"round": round_no, "scores": scores, "total": sum(scores.values()),
                          "passed": passed, "reason": judged.get("reason"), "issues": judged.get("issues", []),
                          "assessment": "模型对照评估加引用机械检查；非外部事实真实性证明"})
            if passed:
                break
        return {"status": "ok" if passed else "quality-not-passed", "text": candidate,
                "audit": audit, "revision_count": len(audit)-1, "passed": passed,
                "unresolved": [] if passed else audit[-1]["issues"]}
    except ServiceUnavailable as e:
        return {"status": "dependency-unavailable", "text": original, "passed": False,
                "audit": audit, "reason": str(e), "unresolved": ["未完成质量验证，原文回退；没有虚构评分。"]}
